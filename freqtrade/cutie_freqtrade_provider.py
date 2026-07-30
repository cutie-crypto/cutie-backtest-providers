"""
Cutie Freqtrade Backtest Provider

Local HTTP sidecar wrapping Freqtrade CLI backtesting.
Exposes /health, /catalog, /cutie/backtest per IMPL W3.8 contract.

Usage:
    CUTIE_BACKTEST_PROVIDER_TOKEN="local-dev-token" \
    uvicorn cutie_freqtrade_provider:app --host 127.0.0.1 --port 8766

Environment variables:
    CUTIE_BACKTEST_PROVIDER_TOKEN  - Bearer token for /catalog and /cutie/backtest
    CUTIE_BACKTEST_PORT            - Port (default 8766, only used with __main__)
    FREQTRADE_USERDIR              - Freqtrade user directory (default ./user_data)
    FREQTRADE_CMD                  - Freqtrade command (default "freqtrade")
    BACKTEST_TIMEOUT               - Subprocess timeout in seconds (default 300)
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROVIDER_TOKEN = os.environ.get("CUTIE_BACKTEST_PROVIDER_TOKEN", "")
FREQTRADE_USERDIR = Path(os.environ.get("FREQTRADE_USERDIR", "./user_data"))
FREQTRADE_CMD = os.environ.get("FREQTRADE_CMD", "freqtrade")
BACKTEST_TIMEOUT = int(os.environ.get("BACKTEST_TIMEOUT", "300"))
REPORTS_DIR = Path(os.environ.get("REPORTS_DIR", "./reports"))
MAX_REPORTS = 100

PROVIDER_ID = "local-freqtrade"
PROVIDER_NAME = "Freqtrade Local"
PROVIDER_VERSION = "1.0.0"
PROVIDER_HOMEPAGE_URL = "https://www.freqtrade.io/"
PROVIDER_MAINTAINER = "cutie-backtest-providers"
ENGINE_NAME = "Freqtrade"
DATA_SOURCE = "freqtrade_data"
RESPONSE_SCHEMA = "cutie.external_backtest.response.v1"
DEFAULT_PORT = 8766
DEFAULT_EXCHANGE = os.environ.get("CUTIE_FREQTRADE_DEFAULT_EXCHANGE", "okx").lower()
EXECUTION_TIMEOUT_MS = BACKTEST_TIMEOUT * 1000
EXECUTION_MAX_RANGE_DAYS = 365

SUPPORTED_TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
TIMEFRAME_SECONDS = {
    "1m": 60,
    "5m": 5 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
    "1d": 24 * 60 * 60,
}
ERROR_OUTPUT_LIMIT = 1000

logger = logging.getLogger("cutie_freqtrade_provider")

app = FastAPI(title="Cutie Freqtrade Provider", version="1.0.0")


@app.on_event("startup")
async def startup_warning():
    if not PROVIDER_TOKEN:
        logger.warning("CUTIE_BACKTEST_PROVIDER_TOKEN not set — running without authentication (dev mode)")


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------

def _check_auth(authorization: Optional[str]) -> None:
    """Validate Bearer token. Raises 401 if invalid."""
    if not PROVIDER_TOKEN:
        # No token configured -- accept anything (dev mode)
        return
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header missing")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or parts[1] != PROVIDER_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_engine_version() -> Optional[str]:
    """Get Freqtrade version string, or None if unavailable."""
    cmd_path = shutil.which(FREQTRADE_CMD)
    if not cmd_path:
        return None
    try:
        result = subprocess.run(
            [FREQTRADE_CMD, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        # Freqtrade outputs something like "freqtrade 2024.1"
        output = result.stdout.strip() or result.stderr.strip()
        # Extract version number
        match = re.search(r"[\d]+[\d.]+\S*", output)
        return match.group(0) if match else output
    except Exception:
        return None


def _symbol_to_pair(symbol: str) -> str:
    """Convert Cutie symbol (BTCUSDT) to Freqtrade pair (BTC/USDT).

    Handles common quote currencies: USDT, USDC, BUSD, BTC, ETH, BNB.
    If already in pair format (contains '/'), returns as-is uppercased.
    """
    s = symbol.upper().strip()
    if "/" in s:
        return s  # Already in pair format
    for quote in ("USDT", "USDC", "BUSD", "TUSD", "BTC", "ETH", "BNB"):
        if s.endswith(quote) and len(s) > len(quote):
            base = s[: -len(quote)]
            return f"{base}/{quote}"
    # Fallback: assume last 4 chars are quote
    if len(s) > 4:
        return f"{s[:-4]}/{s[-4:]}"
    return s


def _ts_to_timerange_str(start_ts: int, end_ts: int) -> str:
    """Convert unix timestamps to Freqtrade timerange format YYYYMMDD-YYYYMMDD."""
    start_dt = datetime.fromtimestamp(start_ts, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(end_ts, tz=timezone.utc)
    return f"{start_dt.strftime('%Y%m%d')}-{end_dt.strftime('%Y%m%d')}"


def _camel_to_snake(name: str) -> str:
    """Convert CamelCase to snake_case for tool_id generation.

    SampleStrategy -> sample_strategy
    MyEMACross -> my_ema_cross
    """
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


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
    """Describe whether the local Freqtrade class is verified against Cutie's draft.

    The provider can run a local strategy class, but it cannot prove that the
    class implements the natural-language/spec rules in the Cutie draft. Keep
    this explicit so sample/reference runs are not presented as full strategy
    verification.
    """
    requested_strategy_name = _extract_requested_strategy_name(body)
    is_sample_strategy = executed_strategy_name.lower().startswith("sample")
    mode = "sample_reference_only" if is_sample_strategy else "provider_strategy_class_not_verified"

    return (
        {
            "requested_strategy_name": requested_strategy_name,
            "executed_strategy_name": executed_strategy_name,
            "strategy_binding": mode,
        },
        {
            "strategy_match": mode,
            "matches_current_strategy": False,
            "strategy_warning": (
                "This provider ran a sample/reference Freqtrade strategy, not a verified "
                "implementation of the current Cutie strategy."
                if is_sample_strategy
                else
                "Cutie did not verify that the selected local Freqtrade class fully "
                "implements the current Cutie strategy rules."
            ),
        },
        {
            "requested_strategy_name": requested_strategy_name,
            "executed_strategy_name": executed_strategy_name,
            "strategy_match": mode,
        },
    )


def _list_strategies() -> list[str]:
    """List available Freqtrade strategy class names in userdir/strategies/."""
    strategies_dir = FREQTRADE_USERDIR / "strategies"
    if not strategies_dir.is_dir():
        return []
    result = set()
    for f in strategies_dir.iterdir():
        if f.suffix == ".py" and f.stem != "__init__" and not f.stem.startswith("_"):
            try:
                content = f.read_text()
            except OSError:
                content = ""
            classes = re.findall(
                r"class\s+([A-Za-z_][A-Za-z0-9_]*)\s*\([^)]*IStrategy[^)]*\)",
                content,
            )
            result.update(classes or [f.stem])
    return sorted(result)


def _check_data_directory() -> tuple[bool, list[str]]:
    """Check if data directory has any pair data files.

    Returns (has_data, list_of_pairs_found).
    """
    data_dir = FREQTRADE_USERDIR / "data"
    if not data_dir.is_dir():
        return False, []

    pairs = set()
    # Freqtrade stores data in user_data/data/<exchange>/<pair>-<timeframe>.json
    # or in feather/hdf5 format
    for exchange_dir in data_dir.iterdir():
        if not exchange_dir.is_dir():
            continue
        for data_file in exchange_dir.iterdir():
            if data_file.suffix in (".json", ".feather", ".h5", ".gz", ".parquet"):
                # Extract pair from filename like "BTC_USDT-1h.json"
                name = data_file.stem
                if "-" in name:
                    pair_part = name.rsplit("-", 1)[0]
                    pairs.add(pair_part)
    return len(pairs) > 0, sorted(pairs)


def _ohlcv_file_format(path: Path) -> Optional[str]:
    name = path.name.lower()
    if name.endswith(".json.gz"):
        return "jsongz"
    if name.endswith(".json"):
        return "json"
    if name.endswith(".feather"):
        return "feather"
    if name.endswith(".parquet"):
        return "parquet"
    if name.endswith(".h5"):
        return "hdf5"
    return None


def _find_local_ohlcv_files(exchange: str, pair: str, timeframe: str) -> list[Path]:
    """Return exact spot OHLCV files without trusting exchange as a path."""
    data_dir = FREQTRADE_USERDIR / "data"
    if not data_dir.is_dir():
        return []

    exchange_dir = next(
        (
            item
            for item in data_dir.iterdir()
            if item.is_dir() and item.name.lower() == str(exchange).strip().lower()
        ),
        None,
    )
    if exchange_dir is None:
        return []

    expected_prefix = f"{pair.replace('/', '_')}-{timeframe}.".lower()
    return sorted(
        item
        for item in exchange_dir.iterdir()
        if item.is_file()
        and item.name.lower().startswith(expected_prefix)
        and _ohlcv_file_format(item) is not None
    )


def _timestamp_to_seconds(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(number):
            return None
        magnitude = abs(number)
        if magnitude >= 1e17:  # nanoseconds
            number /= 1_000_000_000
        elif magnitude >= 1e14:  # microseconds
            number /= 1_000_000
        elif magnitude >= 1e11:  # milliseconds
            number /= 1000
        return int(number)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp())

    text = str(value).strip()
    if not text:
        return None
    try:
        return _timestamp_to_seconds(Decimal(text))
    except InvalidOperation:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _row_timestamp(row: Any) -> Optional[int]:
    if isinstance(row, (list, tuple)) and row:
        return _timestamp_to_seconds(row[0])
    if isinstance(row, dict):
        for key in ("date", "timestamp", "open_time"):
            if key in row:
                return _timestamp_to_seconds(row[key])
    return None


@lru_cache(maxsize=128)
def _read_ohlcv_file_bounds_cached(
    path_text: str,
    _mtime_ns: int,
    _size: int,
) -> Optional[tuple[int, int]]:
    """Read first/last candle timestamps from a supported Freqtrade data file."""
    path = Path(path_text)
    data_format = _ohlcv_file_format(path)
    if data_format in {"json", "jsongz"}:
        opener = gzip.open if data_format == "jsongz" else open
        with opener(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        rows = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(rows, list) or not rows:
            return None
        start_at = _row_timestamp(rows[0])
        end_at = _row_timestamp(rows[-1])
    else:
        # Freqtrade itself depends on pandas and the selected storage backend.
        # Import lazily so a missing optional legacy reader does not make the
        # provider fail at startup; an unreadable file falls back to the CLI's
        # own diagnostic instead of being misclassified as out-of-coverage.
        import pandas as pd

        if data_format == "feather":
            frame = pd.read_feather(path)
        elif data_format == "parquet":
            frame = pd.read_parquet(path)
        elif data_format == "hdf5":
            frame = pd.read_hdf(path)
        else:
            return None
        if frame.empty:
            return None
        series = None
        for key in ("date", "timestamp", "open_time"):
            if key in frame.columns:
                series = frame[key]
                break
        if series is None:
            series = frame.index
        if hasattr(series, "iloc"):
            first_value = series.iloc[0]
            last_value = series.iloc[-1]
        else:
            first_value = series[0]
            last_value = series[-1]
        start_at = _timestamp_to_seconds(first_value)
        end_at = _timestamp_to_seconds(last_value)

    if start_at is None or end_at is None:
        return None
    if end_at < start_at:
        start_at, end_at = end_at, start_at
    return start_at, end_at


def _read_ohlcv_file_bounds(path: Path) -> Optional[tuple[int, int]]:
    stat = path.stat()
    return _read_ohlcv_file_bounds_cached(str(path), stat.st_mtime_ns, stat.st_size)


def _inspect_local_data_coverage(
    exchange: str,
    pair: str,
    timeframe: str,
    requested_start_at: int,
    requested_end_at: int,
) -> dict[str, Any]:
    files = _find_local_ohlcv_files(exchange, pair, timeframe)
    if not files:
        return {
            "status": "missing",
            "requested_start_at": requested_start_at,
            "requested_end_at": requested_end_at,
        }

    readable: list[dict[str, Any]] = []
    unreadable = False
    for path in files:
        try:
            bounds = _read_ohlcv_file_bounds(path)
        except Exception as exc:
            unreadable = True
            logger.warning("Unable to inspect Freqtrade OHLCV coverage for %s: %s", path.name, exc)
            continue
        if bounds is None:
            unreadable = True
            continue
        first_open_at, last_open_at = bounds
        actual_end_at = last_open_at + TIMEFRAME_SECONDS[timeframe]
        candidate = {
            "status": "covered",
            "requested_start_at": requested_start_at,
            "requested_end_at": requested_end_at,
            "actual_start_at": first_open_at,
            "actual_last_open_at": last_open_at,
            "actual_end_at": actual_end_at,
            "data_format": _ohlcv_file_format(path),
        }
        if first_open_at <= requested_start_at and actual_end_at >= requested_end_at:
            return candidate
        candidate["status"] = "outside_coverage"
        readable.append(candidate)

    # Do not pre-empt Freqtrade when an exact matching file exists but this
    # provider process lacks its optional reader. The CLI remains authoritative
    # and its now-tail-preserved error will be returned if it cannot use it.
    if unreadable:
        return {
            "status": "unreadable",
            "requested_start_at": requested_start_at,
            "requested_end_at": requested_end_at,
        }
    if not readable:
        return {
            "status": "unreadable",
            "requested_start_at": requested_start_at,
            "requested_end_at": requested_end_at,
        }

    # Report the candidate with the greatest overlap, then the newest end. This
    # is display provenance only; every readable candidate failed the hard gate.
    def score(item: dict[str, Any]) -> tuple[int, int]:
        overlap = max(
            0,
            min(item["actual_end_at"], requested_end_at)
            - max(item["actual_start_at"], requested_start_at),
        )
        return overlap, item["actual_end_at"]

    return max(readable, key=score)


def _utc_date(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")


def _utc_minute(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _download_data_command(
    exchange: str,
    pair: str,
    timeframe: str,
    start_at: int,
    end_at: int,
    *,
    prepend: bool,
) -> str:
    timerange = _ts_to_timerange_str(start_at, end_at)
    prepend_arg = " --prepend" if prepend else ""
    return (
        f"freqtrade download-data --exchange {exchange} --pairs {pair} "
        f"--timeframes {timeframe} --timerange {timerange}{prepend_arg}"
    )


def _diagnostic_tail(value: str, limit: int = ERROR_OUTPUT_LIMIT) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"... (truncated; showing last {limit} characters)\n{text[-limit:]}"


def _pair_file_to_symbol(pair_file_name: str) -> str:
    """Convert Freqtrade file pair name BTC_USDT to Cutie symbol BTCUSDT."""
    return pair_file_name.replace("_", "").replace("/", "").upper()


def _cleanup_reports() -> None:
    """Remove oldest reports if over MAX_REPORTS."""
    if not REPORTS_DIR.is_dir():
        return
    reports = sorted(REPORTS_DIR.iterdir(), key=lambda p: p.stat().st_mtime)
    while len(reports) > MAX_REPORTS:
        oldest = reports.pop(0)
        try:
            oldest.unlink()
        except OSError as e:
            logger.warning("Failed to delete old report %s: %s", oldest, e)


def _parse_decimal(value: Any, field_name: str) -> Decimal:
    """Parse a value into Decimal. Raises ValueError with context."""
    if value is None:
        raise ValueError(f"{field_name} is required")
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError) as e:
        raise ValueError(f"Invalid decimal value for {field_name}: {value}") from e


def _compute_hash(data: dict) -> str:
    """Compute sha256 hash of JSON-serialized result data."""
    raw = json.dumps(data, sort_keys=True, default=str).encode("utf-8")
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _decimal_str(value: Any, places: int = 8) -> str:
    """Render a money/quantity value as a decimal string (IMPL §6.2).

    Money/quantity fields (equity, pnl, capital, fee) must be serialized as
    decimal strings, never JSON floats. Non-finite or unparseable values
    fall back to "0".
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


