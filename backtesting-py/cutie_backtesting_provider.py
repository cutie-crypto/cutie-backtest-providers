"""
Cutie Backtest Provider: backtesting.py + ccxt

Local HTTP provider that runs backtesting.py with ccxt public OHLCV data.
Conforms to cutie.external_backtest.request/response.v1 schema (IMPL W3.8 §5).

Usage:
    CUTIE_BACKTEST_PROVIDER_TOKEN="your-token" \
      uvicorn cutie_backtesting_provider:app --host 127.0.0.1 --port 8765
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PORT = int(os.environ.get("CUTIE_BACKTEST_PORT", "8765"))
AUTH_TOKEN = os.environ.get("CUTIE_BACKTEST_PROVIDER_TOKEN", "")

PROVIDER_ID = "local-backtesting-py"
PROVIDER_NAME = "Local Backtesting.py"
PROVIDER_VERSION = "1.0.0"
PROVIDER_HOMEPAGE_URL = "https://kernc.github.io/backtesting.py/"
PROVIDER_MAINTAINER = "cutie-backtest-providers"
ENGINE_NAME = "backtesting.py"
DATA_SOURCE = "ccxt_public_ohlcv"
RESPONSE_SCHEMA = "cutie.external_backtest.response.v1"
DEFAULT_EXCHANGE = os.environ.get("CUTIE_BACKTEST_DEFAULT_EXCHANGE", "okx").lower()
DEFAULT_SUPPORTED_SYMBOLS = (
    "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,"
    "ADAUSDT,LINKUSDT,AVAXUSDT,TONUSDT"
)
EXECUTION_TIMEOUT_MS = 120000
EXECUTION_MAX_RANGE_DAYS = 365

BASE_DIR = Path(__file__).resolve().parent
REPORTS_DIR = BASE_DIR / "reports"
CACHE_DIR = BASE_DIR / "cache" / "ohlcv"
MAX_REPORTS = 100
CACHE_TTL_SECONDS = 24 * 60 * 60  # 24 hours

logger = logging.getLogger("cutie_backtesting_provider")

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Cutie Backtesting.py Provider", version="1.0.0")


@app.on_event("startup")
async def startup_warning():
    if not AUTH_TOKEN:
        logger.warning("CUTIE_BACKTEST_PROVIDER_TOKEN not set — running without authentication (dev mode)")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _engine_version() -> str:
    try:
        import backtesting
        return getattr(backtesting, "__version__", "unknown")
    except Exception:
        return "unknown"


def _normalize_catalog_symbol(raw: Any) -> str:
    compact = re.sub(r"[^A-Za-z0-9]", "", str(raw or "")).upper()
    if not compact:
        return ""
    for quote in ("USDT", "USDC", "USD", "BTC", "ETH", "BNB"):
        if compact.endswith(quote) and len(compact) > len(quote):
            return compact
    return f"{compact}USDT"


def _supported_symbols() -> list[str]:
    raw = os.environ.get("CUTIE_BACKTEST_SUPPORTED_SYMBOLS", DEFAULT_SUPPORTED_SYMBOLS)
    symbols: list[str] = []
    for item in re.split(r"[\s,]+", raw):
        symbol = _normalize_catalog_symbol(item)
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols or [_normalize_catalog_symbol("BTCUSDT")]


def _verify_bearer(authorization: Optional[str]) -> None:
    """Validate Bearer token. Raises 401 on mismatch."""
    if not AUTH_TOKEN:
        # No token configured -- accept anything (dev mode)
        return
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or parts[1] != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")


def _enforce_reports_retention() -> None:
    """Keep at most MAX_REPORTS HTML report files, delete oldest by mtime."""
    if not REPORTS_DIR.exists():
        return
    files = sorted(
        [f for f in REPORTS_DIR.iterdir() if f.is_file() and f.suffix == ".html"],
        key=lambda f: f.stat().st_mtime,
    )
    while len(files) > MAX_REPORTS:
        oldest = files.pop(0)
        try:
            oldest.unlink()
        except OSError as e:
            logger.warning("Failed to delete old report %s: %s", oldest, e)


def _cache_key(exchange: str, symbol: str, timeframe: str, start_ms: int, end_ms: int) -> str:
    return f"{exchange}_{symbol}_{timeframe}_{start_ms}_{end_ms}.json"


def _timeframe_milliseconds(timeframe: str) -> int:
    match = re.fullmatch(r"(\d+)([mhdwM])", str(timeframe or ""))
    if not match:
        return 60 * 60 * 1000
    value = int(match.group(1))
    unit = match.group(2)
    multipliers = {
        "m": 60 * 1000,
        "h": 60 * 60 * 1000,
        "d": 24 * 60 * 60 * 1000,
        "w": 7 * 24 * 60 * 60 * 1000,
        "M": 30 * 24 * 60 * 60 * 1000,
    }
    return value * multipliers[unit]


def _read_cache(key: str) -> Optional[list]:
    path = CACHE_DIR / key
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age > CACHE_TTL_SECONDS:
        path.unlink(missing_ok=True)
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Cache read failed for %s: %s", key, e)
        return None


def _write_cache(key: str, data: list) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / key
    try:
        with open(path, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logger.warning("Cache write failed for %s: %s", key, e)


def _safe_float(series: Any, key: str, default: float = 0.0) -> float:
    """Extract a float metric from a pd.Series or dict, with None/NaN protection.

    pd.Series.get(key, default) returns the stored value (even if None/NaN)
    when the key exists — the default is only used for missing keys.
    """
    raw = series.get(key, default) if hasattr(series, "get") else default
    if raw is None:
        return default
    try:
        val = float(raw)
        return val if math.isfinite(val) else default
    except (TypeError, ValueError):
        return default


def _safe_int(series: Any, key: str, default: int = 0) -> int:
    raw = series.get(key, default) if hasattr(series, "get") else default
    if raw is None:
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError, OverflowError):
        return default


def _json_safe(value: Any) -> Any:
    """Convert non-finite floats into JSON-safe nulls."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def _decimal_str(value: Any, places: int = 8) -> str:
    """Render a money/quantity value as a decimal string (IMPL §6.2).

    Money/quantity fields (equity, price, qty, cost, fee, capital) must be
    serialized as decimal strings, never JSON floats. Non-finite or unparseable
    values fall back to "0".
    """
    if isinstance(value, Decimal):
        dec = value
    else:
        if isinstance(value, float) and not math.isfinite(value):
            return "0"
        try:
            dec = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return "0"
    quantized = dec.quantize(Decimal(1).scaleb(-places))
    normalized = quantized.normalize()
    # Avoid scientific notation (e.g. 1E+4 -> 10000)
    return f"{normalized:f}"


