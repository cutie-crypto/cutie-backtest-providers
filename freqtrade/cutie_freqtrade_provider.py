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

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
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
ENGINE_NAME = "Freqtrade"
DEFAULT_PORT = 8766
DEFAULT_EXCHANGE = os.environ.get("CUTIE_FREQTRADE_DEFAULT_EXCHANGE", "okx").lower()

SUPPORTED_TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]

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


def _list_strategies() -> list[str]:
    """List available strategy .py files in userdir/strategies/."""
    strategies_dir = FREQTRADE_USERDIR / "strategies"
    if not strategies_dir.is_dir():
        return []
    result = []
    for f in strategies_dir.iterdir():
        if f.suffix == ".py" and f.stem != "__init__" and not f.stem.startswith("_"):
            result.append(f.stem)
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
            if data_file.suffix in (".json", ".feather", ".h5", ".gz"):
                # Extract pair from filename like "BTC_USDT-1h.json"
                name = data_file.stem
                if "-" in name:
                    pair_part = name.rsplit("-", 1)[0]
                    pairs.add(pair_part)
    return len(pairs) > 0, sorted(pairs)


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
        if f.suffix == ".json" and f.stem.startswith("backtest-result")
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
    with open(result_path, "r") as f:
        raw = json.load(f)

    # Navigate to strategy results - take first strategy
    strategy_data = raw.get("strategy", {})
    if not strategy_data:
        raise ValueError("No strategy results found in Freqtrade output")

    strategy_name = list(strategy_data.keys())[0]
    strat_result = strategy_data[strategy_name]

    # Extract metrics
    total_trades = strat_result.get("total_trades", 0)
    winning_trades = strat_result.get("winning_trades", 0)
    profit_total = strat_result.get("profit_total", 0)  # as ratio (0.05 = 5%)
    profit_total_abs = strat_result.get("profit_total_abs", 0)
    max_drawdown = strat_result.get("max_drawdown", 0)  # as ratio
    max_drawdown_abs = strat_result.get("max_drawdown_abs", 0)

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
            "pnl": t.get("profit_abs", 0),
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
                daily_pnl = Decimal(str(entry[1]))
                cumulative += daily_pnl
                try:
                    dt = datetime.strptime(str(date_str), "%Y-%m-%d")
                    dt = dt.replace(tzinfo=timezone.utc)
                    curve.append({
                        "t": int(dt.timestamp()),
                        "equity": float(cumulative),
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
        pnl = Decimal(str(t.get("profit_abs", 0)))
        cumulative_pnl += pnl
        curve.append({
            "t": close_ts,
            "equity": float(cumulative_pnl),
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


@app.get("/catalog")
async def catalog(authorization: Optional[str] = Header(None)):
    """Return provider catalog (schema cutie.backtest_provider_catalog.v1).

    Dynamically generates tools from available strategies.
    """
    _check_auth(authorization)

    engine_version = _get_engine_version() or "unknown"
    strategies = _list_strategies()

    tools: list[dict] = []

    if strategies:
        # First strategy is default
        for idx, strategy_name in enumerate(strategies):
            tool_id = f"local.freqtrade.{_camel_to_snake(strategy_name)}"
            is_default = idx == 0
            tool: dict[str, Any] = {
                "tool_id": tool_id,
                "kind": "external_http",
                "name": f"Local Freqtrade {strategy_name}",
                "provider_name": "Freqtrade Local",
                "engine_name": ENGINE_NAME,
                "engine_version": engine_version,
                "data_source": "freqtrade_data",
                "markets": ["spot"],
                "timeframes": SUPPORTED_TIMEFRAMES,
                "default": is_default,
                "health": "ok",
                "param_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "strategy_name": {
                            "type": "string",
                            "default": strategy_name,
                            "description": "Freqtrade strategy class name",
                        },
                        "exchange": {
                            "type": "string",
                            "default": DEFAULT_EXCHANGE,
                            "description": "Exchange name for config",
                        },
                    },
                },
                "expected_outputs": ["metrics", "trades", "report_url"],
            }
            tools.append(tool)
    else:
        # No strategies - provide a default tool entry marked unhealthy
        tools.append({
            "tool_id": "local.freqtrade.default_strategy",
            "kind": "external_http",
            "name": "Local Freqtrade Default Strategy",
            "provider_name": "Freqtrade Local",
            "engine_name": ENGINE_NAME,
            "engine_version": engine_version,
            "data_source": "freqtrade_data",
            "markets": ["spot"],
            "timeframes": SUPPORTED_TIMEFRAMES,
            "default": True,
            "health": "unavailable",
            "param_schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "strategy_name": {
                        "type": "string",
                        "default": "SampleStrategy",
                        "description": "Freqtrade strategy class name",
                    },
                    "exchange": {
                        "type": "string",
                        "default": DEFAULT_EXCHANGE,
                        "description": "Exchange name for config",
                    },
                },
            },
            "expected_outputs": ["metrics", "trades", "report_url"],
        })

    return {
        "schema": "cutie.backtest_provider_catalog.v1",
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
            "result_status": "failed",
            "provider_name": "Freqtrade Local",
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

    # Check data availability
    has_data, available_pairs = _check_data_directory()
    if not has_data:
        return _business_failure(
            run_id=run_id,
            engine_version=engine_version,
            error_type="NO_DATA",
            error_message=(
                f"No OHLCV data found in {FREQTRADE_USERDIR}/data/. "
                f"Download data first: freqtrade download-data --userdir {FREQTRADE_USERDIR} "
                f"--exchange {exchange} --pairs {pair} --timeframes {timeframe}"
            ),
        )

    # Normalize pair for comparison: BTC/USDT -> BTC_USDT (Freqtrade file naming)
    pair_file_name = pair.replace("/", "_")
    pair_found = any(pair_file_name in p for p in available_pairs)
    if not pair_found:
        return _business_failure(
            run_id=run_id,
            engine_version=engine_version,
            error_type="NO_DATA",
            error_message=(
                f"No data for pair {pair} ({pair_file_name}). "
                f"Available pairs: {available_pairs}. "
                f"Download: freqtrade download-data --exchange {exchange} "
                f"--pairs {pair} --timeframes {timeframe}"
            ),
        )

    # ----- Build temporary Freqtrade config -----

    # Fee: convert from bps to ratio (10 bps = 0.001)
    fee_ratio = float(fee_bps / Decimal("10000"))
    timerange = _ts_to_timerange_str(start_at, end_at)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    provider_run_id = f"ft_{run_id}"
    export_filename = str(REPORTS_DIR / f"{provider_run_id}")

    ft_config = {
        "exchange": {
            "name": exchange,
            "key": "",
            "secret": "",
            "pair_whitelist": [pair],
        },
        "stake_currency": pair.split("/")[-1] if "/" in pair else "USDT",
        "stake_amount": float(initial_capital),
        "dry_run": True,
        "trading_mode": "spot",
        "margin_mode": "",
        "timeframe": timeframe,
        "fee": {
            "buy": fee_ratio,
            "sell": fee_ratio,
        },
    }

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
            "--export-filename", export_filename,
            "--no-header",
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
                error_type="STRATEGY_ERROR",
                error_message=f"Freqtrade backtesting timed out after {BACKTEST_TIMEOUT}s",
            )

        if proc.returncode != 0:
            error_output = proc.stderr.strip() or proc.stdout.strip()
            # Truncate long error output
            if len(error_output) > 1000:
                error_output = error_output[:1000] + "... (truncated)"
            return _business_failure(
                run_id=run_id,
                engine_version=engine_version,
                error_type="STRATEGY_ERROR",
                error_message=f"Freqtrade backtesting failed (exit {proc.returncode}): {error_output}",
            )

        # ----- Parse results -----

        # Freqtrade writes to export_filename.json or backtest_results/ directory
        results_dir = FREQTRADE_USERDIR / "backtest_results"
        result_path = _find_latest_backtest_result(results_dir, export_filename)

        if not result_path:
            # Try with .json suffix
            result_path = _find_latest_backtest_result(
                results_dir,
                export_filename + ".json",
            )

        if not result_path:
            return _business_failure(
                run_id=run_id,
                engine_version=engine_version,
                error_type="STRATEGY_ERROR",
                error_message=(
                    f"Freqtrade completed but result file not found. "
                    f"Checked: {export_filename}, {results_dir}. "
                    f"Stdout: {proc.stdout[:500] if proc.stdout else '(empty)'}"
                ),
            )

        try:
            parsed = _parse_freqtrade_result(result_path, pair)
        except Exception as e:
            logger.exception("Failed to parse Freqtrade result")
            return _business_failure(
                run_id=run_id,
                engine_version=engine_version,
                error_type="STRATEGY_ERROR",
                error_message=f"Failed to parse Freqtrade result: {e}",
            )

        # Copy result to reports directory for serving
        report_dest = REPORTS_DIR / f"{provider_run_id}.json"
        try:
            shutil.copy2(result_path, report_dest)
        except Exception:
            logger.warning("Failed to copy result to reports dir", exc_info=True)

        _cleanup_reports()

        # Compute result hash
        result_hash = _compute_hash(parsed)

        port = int(os.environ.get("CUTIE_BACKTEST_PORT", str(DEFAULT_PORT)))

        response = {
            "result_status": "success",
            "provider_name": "Freqtrade Local",
            "provider_run_id": provider_run_id,
            "engine_name": ENGINE_NAME,
            "engine_version": engine_version,
            "data_source": "freqtrade_data",
            "result_hash": result_hash,
            "report_url": f"http://127.0.0.1:{port}/reports/{provider_run_id}.json",
            "report_url_scope": "local_machine_only",
            "metrics": parsed["metrics"],
            "equity_curve": parsed["equity_curve"],
            "trades": parsed["trades"],
            "assumptions": {
                "fee_bps": int(fee_bps),
                "slippage_bps": int(slippage_bps),
                "exchange": exchange,
                "strategy_name": strategy_name,
                "real_market_data": True,
                "no_live_trading": True,
            },
            "limitations": {
                "verification": "external_unverified",
                "verified_by_cutie": False,
                "sample_size": "provider_reported",
                "data_quality": "provider_reported",
            },
            "raw_report": {
                "freqtrade_summary": json.dumps(parsed.get("raw_summary", {})),
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
) -> dict:
    """Return a business failure response (provider is healthy, but backtest cannot proceed)."""
    return {
        "result_status": "failed",
        "provider_name": "Freqtrade Local",
        "provider_run_id": f"ft_{run_id}",
        "engine_name": ENGINE_NAME,
        "engine_version": engine_version,
        "data_source": "freqtrade_data",
        "error_type": error_type,
        "error_message": error_message,
        "assumptions": {},
        "limitations": {
            "reason": error_type.lower(),
        },
        "raw_report": {},
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
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "result_status": "failed",
            "provider_name": "Freqtrade Local",
            "error_type": f"HTTP_{exc.status_code}",
            "error_message": str(exc.detail),
        },
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception")
    return JSONResponse(
        status_code=500,
        content={
            "result_status": "failed",
            "provider_name": "Freqtrade Local",
            "error_type": "INTERNAL_ERROR",
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