def _safe_float(data: Any, key: str, default: float = 0.0) -> float:
    """Extract a float metric from a dict, with None/NaN protection.

    dict.get(key, default) returns the stored value (even if explicitly
    None) when the key exists -- the default is only used for missing keys.
    """
    raw = data.get(key, default) if hasattr(data, "get") else default
    if raw is None:
        return default
    try:
        val = float(raw)
        return val if math.isfinite(val) else default
    except (TypeError, ValueError):
        return default


def _safe_int(data: Any, key: str, default: int = 0) -> int:
    raw = data.get(key, default) if hasattr(data, "get") else default
    if raw is None:
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_decimal(value: Any, default: str = "0") -> Decimal:
    """Convert a JSON-sourced value to Decimal, defaulting on None/NaN/invalid."""
    if value is None:
        return Decimal(default)
    if isinstance(value, float) and not math.isfinite(value):
        return Decimal(default)
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)
    # Decimal("NaN") / "Infinity" 字符串能成功构造非有限 Decimal，会污染后续
    # cumulative 累加且无法恢复（2026-07-03 Codex review P2）
    if not dec.is_finite():
        return Decimal(default)
    return dec


# ---------------------------------------------------------------------------
# Freqtrade result parsing
# ---------------------------------------------------------------------------

def _find_latest_backtest_result(results_dir: Path, export_filename: Optional[str] = None) -> Optional[Path]:
    """Find the latest backtest result JSON file.

    Freqtrade writes results to user_data/backtest_results/ with names like
    backtest-result-<timestamp>.json or the export_filename if specified.
    """
    if export_filename:
        candidate = Path(export_filename)
        if candidate.exists():
            return candidate
        # Also try with .json extension
        candidate_json = candidate.with_suffix(".json")
        if candidate_json.exists():
            return candidate_json
        # Try within results_dir
        candidate_in_dir = results_dir / candidate.name
        if candidate_in_dir.exists():
            return candidate_in_dir

    if not results_dir.is_dir():
        return None

    json_files = [
        f for f in results_dir.iterdir()
        if f.suffix in (".json", ".zip") and f.stem.startswith("backtest-result")
    ]
    if not json_files:
        return None
    return max(json_files, key=lambda p: p.stat().st_mtime)