def _fetch_ohlcv(exchange_id: str, symbol: str, timeframe: str,
                 start_sec: int, end_sec: int) -> pd.DataFrame:
    """Fetch OHLCV from ccxt with local file cache."""
    import ccxt

    start_ms = start_sec * 1000
    end_ms = min(end_sec * 1000, int(time.time() * 1000))
    if end_ms <= start_ms:
        raise ValueError("NO_DATA")

    cache_key = _cache_key(exchange_id, symbol, timeframe, start_ms, end_ms)
    cached = _read_cache(cache_key)
    if cached is not None:
        ohlcv = cached
    else:
        exchange_class = getattr(ccxt, exchange_id, None)
        if exchange_class is None:
            raise ValueError(f"Unsupported exchange: {exchange_id}")
        exchange = exchange_class({"enableRateLimit": True})

        # Normalize symbol: BTCUSDT / btcusdt -> BTC/USDT
        upper_symbol = symbol.upper()
        normalized_symbol = upper_symbol
        if "/" not in upper_symbol:
            # Try common patterns: BTCUSDT -> BTC/USDT
            for quote in ("USDT", "USDC", "BUSD", "BTC", "ETH", "BNB"):
                if upper_symbol.endswith(quote) and len(upper_symbol) > len(quote):
                    base = upper_symbol[: len(upper_symbol) - len(quote)]
                    normalized_symbol = f"{base}/{quote}"
                    break

        ohlcv: list = []
        since = start_ms
        timeframe_ms = _timeframe_milliseconds(timeframe)
        max_limit = 300 if exchange_id == "okx" else 1000

        while since < end_ms:
            remaining = max(1, math.ceil((end_ms - since) / timeframe_ms))
            limit = min(max_limit, remaining)
            batch_until = min(end_ms, since + timeframe_ms * limit)
            params: dict[str, Any] = {}
            if exchange_id == "okx":
                # ccxt.okx otherwise derives "after" from since + timeframe * limit,
                # which can point into the future and make OKX reject the request.
                params["until"] = batch_until
            try:
                batch = exchange.fetch_ohlcv(
                    normalized_symbol, timeframe, since=since, limit=limit, params=params
                )
            except ccxt.BadSymbol:
                raise ValueError(f"Symbol not supported on {exchange_id}: {symbol}")
            except ccxt.RateLimitExceeded:
                raise RuntimeError("RATE_LIMITED")
            except ccxt.NetworkError as e:
                raise RuntimeError(f"Network error fetching OHLCV: {e}")

            if not batch:
                break

            for candle in batch:
                if candle[0] <= end_ms:
                    ohlcv.append(candle)

            last_ts = batch[-1][0]
            if last_ts <= since:
                since += timeframe_ms
            else:
                since = last_ts + timeframe_ms

        if ohlcv:
            _write_cache(cache_key, ohlcv)

    if not ohlcv:
        raise ValueError("NO_DATA")

    df = pd.DataFrame(ohlcv, columns=["timestamp", "Open", "High", "Low", "Close", "Volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    df = df[~df.index.duplicated(keep="first")]
    df.sort_index(inplace=True)

    # Ensure float dtype
    for col in ("Open", "High", "Low", "Close", "Volume"):
        df[col] = df[col].astype(float)

    return df


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _extract_requested_strategy_name(body: dict[str, Any]) -> Optional[str]:
    backtest = _as_dict(body.get("backtest"))
    strategy = _as_dict(backtest.get("strategy"))
    value = strategy.get("strategy_name") or strategy.get("name")
    return str(value).strip() if value else None


def _strategy_semantics(
    body: dict[str, Any],
    executed_strategy_name: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    requested_strategy_name = _extract_requested_strategy_name(body)
    # IMPL §9.4: this provider runs a built-in strategy class (selected by
    # provider_tool_id), not the Cutie strategy draft itself, so strategy_match
    # must be provider_strategy_class_not_verified (surrogate backtest).
    mode = "provider_strategy_class_not_verified"
    warning = (
        f"This provider ran its built-in '{executed_strategy_name}' implementation "
        "with the selected parameters. Cutie did not verify that it fully implements "
        "the current strategy draft rules."
    )
    return (
        {
            "requested_strategy_name": requested_strategy_name,
            "executed_strategy_name": executed_strategy_name,
            "strategy_binding": mode,
        },
        {
            "strategy_match": mode,
            "matches_current_strategy": False,
            "strategy_warning": warning,
        },
        {
            "requested_strategy_name": requested_strategy_name,
            "executed_strategy_name": executed_strategy_name,
            "strategy_match": mode,
        },
    )


def _validation_failure(error_type: str, error_message: str, status_code: int = 200) -> JSONResponse:
    """Early validation failure: provider could not even start a backtest."""
    return JSONResponse(status_code=status_code, content={
        "schema": RESPONSE_SCHEMA,
        "result_status": "failed",
        "provider_name": PROVIDER_NAME,
        "error_type": error_type,
        "error_message": error_message,
    })


def _business_failure(
    run_id: str,
    error_type: str,
    error_message: str,
    reason: Optional[str] = None,
) -> JSONResponse:
    """Business failure with provider metadata (IMPL §6.3)."""
    limitations: dict[str, Any] = {}
    if reason:
        limitations["reason"] = reason
    return JSONResponse(content={
        "schema": RESPONSE_SCHEMA,
        "result_status": "failed",
        "provider_name": PROVIDER_NAME,
        "provider_run_id": f"bt_{run_id}",
        "engine_name": ENGINE_NAME,
        "engine_version": _engine_version(),
        "data_source": DATA_SOURCE,
        "error_type": error_type,
        "error_message": error_message,
        "assumptions": {},
        "limitations": limitations,
        "raw_report": {},
    })


# ---------------------------------------------------------------------------
# Strategy library (multi-tool registry)
#
# Each tool's build() takes the request's provider_params dict, validates it
# (raising ValueError("INVALID_PARAMS:<msg>") on bad input), and returns:
#   {"strategy": StrategyClass, "executed_name": str, "min_bars": int}
# The generic backtest pipeline (fetch OHLCV -> Backtest.run -> metrics) is
# shared across all tools; only the Strategy class + param schema differ.
# ---------------------------------------------------------------------------

def _rsi_series(values: Any, period: int):
    """Wilder-smoothed RSI as a numpy array (NaN warmup filled with neutral 50)."""
    s = pd.Series(values, dtype="float64")
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1.0 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1.0 / period, adjust=False).mean()
    # float64 division: loss==0 & gain>0 -> inf -> RSI 100; 0/0 -> NaN -> filled 50.
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50.0).to_numpy()


def _build_ema_cross(params: dict[str, Any]) -> dict[str, Any]:
    try:
        ema_fast = int(params.get("ema_fast", 20))
        ema_slow = int(params.get("ema_slow", 60))
    except (ValueError, TypeError):
        raise ValueError("INVALID_PARAMS:ema_fast and ema_slow must be integers")
    if ema_fast < 2:
        raise ValueError(f"INVALID_PARAMS:ema_fast must be >= 2 (got {ema_fast})")
    if ema_slow < 3:
        raise ValueError(f"INVALID_PARAMS:ema_slow must be >= 3 (got {ema_slow})")
    if ema_fast >= ema_slow:
        raise ValueError("INVALID_PARAMS:ema_fast must be less than ema_slow")

    from backtesting import Strategy
    from backtesting.lib import crossover

    class EmaCrossStrategy(Strategy):
        _ema_fast = ema_fast
        _ema_slow = ema_slow

        def init(self):
            close = self.data.Close
            self.fast_ema = self.I(
                lambda x: pd.Series(x).ewm(span=self._ema_fast, adjust=False).mean(),
                close,
                name=f"EMA({self._ema_fast})",
            )
            self.slow_ema = self.I(
                lambda x: pd.Series(x).ewm(span=self._ema_slow, adjust=False).mean(),
                close,
                name=f"EMA({self._ema_slow})",
            )

        def next(self):
            if crossover(self.fast_ema, self.slow_ema):
                self.buy()
            elif crossover(self.slow_ema, self.fast_ema):
                self.position.close()

    return {
        "strategy": EmaCrossStrategy,
        "executed_name": f"EMA Cross ({ema_fast}/{ema_slow})",
        "min_bars": max(ema_fast, ema_slow) + 1,
    }


def _build_rsi_reversal(params: dict[str, Any]) -> dict[str, Any]:
    try:
        period = int(params.get("rsi_period", 14))
        oversold = float(params.get("oversold", 30))
        overbought = float(params.get("overbought", 70))
    except (ValueError, TypeError):
        raise ValueError("INVALID_PARAMS:rsi_period/oversold/overbought must be numbers")
    if period < 2:
        raise ValueError(f"INVALID_PARAMS:rsi_period must be >= 2 (got {period})")
    if not (0 < oversold < overbought < 100):
        raise ValueError("INVALID_PARAMS:require 0 < oversold < overbought < 100")

    from backtesting import Strategy

    class RsiReversalStrategy(Strategy):
        _period = period
        _oversold = oversold
        _overbought = overbought

        def init(self):
            self.rsi = self.I(
                lambda x: _rsi_series(x, self._period),
                self.data.Close,
                name=f"RSI({self._period})",
            )

        def next(self):
            if not self.position and self.rsi[-1] < self._oversold:
                self.buy()
            elif self.position and self.rsi[-1] > self._overbought:
                self.position.close()

    return {
        "strategy": RsiReversalStrategy,
        "executed_name": f"RSI Reversal ({period}, {oversold:g}/{overbought:g})",
        # F4: Wilder EWM needs more than period+1 bars to converge; require a real warmup.
        "min_bars": 3 * period + 1,
    }


def _build_bollinger_reversal(params: dict[str, Any]) -> dict[str, Any]:
    try:
        period = int(params.get("bb_period", 20))
        std_mult = float(params.get("bb_std", 2.0))
    except (ValueError, TypeError):
        raise ValueError("INVALID_PARAMS:bb_period/bb_std must be numbers")
    if period < 2:
        raise ValueError(f"INVALID_PARAMS:bb_period must be >= 2 (got {period})")
    if std_mult <= 0:
        raise ValueError("INVALID_PARAMS:bb_std must be > 0")

    from backtesting import Strategy

    def _lower_band(values: Any) -> Any:
        s = pd.Series(values, dtype="float64")
        ma = s.rolling(period).mean()
        sd = s.rolling(period).std(ddof=0)
        return (ma - std_mult * sd).to_numpy()

    class BollingerReversalStrategy(Strategy):
        _period = period

        def init(self):
            close = self.data.Close
            self.mid = self.I(
                lambda x: pd.Series(x, dtype="float64").rolling(self._period).mean().to_numpy(),
                close,
                name=f"BB-mid({self._period})",
            )
            self.lower = self.I(_lower_band, close, name="BB-lower")

        def next(self):
            price = self.data.Close[-1]
            if not self.position and price < self.lower[-1]:
                self.buy()
            elif self.position and price >= self.mid[-1]:
                self.position.close()

    return {
        "strategy": BollingerReversalStrategy,
        "executed_name": f"Bollinger Reversal ({period}, {std_mult:g}sigma)",
        "min_bars": period + 1,
    }


def _build_bollinger_breakout(params: dict[str, Any]) -> dict[str, Any]:
    try:
        period = int(params.get("bb_period", 20))
        std_mult = float(params.get("bb_std", 2.0))
    except (ValueError, TypeError):
        raise ValueError("INVALID_PARAMS:bb_period/bb_std must be numbers")
    if period < 2:
        raise ValueError(f"INVALID_PARAMS:bb_period must be >= 2 (got {period})")
    if std_mult <= 0:
        raise ValueError("INVALID_PARAMS:bb_std must be > 0")

    from backtesting import Strategy

    def _upper_band(values: Any) -> Any:
        s = pd.Series(values, dtype="float64")
        ma = s.rolling(period).mean()
        sd = s.rolling(period).std(ddof=0)
        return (ma + std_mult * sd).to_numpy()

    class BollingerBreakoutStrategy(Strategy):
        _period = period

        def init(self):
            close = self.data.Close
            self.mid = self.I(
                lambda x: pd.Series(x, dtype="float64").rolling(self._period).mean().to_numpy(),
                close,
                name=f"BB-mid({self._period})",
            )
            self.upper = self.I(_upper_band, close, name="BB-upper")

        def next(self):
            price = self.data.Close[-1]
            if not self.position and price > self.upper[-1]:
                self.buy()
            elif self.position and price < self.mid[-1]:
                self.position.close()

    return {
        "strategy": BollingerBreakoutStrategy,
        "executed_name": f"Bollinger Breakout ({period}, {std_mult:g}sigma)",
        "min_bars": period + 1,
    }


def _build_breakout(params: dict[str, Any]) -> dict[str, Any]:
    try:
        lookback = int(params.get("lookback", 20))
        exit_lookback = int(params.get("exit_lookback", 10))
    except (ValueError, TypeError):
        raise ValueError("INVALID_PARAMS:lookback/exit_lookback must be integers")
    if lookback < 2:
        raise ValueError(f"INVALID_PARAMS:lookback must be >= 2 (got {lookback})")
    if exit_lookback < 1:
        raise ValueError(f"INVALID_PARAMS:exit_lookback must be >= 1 (got {exit_lookback})")

    from backtesting import Strategy

    class BreakoutStrategy(Strategy):
        _lb = lookback
        _xlb = exit_lookback

        def init(self):
            # shift(1): the channel uses prior bars only, no look-ahead on the current bar.
            self.hh = self.I(
                lambda x: pd.Series(x, dtype="float64").rolling(self._lb).max().shift(1).to_numpy(),
                self.data.High,
                name=f"Donchian-HH({self._lb})",
            )
            self.ll = self.I(
                lambda x: pd.Series(x, dtype="float64").rolling(self._xlb).min().shift(1).to_numpy(),
                self.data.Low,
                name=f"Donchian-LL({self._xlb})",
            )

        def next(self):
            price = self.data.Close[-1]
            if not self.position and price > self.hh[-1]:
                self.buy()
            elif self.position and price < self.ll[-1]:
                self.position.close()

    return {
        "strategy": BreakoutStrategy,
        "executed_name": f"Donchian Breakout ({lookback}/{exit_lookback})",
        # F1: exit channel uses exit_lookback; min_bars must cover the longer of the two.
        "min_bars": max(lookback, exit_lookback) + 1,
    }


def _build_macd(params: dict[str, Any]) -> dict[str, Any]:
    try:
        fast = int(params.get("fast", 12))
        slow = int(params.get("slow", 26))
        signal_period = int(params.get("signal", 9))
    except (ValueError, TypeError):
        raise ValueError("INVALID_PARAMS:fast/slow/signal must be integers")
    if fast < 2:
        raise ValueError(f"INVALID_PARAMS:fast must be >= 2 (got {fast})")
    if slow <= fast:
        raise ValueError("INVALID_PARAMS:slow must be greater than fast")
    if signal_period < 1:
        raise ValueError(f"INVALID_PARAMS:signal must be >= 1 (got {signal_period})")

    from backtesting import Strategy
    from backtesting.lib import crossover

    def _macd_line(values: Any) -> Any:
        s = pd.Series(values, dtype="float64")
        return s.ewm(span=fast, adjust=False).mean() - s.ewm(span=slow, adjust=False).mean()

    class MacdStrategy(Strategy):
        def init(self):
            close = self.data.Close
            self.macd = self.I(lambda x: _macd_line(x).to_numpy(), close, name="MACD")
            self.signal = self.I(
                lambda x: _macd_line(x).ewm(span=signal_period, adjust=False).mean().to_numpy(),
                close,
                name="Signal",
            )

        def next(self):
            if crossover(self.macd, self.signal):
                self.buy()
            elif crossover(self.signal, self.macd):
                self.position.close()

    return {
        "strategy": MacdStrategy,
        "executed_name": f"MACD ({fast}/{slow}/{signal_period})",
        # F4: EWMA is an infinite-response filter; signal line needs a real warmup.
        "min_bars": slow * 3 + signal_period + 1,
    }


# tool_id -> spec. param_schema_properties drives both the catalog param_schema
# and (via build) the runtime validation. Add new tools here.
TOOL_SPECS: dict[str, dict[str, Any]] = {
    "local.backtesting_py.ema_cross": {
        "name": "Local Backtesting.py EMA Cross",
        "description": (
            "Trend-following EMA crossover (fast EMA crosses slow EMA) on ccxt "
            "public OHLCV; in-process backtesting.py. Suits trending markets."
        ),
        "strategy_family": "trend",
        "is_default": True,
        "build": _build_ema_cross,
        "param_schema_properties": {
            "ema_fast": {"type": "integer", "default": 20, "minimum": 2},
            "ema_slow": {"type": "integer", "default": 60, "minimum": 3},
            "exchange": {"type": "string", "default": DEFAULT_EXCHANGE},
        },
    },
    "local.backtesting_py.rsi_reversal": {
        "name": "Local Backtesting.py RSI Reversal",
        "description": (
            "Mean-reversion (level-based): hold long while RSI is below the oversold "
            "threshold, exit while above overbought. Suits range-bound markets — maps "
            "to KOL '博反弹 / 抄底 / 超卖'. Not for strong trends."
        ),
        "strategy_family": "mean_reversion",
        "is_default": False,
        "build": _build_rsi_reversal,
        "param_schema_properties": {
            "rsi_period": {"type": "integer", "default": 14, "minimum": 2},
            "oversold": {"type": "number", "default": 30, "minimum": 1, "maximum": 49},
            "overbought": {"type": "number", "default": 70, "minimum": 51, "maximum": 99},
            "exchange": {"type": "string", "default": DEFAULT_EXCHANGE},
        },
    },
    "local.backtesting_py.bollinger_reversal": {
        "name": "Local Backtesting.py Bollinger Reversal",
        "description": (
            "Mean-reversion: buy when price closes below the lower Bollinger band, "
            "exit when it returns to the middle band. Suits range-bound markets — "
            "maps to KOL '触下轨回归 / 抄底'. Not for strong trends."
        ),
        "strategy_family": "mean_reversion",
        "is_default": False,
        "build": _build_bollinger_reversal,
        "param_schema_properties": {
            "bb_period": {"type": "integer", "default": 20, "minimum": 2},
            "bb_std": {"type": "number", "default": 2.0, "minimum": 0.1, "maximum": 5},
            "exchange": {"type": "string", "default": DEFAULT_EXCHANGE},
        },
    },
    "local.backtesting_py.bollinger_breakout": {
        "name": "Local Backtesting.py Bollinger Breakout",
        "description": (
            "Breakout: buy when price closes above the upper Bollinger band, exit "
            "when it falls back to the middle band. Suits volatility expansion — "
            "maps to KOL '突破上轨 / 放量突破'."
        ),
        "strategy_family": "breakout",
        "is_default": False,
        "build": _build_bollinger_breakout,
        "param_schema_properties": {
            "bb_period": {"type": "integer", "default": 20, "minimum": 2},
            "bb_std": {"type": "number", "default": 2.0, "minimum": 0.1, "maximum": 5},
            "exchange": {"type": "string", "default": DEFAULT_EXCHANGE},
        },
    },
    "local.backtesting_py.breakout": {
        "name": "Local Backtesting.py Donchian Breakout",
        "description": (
            "Breakout: buy when price breaks above the N-bar high (Donchian "
            "channel), exit when it breaks below the M-bar low. Maps to KOL "
            "'突破关键阻力位'. Channel uses prior bars only (no look-ahead)."
        ),
        "strategy_family": "breakout",
        "is_default": False,
        "build": _build_breakout,
        "param_schema_properties": {
            "lookback": {"type": "integer", "default": 20, "minimum": 2},
            "exit_lookback": {"type": "integer", "default": 10, "minimum": 1},
            "exchange": {"type": "string", "default": DEFAULT_EXCHANGE},
        },
    },
    "local.backtesting_py.macd": {
        "name": "Local Backtesting.py MACD",
        "description": (
            "Trend-following: buy when the MACD line crosses above its signal "
            "line, exit on the opposite cross. Suits trending markets — maps to "
            "KOL '趋势 / 金叉死叉'."
        ),
        "strategy_family": "trend",
        "is_default": False,
        "build": _build_macd,
        "param_schema_properties": {
            "fast": {"type": "integer", "default": 12, "minimum": 2},
            "slow": {"type": "integer", "default": 26, "minimum": 3},
            "signal": {"type": "integer", "default": 9, "minimum": 1},
            "exchange": {"type": "string", "default": DEFAULT_EXCHANGE},
        },
    },
}

DEFAULT_TOOL_ID = "local.backtesting_py.ema_cross"

# F10: connector silently downgrades multiple defaults — enforce exactly one at import.
assert sum(1 for s in TOOL_SPECS.values() if s.get("is_default")) == 1, (
    "exactly one TOOL_SPECS entry must have is_default=True"
)
assert DEFAULT_TOOL_ID in TOOL_SPECS, "DEFAULT_TOOL_ID must be a registered tool"


def _validate_params_against_schema(
    params: dict[str, Any], properties: dict[str, Any]
) -> Optional[str]:
    """F2: enforce the catalog param_schema at runtime (single source of truth).

    additionalProperties:false (reject unknown keys) + type (integer/number/string,
    bool excluded per project governance) + minimum/maximum. Returns an error
    message, or None if valid. Cross-field rules (fast<slow etc.) stay in build().
    """
    for key in params:
        if key not in properties:
            return f"unknown parameter '{key}' (not in tool param_schema)"
    for key, spec in properties.items():
        if key not in params:
            continue
        val = params[key]
        typ = spec.get("type")
        if typ in ("integer", "number"):
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                return f"{key} must be a {typ}"
            if typ == "integer" and not float(val).is_integer():
                return f"{key} must be an integer (got {val})"
            if "minimum" in spec and val < spec["minimum"]:
                return f"{key} must be >= {spec['minimum']} (got {val})"
            if "maximum" in spec and val > spec["maximum"]:
                return f"{key} must be <= {spec['maximum']} (got {val})"
        elif typ == "string":
            if not isinstance(val, str):
                return f"{key} must be a string"
    return None


def _catalog_tool(tool_id: str, spec: dict[str, Any], supported_symbols: list[str]) -> dict[str, Any]:
    """Build one catalog entry from a tool spec; shared fields kept identical across tools."""
    return {
        "tool_id": tool_id,
        "kind": "external_http",
        "name": spec["name"],
        "description": spec["description"],
        "wrapper_type": "python_inprocess",
        "provider_name": PROVIDER_NAME,
        "engine_name": ENGINE_NAME,
        "engine_version": _engine_version(),
        "data_source": {
            "type": "provider_reported",
            "name": DATA_SOURCE,
            "description": (
                "Public OHLCV fetched via ccxt; Cutie does not verify "
                "coverage, gaps, or unclosed candles."
            ),
            "coverage_hint": f"{', '.join(supported_symbols[:5])} 1h/4h/1d from exchange public API",
            "external_unverified": True,
        },
        "supported_symbols": supported_symbols,
        "markets": ["spot"],
        "timeframes": ["1h", "4h", "1d"],
        "is_default": spec.get("is_default", False),
        "execution": {
            "mode": "sync",
            "timeout_ms": EXECUTION_TIMEOUT_MS,
            "max_range_days": EXECUTION_MAX_RANGE_DAYS,
            "max_parallel_runs": 1,
            "async_supported": False,
        },
        "adapter": {
            "requires_manual_export": False,
            "working_dir_policy": "ephemeral_or_provider_managed",
            "result_file_patterns": ["*.html"],
            "upstream_auth_local_only": True,
        },
        "param_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": spec["param_schema_properties"],
        },
        "output_schema": {
            "metrics": ["total_return_pct", "win_rate_pct", "max_drawdown_pct", "trade_count"],
            "artifacts": ["report_url"],
            "series": ["equity_curve"],
            "tables": ["trades"],
        },
        "report_capabilities": {
            "report_url": True,
            "scope": "local_machine_only",
            "formats": ["html"],
            "retention_hint": "last_100_runs",
        },
        "failure_codes": [
            "INVALID_REQUEST",
            "INVALID_PARAMS",
            "TOOL_NOT_FOUND",
            "SYMBOL_UNSUPPORTED",
            "TIMEFRAME_UNSUPPORTED",
            "NO_DATA",
            "INSUFFICIENT_DATA",
            "RATE_LIMITED",
            "ENGINE_ERROR",
        ],
        "security": {
            "network_scope": "openclaw_hermes_local_or_private",
            "requires_user_secret": False,
            "secrets_stay_local": True,
            "live_trading": False,
            "filesystem_paths_exposed": False,
        },
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    """Health check -- no auth required (IMPL §5.2)."""
    checks: dict[str, Any] = {}
    ok = True

    # Check backtesting import
    try:
        import backtesting  # noqa: F401
        checks["backtesting"] = True
    except ImportError:
        checks["backtesting"] = False
        ok = False

    # Check ccxt import and exchange init
    try:
        import ccxt
        exchange_class = getattr(ccxt, DEFAULT_EXCHANGE)
        exchange = exchange_class({"enableRateLimit": True})
        checks["ccxt"] = True
        checks["exchange"] = exchange.id
    except Exception as e:
        checks["ccxt"] = False
        checks["exchange_error"] = str(e)
        ok = False

    # Check pandas import
    try:
        import pandas  # noqa: F401
        checks["pandas"] = True
    except ImportError:
        checks["pandas"] = False
        ok = False

    # Check reports dir writable
    try:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        test_file = REPORTS_DIR / ".write_test"
        test_file.write_text("ok")
        test_file.unlink()
        checks["reports_writable"] = True
    except Exception:
        checks["reports_writable"] = False
        ok = False

    data_ready = checks.get("ccxt", False) and checks.get("backtesting", False)

    if ok:
        return JSONResponse({
            "ok": True,
            "provider_id": PROVIDER_ID,
            "engine_name": ENGINE_NAME,
            "engine_version": _engine_version(),
            "data_ready": data_ready,
            "checked_at": int(time.time()),
        })
    else:
        return JSONResponse({
            "ok": False,
            "error_type": "DEPENDENCY_CHECK_FAILED",
            "error_message": f"Failed checks: {checks}",
        })


@app.get("/catalog")
async def catalog(authorization: Optional[str] = Header(default=None)):
    """Return provider tool catalog (IMPL §5.1 cutie.backtest_provider_catalog.v1)."""
    _verify_bearer(authorization)
    supported_symbols = _supported_symbols()

    return JSONResponse({
        "schema": "cutie.backtest_provider_catalog.v1",
        "provider": {
            "provider_id": PROVIDER_ID,
            "provider_name": PROVIDER_NAME,
            "provider_version": PROVIDER_VERSION,
            "homepage_url": PROVIDER_HOMEPAGE_URL,
            "maintainer": PROVIDER_MAINTAINER,
        },
        "tools": [
            _catalog_tool(tool_id, spec, supported_symbols)
            for tool_id, spec in TOOL_SPECS.items()
        ],
    })


@app.post("/cutie/backtest")
async def run_backtest(request: Request, authorization: Optional[str] = Header(default=None)):
    """Execute backtest (IMPL §5.4 / §5.5)."""
    _verify_bearer(authorization)

    try:
        body = await request.json()
    except Exception:
        return _validation_failure("INVALID_REQUEST", "Request body must be valid JSON", status_code=400)

    bt_req = body.get("backtest", {})
    provider_info = body.get("provider", {})

    run_id = re.sub(r'[^a-zA-Z0-9_\-]', '', str(bt_req.get("run_id", "unknown")))[:64]
    tool_id = bt_req.get("provider_tool_id", "")
    params = bt_req.get("provider_params", {})
    symbol = bt_req.get("symbol", "")
    market = bt_req.get("market", "spot")
    timeframe = bt_req.get("timeframe", "")
    start_at = bt_req.get("start_at")
    end_at = bt_req.get("end_at")
    initial_capital_str = bt_req.get("initial_capital", "10000")
    fee_bps_str = bt_req.get("fee_bps", "10")
    slippage_bps_str = bt_req.get("slippage_bps", "5")

    # --- Validate tool_id ---
    if tool_id and tool_id not in TOOL_SPECS:
        return _validation_failure("TOOL_NOT_FOUND", f"Unknown provider_tool_id: {tool_id}")
    effective_tool_id = tool_id or DEFAULT_TOOL_ID

    # --- Validate symbol ---
    if not symbol:
        return _validation_failure("INVALID_PARAMS", "symbol is required")

    # --- Validate timeframe ---
    supported_timeframes = {"1m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w", "1M"}
    if timeframe not in supported_timeframes:
        return _validation_failure(
            "TIMEFRAME_UNSUPPORTED",
            f"Unsupported timeframe: {timeframe}. Supported: {sorted(supported_timeframes)}",
        )

    # --- Validate date range ---
    if start_at is None or end_at is None:
        return _validation_failure("INVALID_PARAMS", "start_at and end_at are required (unix seconds)")
    try:
        start_at = int(start_at)
        end_at = int(end_at)
    except (TypeError, ValueError):
        return _validation_failure("INVALID_PARAMS", "start_at and end_at must be integers (unix seconds)")
    if start_at >= end_at:
        return _validation_failure("INVALID_PARAMS", "start_at must be before end_at")

    # --- Parse Decimal fields ---
    try:
        initial_capital = Decimal(str(initial_capital_str))
        fee_bps = Decimal(str(fee_bps_str))
        slippage_bps = Decimal(str(slippage_bps_str))
    except (InvalidOperation, TypeError, ValueError) as e:
        return _validation_failure("INVALID_PARAMS", f"Cannot parse decimal fields: {e}")

    if initial_capital <= 0:
        return _validation_failure("INVALID_PARAMS", "initial_capital must be positive")

    # --- Resolve tool + validate params (schema first, then build's business rules) ---
    if params is None:  # F5: provider_params:null must not be treated as a bad type
        params = {}
    if not isinstance(params, dict):  # F5: non-dict -> INVALID_PARAMS, not a 500
        return _validation_failure("INVALID_PARAMS", "provider_params must be an object")
    tool_spec = TOOL_SPECS[effective_tool_id]
    schema_err = _validate_params_against_schema(params, tool_spec["param_schema_properties"])
    if schema_err:  # F2: enforce catalog schema at runtime (unknown key / type / bounds)
        return _validation_failure("INVALID_PARAMS", schema_err)
    raw_exchange = params.get("exchange")  # F7: explicit None handling (str(None) -> "none")
    exchange_id = str(raw_exchange).lower() if raw_exchange else DEFAULT_EXCHANGE
    try:
        built = tool_spec["build"](params)
    except ValueError as e:
        msg = str(e)
        if msg.startswith("INVALID_PARAMS:"):
            return _validation_failure("INVALID_PARAMS", msg[len("INVALID_PARAMS:"):])
        # F8: a ValueError without the INVALID_PARAMS prefix is an internal bug, not user error.
        logger.exception("strategy build failed tool=%s", effective_tool_id)
        return _business_failure(run_id, "ENGINE_ERROR", f"Strategy build failed: {msg}")
    except Exception as e:  # F8: don't let build bugs become bare 500s with lost context
        logger.exception("strategy build failed tool=%s", effective_tool_id)
        return _business_failure(run_id, "ENGINE_ERROR", f"Strategy build failed: {e}")
    min_bars = int(built["min_bars"])
    executed_name = str(built["executed_name"])
    strategy_class = built["strategy"]

    # --- Fetch OHLCV ---
    try:
        df = _fetch_ohlcv(exchange_id, symbol, timeframe, start_at, end_at)
    except ValueError as e:
        error_msg = str(e)
        if error_msg == "NO_DATA":
            return _business_failure(
                run_id,
                "NO_DATA",
                f"No OHLCV data available for {symbol} {timeframe} in requested range",
                reason="data_missing",
            )
        elif "not supported" in error_msg.lower() or "Unsupported" in error_msg:
            return _business_failure(run_id, "SYMBOL_UNSUPPORTED", error_msg, reason="symbol_unsupported")
        else:
            return _business_failure(run_id, "INVALID_PARAMS", error_msg)
    except RuntimeError as e:
        error_msg = str(e)
        if "RATE_LIMITED" in error_msg:
            return _business_failure(
                run_id,
                "RATE_LIMITED",
                "Exchange rate limit exceeded, please retry later",
                reason="rate_limited",
            )
        return _business_failure(run_id, "ENGINE_ERROR", error_msg)
    except Exception as e:
        logger.exception("OHLCV fetch unexpected error")
        return _business_failure(run_id, "ENGINE_ERROR", f"Failed to fetch market data: {e}")

    if len(df) < min_bars:
        return _business_failure(
            run_id,
            "INSUFFICIENT_DATA",
            (
                f"Insufficient data: got {len(df)} candles, "
                f"need at least {min_bars} for {executed_name}"
            ),
            reason="insufficient_data",
        )

    # --- Run backtest ---
    try:
        from backtesting import Backtest

        # Commission: fee_bps / 10000 (basis points to ratio)
        commission = float(fee_bps / Decimal("10000"))

        StrategyClass = strategy_class
        # backtesting.py trades WHOLE units; a small cash on a high-priced asset
        # (e.g. BTC ~$80k with $10k cash) floors position size to 0 units -> no trades.
        # Run with a large internal cash so sizing is effectively continuous, then scale
        # equity/PnL back to the user's capital. Percentage metrics are cash-invariant.
        user_capital = float(initial_capital)
        internal_cash = max(user_capital, float(df["Close"].max()) * 100_000.0)
        equity_scale = user_capital / internal_cash
        bt = Backtest(
            df,
            StrategyClass,
            cash=internal_cash,
            commission=commission,
            exclusive_orders=True,
            # Settle trades still open at the end (close at last bar) so metrics /
            # trade_count reflect them instead of silently dropping unrealized PnL.
            finalize_trades=True,
        )
        stats = bt.run()

        # Generate HTML report
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        report_filename = f"{run_id}.html"
        report_path = REPORTS_DIR / report_filename
        bt.plot(filename=str(report_path), open_browser=False)
        _enforce_reports_retention()

    except Exception as e:
        logger.exception("Backtest execution failed")
        return _business_failure(run_id, "ENGINE_ERROR", f"Backtest execution failed: {e}")

    # --- Build result (W3.10: entire post-processing wrapped in try-except) ---
    try:
        equity_curve: list[dict] = []
        if hasattr(stats, "_equity_curve") and stats._equity_curve is not None:
            eq = stats._equity_curve
            for idx, row in eq.iterrows():
                ts = int(idx.timestamp()) if hasattr(idx, "timestamp") else 0
                raw_equity = row.get("Equity", None)
                if raw_equity is None:
                    raw_equity = row.iloc[0] if len(row) > 0 else internal_cash
                equity_curve.append({
                    "t": ts,
                    "equity": _decimal_str(float(raw_equity) * equity_scale, places=2),
                })
        else:
            start_ts = int(df.index[0].timestamp()) if hasattr(df.index[0], "timestamp") else start_at
            end_ts = int(df.index[-1].timestamp()) if hasattr(df.index[-1], "timestamp") else end_at
            final_equity = _safe_float(stats, "Equity Final [$]", internal_cash)
            equity_curve = [
                {"t": start_ts, "equity": _decimal_str(initial_capital, places=2)},
                {"t": end_ts, "equity": _decimal_str(final_equity * equity_scale, places=2)},
            ]

        if len(equity_curve) > 500:
            step = len(equity_curve) // 500
            equity_curve = equity_curve[::step]
            last_eq = stats._equity_curve
            if last_eq is not None and len(last_eq) > 0:
                last_idx = last_eq.index[-1]
                last_ts = int(last_idx.timestamp()) if hasattr(last_idx, "timestamp") else end_at
                raw_last = last_eq.iloc[-1].get("Equity", None)
                if raw_last is None:
                    raw_last = last_eq.iloc[-1].iloc[0] if len(last_eq.iloc[-1]) > 0 else internal_cash
                equity_curve.append({
                    "t": last_ts,
                    "equity": _decimal_str(float(raw_last) * equity_scale, places=2),
                })

        trades_list: list[dict] = []
        if hasattr(stats, "_trades") and stats._trades is not None:
            for _, trade in stats._trades.iterrows():
                entry_time = trade.get("EntryTime")
                exit_time = trade.get("ExitTime")
                entry_ts = int(entry_time.timestamp()) if hasattr(entry_time, "timestamp") else 0
                exit_ts = int(exit_time.timestamp()) if hasattr(exit_time, "timestamp") else 0
                size = trade.get("Size", 0) or 0
                side = "long" if size > 0 else "short"
                trades_list.append({
                    "side": side,
                    "entry_at": entry_ts,
                    "exit_at": exit_ts,
                    "pnl": _decimal_str(float(trade.get("PnL", 0) or 0) * equity_scale, places=2),
                })

        total_return_pct = _safe_float(stats, "Return [%]", 0.0)
        win_rate_pct = _safe_float(stats, "Win Rate [%]", 0.0)
        max_drawdown_pct = abs(_safe_float(stats, "Max. Drawdown [%]", 0.0))
        trade_count = _safe_int(stats, "# Trades", 0)

        metrics = {
            "total_return_pct": round(total_return_pct, 2),
            "win_rate_pct": round(win_rate_pct, 2),
            "max_drawdown_pct": round(max_drawdown_pct, 2),
            "trade_count": trade_count,
        }

        metrics_json = json.dumps(metrics, sort_keys=True)
        result_hash = f"sha256:{hashlib.sha256(metrics_json.encode()).hexdigest()}"

        candle_count = len(df)
        if candle_count < 100:
            sample_size = "small"
        elif candle_count < 1000:
            sample_size = "medium"
        else:
            sample_size = "large"

        report_url = f"reports/{report_filename}"

        provider_summary = (
            f"{executed_name} on {symbol} {timeframe}, "
            f"{exchange_id} public OHLCV, {candle_count} candles, "
            f"{trade_count} trades, return {total_return_pct:.2f}%"
        )
        strategy_assumptions, strategy_limitations, strategy_raw_report = _strategy_semantics(
            body,
            executed_name,
        )

        return JSONResponse(content=_json_safe({
            "schema": RESPONSE_SCHEMA,
            "result_status": "success",
            "provider_name": PROVIDER_NAME,
            "provider_run_id": f"bt_{run_id}",
            "engine_name": ENGINE_NAME,
            "engine_version": _engine_version(),
            "data_source": DATA_SOURCE,
            "result_hash": result_hash,
            "report_url": report_url,
            "report_url_scope": "local_machine_only",
            "metrics": metrics,
            "initial_capital": _decimal_str(initial_capital, places=2),
            "equity_curve": equity_curve,
            "trades": trades_list,
            "assumptions": {
                "fee_bps": _decimal_str(fee_bps, places=4),
                "slippage_bps": _decimal_str(slippage_bps, places=4),
                "exchange": exchange_id,
                **strategy_assumptions,
                "real_market_data": True,
                "no_live_trading": True,
            },
            "limitations": {
                "verification": "external_unverified",
                "verified_by_cutie": False,
                **strategy_limitations,
                "sample_size": sample_size,
                "data_quality": "provider_reported",
                # F9: 0 settled trades -> metrics are zero by default; flag so the
                # caller can tell "ran but never traded" from "real 0% return".
                "no_trades_executed": trade_count == 0,
            },
            "raw_report": {
                "provider_summary": provider_summary,
                "strategy_semantics": strategy_raw_report,
            },
        }))

    except Exception as e:
        logger.exception("Result post-processing failed")
        return _business_failure(run_id, "ENGINE_ERROR", f"Result processing failed: {e}")


# ---------------------------------------------------------------------------
# Static file serving for HTML reports
# ---------------------------------------------------------------------------

@app.get("/reports/{filename}")
async def serve_report(filename: str):
    """Serve generated HTML report files."""
    # Sanitize filename to prevent path traversal
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    file_path = REPORTS_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Report not found")

    return FileResponse(str(file_path), media_type="text/html")


# ---------------------------------------------------------------------------
# Global exception handler -- all responses must be JSON
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception")
    return JSONResponse(
        status_code=500,
        content={
            "schema": RESPONSE_SCHEMA,
            "result_status": "failed",
            "provider_name": PROVIDER_NAME,
            "error_type": "ENGINE_ERROR",
            "error_message": str(exc),
        },
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    # 401/403 from Bearer auth -> AUTH_FAILED; other HTTP errors -> INVALID_REQUEST.
    error_type = "AUTH_FAILED" if exc.status_code in (401, 403) else "INVALID_REQUEST"
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "schema": RESPONSE_SCHEMA,
            "result_status": "failed",
            "provider_name": PROVIDER_NAME,
            "error_type": error_type,
            "error_message": exc.detail,
        },
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "cutie_backtesting_provider:app",
        host="127.0.0.1",
        port=PORT,
        log_level="info",
    )