def _parse_freqtrade_result(result_path: Path, pair: str) -> dict:
    """Parse Freqtrade backtesting result JSON into Cutie-compatible format.

    Freqtrade result JSON structure (simplified):
    {
      "strategy": {
        "<StrategyName>": {
          "trades": [...],
          "results_per_pair": [...],
          "total_trades": N,
          "profit_total": 0.05,
          "profit_total_abs": 500.0,
          "profit_factor": 1.5,
          "max_drawdown": 0.03,
          "max_drawdown_abs": 300.0,
          "winning_trades": N,
          "losing_trades": N,
          "backtest_start": "2024-01-01 00:00:00",
          "backtest_end": "2024-06-01 00:00:00",
          ...
        }
      },
      "strategy_comparison": [...],
      ...
    }
    """
    if result_path.suffix == ".zip":
        with zipfile.ZipFile(result_path) as archive:
            result_members = [
                name for name in archive.namelist()
                if name.endswith(".json") and "_config" not in name
            ]
            if not result_members:
                raise ValueError("No result JSON found in Freqtrade zip output")
            raw = json.loads(archive.read(result_members[0]))
    else:
        with open(result_path, "r") as f:
            raw = json.load(f)

    # Navigate to strategy results - take first strategy
    strategy_data = raw.get("strategy", {})
    if not strategy_data:
        raise ValueError("No strategy results found in Freqtrade output")

    strategy_name = list(strategy_data.keys())[0]
    strat_result = strategy_data[strategy_name]

    # Extract metrics (strat_result values may be explicit JSON null -> .get()
    # returns None even with a default, so use the None/NaN-safe helpers)
    total_trades = _safe_int(strat_result, "total_trades", 0)
    winning_trades = _safe_int(strat_result, "winning_trades", 0)
    profit_total = _safe_float(strat_result, "profit_total", 0.0)  # as ratio (0.05 = 5%)
    profit_total_abs = _safe_float(strat_result, "profit_total_abs", 0.0)
    max_drawdown = _safe_float(strat_result, "max_drawdown", 0.0)  # as ratio
    max_drawdown_abs = _safe_float(strat_result, "max_drawdown_abs", 0.0)

    win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0

    metrics = {
        "total_return_pct": round(profit_total * 100, 4),
        "win_rate_pct": round(win_rate, 2),
        "max_drawdown_pct": round(abs(max_drawdown) * 100, 4),
        "trade_count": total_trades,
    }

    # Extract trades
    trades = []
    raw_trades = strat_result.get("trades", [])
    for t in raw_trades:
        trade_pair = t.get("pair", "")
        # Normalize pair comparison: "BTC/USDT" matches our requested pair
        if pair and trade_pair.replace("/", "").replace("_", "") != pair.replace("/", "").replace("_", ""):
            continue
        trade_entry: dict[str, Any] = {
            "side": "long" if not t.get("is_short", False) else "short",
            "pnl": _decimal_str(t.get("profit_abs", 0), places=8),
        }
        # Parse timestamps
        open_date = t.get("open_date")
        close_date = t.get("close_date")
        if open_date:
            entry_at = _parse_trade_timestamp(open_date)
            if entry_at is not None:
                trade_entry["entry_at"] = entry_at
        if close_date:
            exit_at = _parse_trade_timestamp(close_date)
            if exit_at is not None:
                trade_entry["exit_at"] = exit_at
        trades.append(trade_entry)

    # Build equity curve from trades (simplified)
    equity_curve = _build_equity_curve(strat_result, raw_trades)

    return {
        "metrics": metrics,
        "trades": trades,
        "equity_curve": equity_curve,
        "strategy_name": strategy_name,
        "raw_summary": {
            "strategy_name": strategy_name,
            "total_trades": total_trades,
            "winning_trades": winning_trades,
            "losing_trades": strat_result.get("losing_trades", 0),
            "profit_total": profit_total,
            "profit_total_abs": profit_total_abs,
            "max_drawdown": max_drawdown,
            "max_drawdown_abs": max_drawdown_abs,
            "profit_factor": strat_result.get("profit_factor", 0),
            "backtest_start": strat_result.get("backtest_start", ""),
            "backtest_end": strat_result.get("backtest_end", ""),
        },
    }


def _parse_trade_timestamp(ts_str) -> Optional[int]:
    """Parse Freqtrade trade timestamp to unix seconds.

    Freqtrade uses formats like "2024-01-15 14:00:00+00:00" or epoch ms.
    Returns None if unparseable (instead of 0 which displays as 1970-01-01).
    """
    if isinstance(ts_str, (int, float)):
        # Could be epoch seconds or milliseconds
        if ts_str > 1e12:
            return int(ts_str / 1000)
        return int(ts_str)
    try:
        # Try ISO format with timezone
        dt = datetime.fromisoformat(str(ts_str))
        return int(dt.timestamp())
    except ValueError:
        pass
    try:
        # Try without timezone (assume UTC)
        dt = datetime.strptime(str(ts_str), "%Y-%m-%d %H:%M:%S")
        dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        logger.warning("Unparseable trade timestamp: %s", ts_str)
        return None


def _build_equity_curve(strat_result: dict, trades: list[dict]) -> list[dict]:
    """Build a simplified equity curve from trade data.

    Uses trade close timestamps and cumulative P&L.
    A production implementation might use Freqtrade's daily stats if available.
    """
    curve: list[dict] = []

    # Try to use backtest daily stats if available
    daily_stats = strat_result.get("daily_profit", [])
    if daily_stats:
        # daily_profit is list of [date_str, abs_profit, cumulative_profit]
        # or dict entries depending on version
        cumulative = Decimal("0")
        for entry in daily_stats:
            if isinstance(entry, list) and len(entry) >= 2:
                date_str = entry[0]
                daily_pnl = _safe_decimal(entry[1])
                cumulative += daily_pnl
                try:
                    dt = datetime.strptime(str(date_str), "%Y-%m-%d")
                    dt = dt.replace(tzinfo=timezone.utc)
                    curve.append({
                        "t": int(dt.timestamp()),
                        "equity": _decimal_str(cumulative, places=8),
                    })
                except ValueError:
                    continue
        if curve:
            return curve

    # Fallback: build from individual trades
    if not trades:
        return curve

    sorted_trades = sorted(
        [t for t in trades if t.get("close_date") and _parse_trade_timestamp(t["close_date"]) is not None],
        key=lambda t: _parse_trade_timestamp(t["close_date"]),  # type: ignore[arg-type]
    )
    cumulative_pnl = Decimal("0")
    for t in sorted_trades:
        close_ts = _parse_trade_timestamp(t["close_date"])
        if close_ts is None:
            continue
        pnl = _safe_decimal(t.get("profit_abs", 0))
        cumulative_pnl += pnl
        curve.append({
            "t": close_ts,
            "equity": _decimal_str(cumulative_pnl, places=8),
        })
    return curve


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    """Health check - no auth required.

    Checks:
    - freqtrade command is available
    - userdir exists
    - data directory has at least one pair
    - strategies directory has at least one strategy
    """
    checked_at = int(time.time())

    # Check freqtrade binary
    ft_path = shutil.which(FREQTRADE_CMD)
    if not ft_path:
        return JSONResponse(
            status_code=200,
            content={
                "ok": False,
                "error_type": "DEPENDENCY_MISSING",
                "error_message": (
                    f"'{FREQTRADE_CMD}' command not found in PATH. "
                    "Install Freqtrade: pip install freqtrade"
                ),
                "checked_at": checked_at,
            },
        )

    # Get version
    engine_version = _get_engine_version() or "unknown"

    # Check userdir
    if not FREQTRADE_USERDIR.is_dir():
        return JSONResponse(
            status_code=200,
            content={
                "ok": False,
                "error_type": "USERDIR_MISSING",
                "error_message": (
                    f"Freqtrade user directory not found: {FREQTRADE_USERDIR}. "
                    f"Run: freqtrade create-userdir --userdir {FREQTRADE_USERDIR}"
                ),
                "checked_at": checked_at,
            },
        )

    # Check data
    has_data, pairs = _check_data_directory()

    # Check strategies
    strategies = _list_strategies()

    if not strategies:
        return JSONResponse(
            status_code=200,
            content={
                "ok": False,
                "error_type": "NO_STRATEGIES",
                "error_message": (
                    f"No strategy files found in {FREQTRADE_USERDIR}/strategies/. "
                    "Copy a strategy .py file there (e.g. SampleStrategy.py from sample_strategies/)."
                ),
                "checked_at": checked_at,
            },
        )

    return {
        "ok": True,
        "provider_id": PROVIDER_ID,
        "engine_name": ENGINE_NAME,
        "engine_version": engine_version,
        "data_ready": has_data,
        "strategies_available": strategies,
        "checked_at": checked_at,
    }


def _build_catalog_tool(
    tool_id: str,
    name: str,
    strategy_default: str,
    engine_version: str,
    available_symbols: list[str],
    is_default: bool,
) -> dict[str, Any]:
    """Build one tool entry in the cutie.backtest_provider_catalog.v1 shape (IMPL §5.1).

    Does NOT include `health` — health is derived by the connector from /health
    and smoke/catalog checks, never declared in the provider catalog.
    """
    return {
        "tool_id": tool_id,
        "kind": "external_http",
        "name": name,
        "description": (
            "Runs a local Freqtrade strategy class via the Freqtrade CLI on the "
            "local Freqtrade data directory; results parsed from the official "
            "backtest result file."
        ),
        "wrapper_type": "local_cli",
        "provider_name": PROVIDER_NAME,
        "engine_name": ENGINE_NAME,
        "engine_version": engine_version,
        "data_source": {
            "type": "provider_reported",
            "name": DATA_SOURCE,
            "description": (
                "Historical OHLCV from the local Freqtrade data directory; Cutie "
                "does not verify coverage, pairlist reproducibility, or data quality."
            ),
            "coverage_hint": (
                f"Pairs present in user_data/data: {', '.join(available_symbols)}"
                if available_symbols
                else "No local data downloaded yet"
            ),
            "external_unverified": True,
        },
        "supported_symbols": available_symbols,
        "markets": ["spot"],
        "timeframes": SUPPORTED_TIMEFRAMES,
        "is_default": is_default,
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
            "result_file_patterns": ["backtest-result-*.json", "backtest-result-*.zip"],
            "upstream_auth_local_only": True,
        },
        "param_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "strategy_name": {
                    "type": "string",
                    "default": strategy_default,
                    "description": "Freqtrade strategy class name",
                },
                "exchange": {
                    "type": "string",
                    "default": DEFAULT_EXCHANGE,
                    "description": "Exchange name for config",
                },
            },
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
            "formats": ["json"],
            "retention_hint": "last_100_runs",
        },
        "failure_codes": [
            "INVALID_PARAMS",
            "NO_DATA",
            "ENGINE_ERROR",
            "REPORT_UNAVAILABLE",
        ],
        "security": {
            "network_scope": "openclaw_hermes_local_or_private",
            "requires_user_secret": False,
            "secrets_stay_local": True,
            "live_trading": False,
            "filesystem_paths_exposed": False,
        },
    }


@app.get("/catalog")
async def catalog(authorization: Optional[str] = Header(None)):
    """Return provider catalog (schema cutie.backtest_provider_catalog.v1, IMPL §5.1).

    Dynamically generates tools from available strategies.
    """
    _check_auth(authorization)

    engine_version = _get_engine_version() or "unknown"
    strategies = _list_strategies()
    has_data, available_pairs = _check_data_directory()
    available_symbols = [_pair_file_to_symbol(pair) for pair in available_pairs] if has_data else ["BTCUSDT"]

    tools: list[dict] = []

    if strategies:
        # First strategy is default
        for idx, strategy_name in enumerate(strategies):
            tools.append(_build_catalog_tool(
                tool_id=f"local.freqtrade.{_camel_to_snake(strategy_name)}",
                name=f"Local Freqtrade {strategy_name}",
                strategy_default=strategy_name,
                engine_version=engine_version,
                available_symbols=available_symbols,
                is_default=(idx == 0),
            ))
    else:
        # No strategies installed yet. Connector will derive unhealthy from /health.
        tools.append(_build_catalog_tool(
            tool_id="local.freqtrade.default_strategy",
            name="Local Freqtrade Default Strategy",
            strategy_default="SampleStrategy",
            engine_version=engine_version,
            available_symbols=available_symbols,
            is_default=True,
        ))

    return {
        "schema": "cutie.backtest_provider_catalog.v1",
        "provider": {
            "provider_id": PROVIDER_ID,
            "provider_name": PROVIDER_NAME,
            "provider_version": PROVIDER_VERSION,
            "homepage_url": PROVIDER_HOMEPAGE_URL,
            "maintainer": PROVIDER_MAINTAINER,
        },
        "tools": tools,
    }


@app.post("/cutie/backtest")
async def run_backtest(request: Request, authorization: Optional[str] = Header(None)):
    """Execute Freqtrade backtesting and return Cutie-schema results.

    Receives JSON body per IMPL W3.8 section 5.4 contract.
    """
    _check_auth(authorization)

    engine_version = _get_engine_version() or "unknown"

    # Parse request body
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={
            "schema": RESPONSE_SCHEMA,
            "result_status": "failed",
            "provider_name": PROVIDER_NAME,
            "error_type": "INVALID_REQUEST",
            "error_message": "Request body must be valid JSON",
        })

    backtest = body.get("backtest", {})
    provider_info = body.get("provider", {})

    run_id = re.sub(r'[^a-zA-Z0-9_\-]', '', str(backtest.get("run_id", str(uuid.uuid4()))))[:64]
    provider_tool_id = backtest.get("provider_tool_id", "")
    provider_params = backtest.get("provider_params", {})
    symbol = backtest.get("symbol", "")
    timeframe = backtest.get("timeframe", "1h")
    start_at = backtest.get("start_at")
    end_at = backtest.get("end_at")
    initial_capital_str = backtest.get("initial_capital", "10000")
    fee_bps_str = backtest.get("fee_bps", "10")
    slippage_bps_str = backtest.get("slippage_bps", "5")

    # ----- Validate params -----

    # Strategy name from provider_params or tool_id
    strategy_name = provider_params.get("strategy_name")
    if not strategy_name:
        # Try to extract from tool_id: local.freqtrade.<name>
        if provider_tool_id and provider_tool_id.startswith("local.freqtrade."):
            strategy_name = provider_tool_id.split(".", 2)[-1]
            # Capitalize first letter to match class name convention
            if strategy_name:
                strategy_name = strategy_name[0].upper() + strategy_name[1:]
        if not strategy_name:
            strategy_name = "SampleStrategy"

    exchange = provider_params.get("exchange", DEFAULT_EXCHANGE)

    # Validate strategy exists
    available_strategies = _list_strategies()
    # Match by exact name, case-insensitive, or snake_case form
    matched_strategy = None
    for s in available_strategies:
        if (
            s == strategy_name
            or s.lower() == strategy_name.lower()
            or _camel_to_snake(s) == strategy_name.lower()
        ):
            matched_strategy = s
            break
    if not matched_strategy:
        return _business_failure(
            run_id=run_id,
            engine_version=engine_version,
            error_type="INVALID_PARAMS",
            error_message=(
                f"Strategy '{strategy_name}' not found. "
                f"Available strategies: {available_strategies}"
            ),
        )
    strategy_name = matched_strategy

    # Validate symbol
    if not symbol:
        return _business_failure(
            run_id=run_id,
            engine_version=engine_version,
            error_type="INVALID_PARAMS",
            error_message="symbol is required",
        )

    pair = _symbol_to_pair(symbol)

    # Validate timeframe
    if timeframe not in SUPPORTED_TIMEFRAMES:
        return _business_failure(
            run_id=run_id,
            engine_version=engine_version,
            error_type="INVALID_PARAMS",
            error_message=f"Unsupported timeframe: {timeframe}. Supported: {SUPPORTED_TIMEFRAMES}",
        )

    # Validate timestamps
    if not start_at or not end_at:
        return _business_failure(
            run_id=run_id,
            engine_version=engine_version,
            error_type="INVALID_PARAMS",
            error_message="start_at and end_at are required (unix seconds)",
        )

    try:
        start_at = int(start_at)
        end_at = int(end_at)
    except (ValueError, TypeError):
        return _business_failure(
            run_id=run_id,
            engine_version=engine_version,
            error_type="INVALID_PARAMS",
            error_message="start_at and end_at must be integers (unix seconds)",
        )

    if end_at <= start_at:
        return _business_failure(
            run_id=run_id,
            engine_version=engine_version,
            error_type="INVALID_PARAMS",
            error_message="end_at must be after start_at",
        )

    # Parse decimal values
    try:
        initial_capital = _parse_decimal(initial_capital_str, "initial_capital")
        fee_bps = _parse_decimal(fee_bps_str, "fee_bps")
        slippage_bps = _parse_decimal(slippage_bps_str, "slippage_bps")
    except ValueError as e:
        return _business_failure(
            run_id=run_id,
            engine_version=engine_version,
            error_type="INVALID_PARAMS",
            error_message=str(e),
        )

    # Check the exact exchange + pair + timeframe and, when the local storage
    # format is readable, prove the requested interval is fully covered before
    # paying the cost of a Freqtrade subprocess. The previous pair-only check
    # treated stale May data as ready for a recent 30-day run.
    coverage = _inspect_local_data_coverage(exchange, pair, timeframe, start_at, end_at)
    if coverage["status"] == "missing":
        download_command = _download_data_command(
            exchange,
            pair,
            timeframe,
            start_at,
            end_at,
            prepend=False,
        )
        return _business_failure(
            run_id=run_id,
            engine_version=engine_version,
            error_type="NO_DATA",
            error_message=(
                f"No local OHLCV file for {exchange} {pair} {timeframe}. "
                f"Download it first: {download_command}"
            ),
            reason="local_data_missing",
            raw_report={"market_data_coverage": coverage},
        )
    if coverage["status"] == "outside_coverage":
        prepend = start_at < coverage["actual_start_at"]
        download_command = _download_data_command(
            exchange,
            pair,
            timeframe,
            start_at,
            end_at,
            prepend=prepend,
        )
        return _business_failure(
            run_id=run_id,
            engine_version=engine_version,
            error_type="NO_DATA",
            error_message=(
                f"Local {exchange} {pair} {timeframe} data covers "
                f"{_utc_minute(coverage['actual_start_at'])} through "
                f"{_utc_minute(coverage['actual_last_open_at'])}, but this backtest requests "
                f"{_utc_date(start_at)} to {_utc_date(end_at)}. "
                f"Update the local data, then retry: {download_command}"
            ),
            reason="local_data_outside_coverage",
            raw_report={"market_data_coverage": coverage},
        )

    # ----- Build temporary Freqtrade config -----

    # Fee: convert from bps to ratio (10 bps = 0.001)
    fee_ratio = float(fee_bps / Decimal("10000"))
    timerange = _ts_to_timerange_str(start_at, end_at)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    provider_run_id = f"ft_{run_id}"
    backtest_dir = REPORTS_DIR / provider_run_id
    backtest_dir.mkdir(parents=True, exist_ok=True)
    stake_amount = max(
        Decimal("1"),
        min(initial_capital * Decimal("0.1"), initial_capital * Decimal("0.99")),
    )

    ft_config = {
        "exchange": {
            "name": exchange,
            "key": "",
            "secret": "",
            "pair_whitelist": [pair],
            "pair_blacklist": [],
        },
        "pairlists": [{"method": "StaticPairList"}],
        "stake_currency": pair.split("/")[-1] if "/" in pair else "USDT",
        "stake_amount": float(stake_amount),
        "dry_run_wallet": float(initial_capital),
        "dry_run": True,
        "trading_mode": "spot",
        "margin_mode": "",
        "timeframe": timeframe,
        "fee": fee_ratio,
        "max_open_trades": 1,
        "entry_pricing": {
            "price_side": "same",
            "use_order_book": True,
            "order_book_top": 1,
        },
        "exit_pricing": {
            "price_side": "same",
            "use_order_book": True,
            "order_book_top": 1,
        },
    }
    if coverage.get("status") == "covered" and coverage.get("data_format"):
        # Make the CLI consume the same physical format whose bounds passed the
        # preflight.  Otherwise a covered JSON file could pass this guard while
        # Freqtrade silently keeps its default (normally Feather) and reports no
        # data for the very same request.
        ft_config["dataformat_ohlcv"] = coverage["data_format"]

    # Write temp config
    tmp_config = None
    try:
        tmp_fd, tmp_config = tempfile.mkstemp(suffix=".json", prefix="ft_config_")
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(ft_config, f)

        # ----- Run Freqtrade backtesting -----

        cmd = [
            FREQTRADE_CMD,
            "backtesting",
            "--strategy", strategy_name,
            "--config", tmp_config,
            "--userdir", str(FREQTRADE_USERDIR),
            "--timerange", timerange,
            "--export", "trades",
            "--backtest-directory", str(backtest_dir),
            "--no-color",
        ]

        logger.info("Running Freqtrade: %s", " ".join(cmd))

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=BACKTEST_TIMEOUT,
                cwd=str(FREQTRADE_USERDIR.parent),
            )
        except subprocess.TimeoutExpired:
            return _business_failure(
                run_id=run_id,
                engine_version=engine_version,
                error_type="ENGINE_ERROR",
                error_message=f"Freqtrade backtesting timed out after {BACKTEST_TIMEOUT}s",
            )

        if proc.returncode != 0:
            error_output = proc.stderr.strip() or proc.stdout.strip()
            # CLI log prologues are long; Freqtrade puts the actionable cause
            # (for example "No data found") at the end. Preserve that tail.
            error_output = _diagnostic_tail(error_output)
            no_data = "no data found" in error_output.lower()
            return _business_failure(
                run_id=run_id,
                engine_version=engine_version,
                error_type="NO_DATA" if no_data else "ENGINE_ERROR",
                error_message=f"Freqtrade backtesting failed (exit {proc.returncode}): {error_output}",
                reason="freqtrade_no_data" if no_data else None,
                raw_report={"market_data_coverage": coverage},
            )

        # ----- Parse results -----

        # Freqtrade writes a backtest-result zip/json into the selected directory.
        results_dir = FREQTRADE_USERDIR / "backtest_results"
        result_path = _find_latest_backtest_result(backtest_dir)
        if not result_path:
            result_path = _find_latest_backtest_result(results_dir)

        if not result_path:
            # Avoid leaking absolute local paths into the response (IMPL §7/§12).
            return _business_failure(
                run_id=run_id,
                engine_version=engine_version,
                error_type="REPORT_UNAVAILABLE",
                error_message=(
                    "Freqtrade completed but the backtest result file was not found "
                    "in the run or backtest_results directory."
                ),
            )

        try:
            parsed = _parse_freqtrade_result(result_path, pair)
        except Exception as e:
            logger.exception("Failed to parse Freqtrade result")
            return _business_failure(
                run_id=run_id,
                engine_version=engine_version,
                error_type="ENGINE_ERROR",
                error_message=f"Failed to parse Freqtrade result: {e}",
            )

        # Copy result to reports directory for serving
        report_dest = REPORTS_DIR / f"{provider_run_id}{result_path.suffix}"
        try:
            shutil.copy2(result_path, report_dest)
        except Exception:
            logger.warning("Failed to copy result to reports dir", exc_info=True)

        _cleanup_reports()

        # Compute result hash
        result_hash = _compute_hash(parsed)

        strategy_assumptions, strategy_limitations, strategy_raw_report = _strategy_semantics(
            body,
            strategy_name,
        )

        response = {
            "schema": RESPONSE_SCHEMA,
            "result_status": "success",
            "provider_name": PROVIDER_NAME,
            "provider_run_id": provider_run_id,
            "engine_name": ENGINE_NAME,
            "engine_version": engine_version,
            "data_source": DATA_SOURCE,
            "result_hash": result_hash,
            # report_url is a relative path/ref only (IMPL §7); no scheme/host/absolute path.
            "report_url": f"reports/{provider_run_id}{result_path.suffix}",
            "report_url_scope": "local_machine_only",
            "metrics": parsed["metrics"],
            "initial_capital": _decimal_str(initial_capital, places=2),
            "equity_curve": parsed["equity_curve"],
            "trades": parsed["trades"],
            "assumptions": {
                "fee_bps": _decimal_str(fee_bps, places=4),
                "slippage_bps": _decimal_str(slippage_bps, places=4),
                "exchange": exchange,
                "strategy_name": strategy_name,
                **strategy_assumptions,
                "real_market_data": True,
                "no_live_trading": True,
            },
            "limitations": {
                "verification": "external_unverified",
                "verified_by_cutie": False,
                **strategy_limitations,
                "sample_size": "provider_reported",
                "data_quality": "provider_reported",
            },
            "raw_report": {
                "freqtrade_summary": json.dumps(parsed.get("raw_summary", {})),
                "strategy_semantics": strategy_raw_report,
            },
        }

        return response

    finally:
        if tmp_config and os.path.exists(tmp_config):
            try:
                os.unlink(tmp_config)
            except OSError:
                pass


def _business_failure(
    run_id: str,
    engine_version: str,
    error_type: str,
    error_message: str,
    *,
    reason: Optional[str] = None,
    raw_report: Optional[dict[str, Any]] = None,
) -> dict:
    """Return a business failure response (provider is healthy, but backtest cannot proceed)."""
    return {
        "schema": RESPONSE_SCHEMA,
        "result_status": "failed",
        "provider_name": PROVIDER_NAME,
        "provider_run_id": f"ft_{run_id}",
        "engine_name": ENGINE_NAME,
        "engine_version": engine_version,
        "data_source": DATA_SOURCE,
        "error_type": error_type,
        "error_message": error_message,
        "assumptions": {},
        "limitations": {
            "reason": reason or error_type.lower(),
        },
        "raw_report": raw_report or {},
    }


# ---------------------------------------------------------------------------
# Serve report files
# ---------------------------------------------------------------------------

@app.get("/reports/{filename}")
async def get_report(filename: str):
    """Serve a backtest report file from the reports directory."""
    # Sanitize filename - only allow alphanumeric, dash, underscore, dot
    if not re.match(r"^[\w\-.]+$", filename):
        raise HTTPException(status_code=400, detail="Invalid filename")

    file_path = REPORTS_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Report not found")

    return FileResponse(
        path=str(file_path),
        media_type="application/json",
        filename=filename,
    )


# ---------------------------------------------------------------------------
# Global exception handlers -- all responses must be JSON
# ---------------------------------------------------------------------------

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
            "error_message": str(exc.detail),
        },
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("CUTIE_BACKTEST_PORT", str(DEFAULT_PORT)))

    if not PROVIDER_TOKEN:
        logger.warning(
            "CUTIE_BACKTEST_PROVIDER_TOKEN not set. "
            "/catalog and /cutie/backtest will reject all requests."
        )

    uvicorn.run(
        "cutie_freqtrade_provider:app",
        host="127.0.0.1",
        port=port,
        log_level="info",
    )
