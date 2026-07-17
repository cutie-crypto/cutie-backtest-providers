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
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from canonical_json import canonical_decimal_str, canonical_json_sha256
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from strategy_execution import (
    EXECUTION_MAX_RANGE_DAYS,
    CoverageInput,
    build_artifact_response,
    build_coverage_manifest,
    is_strategy_execution_intent,
    validate_execution_request,
)
from strategy_kernel import (
    COMPILER_TOOL_ID,
    ERR_CAPABILITY_MISMATCH,
    ERR_COVERAGE_INCOMPLETE,
    KernelExecutionError,
    StrategyContractError,
    build_frames,
    capability_hash,
    capability_payload,
    initial_state,
    kline_primary_bucket_required_start,
    ohlcv_resample,
    simulate,
)

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

BASE_DIR = Path(__file__).resolve().parent
REPORTS_DIR = BASE_DIR / "reports"
CACHE_DIR = BASE_DIR / "cache" / "ohlcv"
MAX_REPORTS = 100
CACHE_TTL_SECONDS = 24 * 60 * 60  # 24 hours
MAX_CACHE_FILES = 500  # LRU 上限：超出按 mtime 删最旧文件（WS-7 Step 7.3）

# WS-7 Step 7.3：中心行情数据服务（cutie-server /v1/internal/market-data/klines）。
# 未配置 URL/token 时本 provider 完全走原有 ccxt 路径（向后兼容，不强制依赖中心服务）。
# 62-1 F1：中心缓存覆盖 binance spot + futures（Binance USDT 永续，分源存储，见
# cutie-server market_kline_cache_service.py 范围声明；server 侧 /klines 接受
# market=spot|futures|swap，swap 归一为 futures），其它 exchange/market 组合直接
# 跳过中心 API，不发请求。
CENTRAL_MARKET_DATA_URL = os.environ.get("CUTIE_CENTRAL_MARKET_DATA_URL", "").rstrip("/")
CENTRAL_MARKET_DATA_TOKEN = os.environ.get("CUTIE_CENTRAL_MARKET_DATA_TOKEN", "")
CENTRAL_MARKET_DATA_TIMEOUT_SEC = float(os.environ.get("CUTIE_CENTRAL_MARKET_DATA_TIMEOUT_SEC", "5"))
CENTRAL_MARKET_DATA_USER_AGENT = f"cutie-backtest-provider/{PROVIDER_VERSION}"
_raw_provider_revision = os.environ.get("CUTIE_PROVIDER_REVISION", "").strip()
PROVIDER_REVISION = (
    _raw_provider_revision
    if re.fullmatch(r"[0-9a-f]{7,64}", _raw_provider_revision)
    else "unknown"
)
CENTRAL_SUPPORTED_EXCHANGE = "binance"
CENTRAL_SUPPORTED_MARKETS = frozenset({"spot", "futures"})
# 数据缺口容差：中心 API 返回条数 < 预期条数 * 该比例即视为缺口，回退 ccxt（不是硬失败）。
CENTRAL_GAP_TOLERANCE_RATIO = 0.9

# 62-1 result.v2（SPEC_验证复核契约.md §2）：schema 常量 + data_manifest.source 命名。
# 后者必须与 TokenBeep 主仓 cutie-server/services/strategy_backtest_service.py 的
# _CENTRAL_MARKET_SOURCES 完全一致——server 的逐 run 完整性门按此常量做身份核对，
# 命名不一致会 fail-closed 拒绝升级 platform_managed（与数据内容是否正确无关，纯字符串
# 核对）。futures 中心命中现已启用（见 CENTRAL_SUPPORTED_MARKETS），映射直接生效。
RESULT_V2_SCHEMA = "cutie.backtest_result.v2"
_CENTRAL_MARKET_SOURCES = {"spot": "binance_us", "futures": "binance_futures"}

# 进程启动时刻（Unix 秒），/health.process_fingerprint 用；同一进程生命周期内不变。
_PROCESS_START_TIME = int(time.time())


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward the central-market Bearer token through an HTTP redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ARG002
        return None


_CENTRAL_HTTP_OPENER = urllib.request.build_opener(_NoRedirectHandler())
_CENTRAL_STATS_LOCK = threading.Lock()
_CENTRAL_FETCH_SUCCESS_COUNT = 0
_CENTRAL_LAST_SUCCESS_AT = 0

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


def _cache_key(exchange: str, market: str, symbol: str, timeframe: str, start_ms: int, end_ms: int) -> str:
    # fully-qualified 输入（BTC/USDT:USDT）含 / 会让 key 变成子目录路径，_write_cache
    # 不建中间目录导致缓存写入被静默吞掉——文件名字符一律 sanitize
    safe_symbol = re.sub(r"[^A-Za-z0-9]", "-", symbol)
    return f"{exchange}_{market}_{safe_symbol}_{timeframe}_{start_ms}_{end_ms}.json"


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


def _read_cache(key: str) -> Optional[dict[str, Any]]:
    path = CACHE_DIR / key
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age > CACHE_TTL_SECONDS:
        path.unlink(missing_ok=True)
        return None
    try:
        with open(path, "r") as f:
            cached = json.load(f)
        if isinstance(cached, list):
            # Backward-compatible read of pre-provenance cache files.
            return {"ohlcv": cached, "source": "legacy_ohlcv_cache", "central_market_data_used": None}
        if isinstance(cached, dict) and isinstance(cached.get("ohlcv"), list):
            return {
                "ohlcv": cached["ohlcv"],
                "source": str(cached.get("source") or "unknown_ohlcv_cache"),
                "central_market_data_used": cached.get("central_market_data_used"),
            }
        return None
    except Exception as e:
        logger.warning("Cache read failed for %s: %s", key, e)
        return None


def _write_cache(
    key: str,
    data: list,
    *,
    source: str = DATA_SOURCE,
    central_market_data_used: Optional[bool] = False,
) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / key
    try:
        with open(path, "w") as f:
            json.dump(
                {
                    "ohlcv": data,
                    "source": source,
                    "central_market_data_used": central_market_data_used,
                },
                f,
            )
    except Exception as e:
        logger.warning("Cache write failed for %s: %s", key, e)
        return
    _enforce_cache_lru_limit()


def _enforce_cache_lru_limit() -> None:
    """LRU 上限：OHLCV 缓存文件数超过 MAX_CACHE_FILES 时按 mtime 删最旧的（WS-7 Step 7.3）。

    与 _enforce_reports_retention 同款模式，但对象是 cache/ohlcv/ 而非 reports/——
    中心 API 接入后 provider 仍会为多币种/多周期/多月份组合持续写入缓存文件，
    原有仅靠 CACHE_TTL_SECONDS 过期不足以防止长期运行下磁盘无界增长。
    """
    if not CACHE_DIR.exists():
        return
    files = sorted(
        [f for f in CACHE_DIR.iterdir() if f.is_file() and f.suffix == ".json"],
        key=lambda f: f.stat().st_mtime,
    )
    while len(files) > MAX_CACHE_FILES:
        oldest = files.pop(0)
        try:
            oldest.unlink()
        except OSError as e:
            logger.warning("Failed to delete old cache file %s: %s", oldest, e)


def _expected_bar_count(timeframe: str, start_ms: int, end_ms: int) -> int:
    step_ms = _timeframe_milliseconds(timeframe)
    if step_ms <= 0:
        return 0
    return max(0, (min(end_ms, int(time.time() * 1000)) - start_ms) // step_ms)


def _record_central_fetch_success() -> None:
    global _CENTRAL_FETCH_SUCCESS_COUNT, _CENTRAL_LAST_SUCCESS_AT
    with _CENTRAL_STATS_LOCK:
        _CENTRAL_FETCH_SUCCESS_COUNT += 1
        _CENTRAL_LAST_SUCCESS_AT = int(time.time())


def _central_health_snapshot() -> dict[str, int]:
    with _CENTRAL_STATS_LOCK:
        return {
            "central_fetch_success_count": _CENTRAL_FETCH_SUCCESS_COUNT,
            "central_last_success_at": _CENTRAL_LAST_SUCCESS_AT,
        }


def _central_market_data_auth_mode() -> str:
    return "market_data_bearer" if CENTRAL_MARKET_DATA_URL and CENTRAL_MARKET_DATA_TOKEN else "disabled"


def _process_fingerprint() -> str:
    """/health.process_fingerprint（SPEC §2 wire 字段 provider_process_fingerprint 的
    provider 侧来源）：格式 "{hostname}:{pid}:{进程启动 Unix 秒}"，随进程重启天然更新，
    同一进程生命周期内保持不变（区别于 PROVIDER_REVISION——那是代码版本，这是运行实例）。
    """
    return f"{socket.gethostname()}:{os.getpid()}:{_PROCESS_START_TIME}"


# F7 修（Kimi review）：server 侧 handlers/internal/market_data.py 的 MAX_KLINE_RANGE_SECONDS
# 单次请求跨度上限是 90 天，但 provider 的回测区间最长可达 365 天（EXECUTION_MAX_RANGE_DAYS）。
# 此前 _fetch_from_central 整段区间打一次请求，长区间回测一律被 server 参数校验拒绝
# （ERR_INVALID_PARAMS），直接回退 ccxt——中心缓存对长区间 spot 回测形同虚设，白建了
# Step 7.2 的月度分片缓存却用不上。改为按 90 天分片串行请求再拼接。
CENTRAL_MAX_CHUNK_MS = 90 * 24 * 60 * 60 * 1000


def _fetch_from_central(
    exchange_id: str, market: str, symbol: str, timeframe: str, start_ms: int, end_ms: int
) -> Optional[list]:
    """尝试从 cutie-server 中心行情 API 拉 K 线；任何失败/缺口都返回 None 触发 ccxt 回退。

    回退条件（附录 C.4）：超时 / 5xx / 数据缺口。未配置中心 API 或
    exchange/market 不在中心缓存覆盖范围内（binance spot + futures，见
    CENTRAL_SUPPORTED_MARKETS）时直接跳过，不发请求。

    HIGH-3 修（2a review）：中心 API（cutie-server market_kline_cache_service）内部把裸
    symbol 一律映射成 Binance **USDT** 交易对（COIN_TO_PAIR）。此前本函数把归一化后的
    symbol 无脑剥成 base（`.split("/")[0]`）就发给中心 API——若请求方实际要的是非 USDT
    计价对（如 ETHBTC、BTC/USDC），中心 API 会静默返回错误 quote 的数据（BTCUSDT 而非
    BTC/USDC），provider 把这份"看起来正常"但价格体系完全不同的数据当正确结果缓存/喂给
    回测引擎——没有任何报错，纯粹的脏数据 bug。修复：只有归一化后 quote 恰好是 USDT
    时才走中心 API，否则直接回退 ccxt（ccxt 走真实 symbol，语义不会错）。

    F7 修（Kimi review）：>90 天的区间按 `CENTRAL_MAX_CHUNK_MS` 分片串行调用中心 API 再
    拼接。任一分片失败/缺口即整体返回 None（回退 ccxt），维持"要么完整命中中心缓存要么
    整段走 ccxt"的语义——不会出现"前半段中心数据 + 后半段 ccxt 数据"拼出来的混合序列。

    62-1 F1（futures 扩展）：quote 提取要先剥掉 `_normalize_ohlcv_symbol` 给 futures
    加的 `:SETTLE` 后缀——futures 归一结果形如 `BTC/USDT:USDT`，若直接
    partition("/") 取 quote 会得到 `USDT:USDT`，永远 != "USDT"，导致 futures 100%
    误判非 USDT 计价对而被拒；本 provider 的 futures 归一约定 settle 恒等于 quote
    （USDT 本位永续），所以 `:` 前半段才是真正要核对的计价币种。
    """
    if not CENTRAL_MARKET_DATA_URL or not CENTRAL_MARKET_DATA_TOKEN:
        return None
    if exchange_id != CENTRAL_SUPPORTED_EXCHANGE or market not in CENTRAL_SUPPORTED_MARKETS:
        return None

    full_normalized = _normalize_ohlcv_symbol(symbol, market)
    if "/" not in full_normalized:
        return None
    base, _, quote_with_settle = full_normalized.partition("/")
    quote = quote_with_settle.split(":", 1)[0]
    if quote != "USDT":
        # 中心 API 只服务 USDT 计价对；非 USDT quote 直接回退 ccxt，避免张冠李戴。
        return None
    normalized_symbol = base

    all_rows: list = []
    chunk_start = start_ms
    while chunk_start < end_ms:
        chunk_end = min(chunk_start + CENTRAL_MAX_CHUNK_MS, end_ms)
        chunk_rows = _fetch_central_chunk(exchange_id, market, normalized_symbol, timeframe, chunk_start, chunk_end)
        if chunk_rows is None:
            return None  # 任一分片失败/缺口 → 整体回退 ccxt，不拼混合数据源
        all_rows.extend(chunk_rows)
        chunk_start = chunk_end

    _record_central_fetch_success()
    return all_rows


def _fetch_central_chunk(
    exchange_id: str, market: str, normalized_symbol: str, timeframe: str, start_ms: int, end_ms: int
) -> Optional[list]:
    """单个 ≤90 天分片的中心 API 请求 + 解析 + 缺口检测。返回 None 表示该分片失败/缺口。"""
    params = {
        "symbol": normalized_symbol,
        "exchange": exchange_id,
        "market": market,
        "interval": timeframe,
        "start_ts": start_ms // 1000,
        "end_ts": end_ms // 1000,
    }
    url = f"{CENTRAL_MARKET_DATA_URL}/klines?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {CENTRAL_MARKET_DATA_TOKEN}",
            "Accept": "application/json",
            # Cloudflare blocks urllib's default Python-urllib signature on the
            # real /klines route (error 1010).  Send a stable product identity
            # so managed Providers can reach the same endpoint that passed
            # direct Fan runtime acceptance with an explicit User-Agent.
            "User-Agent": CENTRAL_MARKET_DATA_USER_AGENT,
        },
    )
    try:
        with _CENTRAL_HTTP_OPENER.open(req, timeout=CENTRAL_MARKET_DATA_TIMEOUT_SEC) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if 500 <= e.code < 600:
            logger.warning("Central market-data 5xx (%s), falling back to ccxt: %s", e.code, e)
        else:
            logger.warning("Central market-data HTTP error %s, falling back to ccxt", e.code)
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        logger.warning("Central market-data unreachable/timeout, falling back to ccxt: %s", e)
        return None

    try:
        body = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("Central market-data response not JSON, falling back to ccxt: %s", e)
        return None

    if body.get("err_code") != 100:
        logger.warning("Central market-data err_code=%s, falling back to ccxt", body.get("err_code"))
        return None
    data = body.get("data") or {}
    if not data.get("available"):
        # exchange/symbol 组合中心缓存不覆盖：非错误，静默回退 ccxt。
        return None
    items = data.get("items") or []
    if not items:
        return None  # 数据缺口（空区间）：回退 ccxt

    expected = _expected_bar_count(timeframe, start_ms, end_ms)
    if expected and len(items) < expected * CENTRAL_GAP_TOLERANCE_RATIO:
        logger.warning(
            "Central market-data gap detected (got=%d expected=%d), falling back to ccxt",
            len(items),
            expected,
        )
        return None

    return [
        [item["open_time"] * 1000, item["open"], item["high"], item["low"], item["close"], item["volume"]]
        for item in items
    ]


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


def _normalize_ohlcv_symbol(symbol: str, market: str) -> str:
    """Normalize BTCUSDT / btcusdt -> BTC/USDT (spot) or BTC/USDT:USDT (futures).

    Futures uses ccxt's unified linear-perpetual-swap symbol convention
    (BASE/QUOTE:SETTLE, settle == quote for USDT-margined perpetuals), which
    works across exchanges (okx, binance, bybit, ...) without a dedicated
    per-exchange futures class.
    """
    upper_symbol = symbol.upper()
    if "/" in upper_symbol:
        normalized_symbol = upper_symbol
    else:
        normalized_symbol = upper_symbol
        for quote in ("USDT", "USDC", "BUSD", "BTC", "ETH", "BNB"):
            if upper_symbol.endswith(quote) and len(upper_symbol) > len(quote):
                base = upper_symbol[: len(upper_symbol) - len(quote)]
                normalized_symbol = f"{base}/{quote}"
                break
    if market == "futures" and ":" not in normalized_symbol and "/" in normalized_symbol:
        _, quote = normalized_symbol.split("/", 1)
        normalized_symbol = f"{normalized_symbol}:{quote}"
    return normalized_symbol


def _fetch_ohlcv(exchange_id: str, market: str, symbol: str, timeframe: str,
                 start_sec: int, end_sec: int) -> pd.DataFrame:
    """Fetch OHLCV from ccxt with local file cache."""
    import ccxt

    start_ms = start_sec * 1000
    end_ms = min(end_sec * 1000, int(time.time() * 1000))
    if end_ms <= start_ms:
        raise ValueError("NO_DATA")

    cache_key = _cache_key(exchange_id, market, symbol, timeframe, start_ms, end_ms)
    cached = _read_cache(cache_key)
    if cached is not None:
        ohlcv = cached["ohlcv"]
        actual_data_source = cached["source"]
        central_market_data_used: Optional[bool] = cached["central_market_data_used"]
        market_data_cache_hit = True
    elif (central_ohlcv := _fetch_from_central(exchange_id, market, symbol, timeframe, start_ms, end_ms)) is not None:
        # WS-7 Step 7.3：中心 API 命中，跳过 ccxt，直接写缓存。
        ohlcv = central_ohlcv
        actual_data_source = "cutie_central_market_data"
        central_market_data_used = True
        market_data_cache_hit = False
        _write_cache(
            cache_key,
            ohlcv,
            source=actual_data_source,
            central_market_data_used=central_market_data_used,
        )
    else:
        actual_data_source = DATA_SOURCE
        central_market_data_used = False
        market_data_cache_hit = False
        exchange_class = getattr(ccxt, exchange_id, None)
        if exchange_class is None:
            raise ValueError(f"Unsupported exchange: {exchange_id}")
        exchange_options: dict[str, Any] = {"enableRateLimit": True}
        if market == "futures":
            # Unified ccxt option: fetch/derive the linear perpetual swap market
            # instead of spot for the same exchange class.
            exchange_options["options"] = {"defaultType": "swap"}
        exchange = exchange_class(exchange_options)

        normalized_symbol = _normalize_ohlcv_symbol(symbol, market)

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
            _write_cache(
                cache_key,
                ohlcv,
                source=actual_data_source,
                central_market_data_used=central_market_data_used,
            )

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

    # Request-local provenance travels with the DataFrame, avoiding shared
    # globals when concurrent backtests use different market-data sources.
    df.attrs["cutie_data_source"] = actual_data_source
    df.attrs["cutie_central_market_data_used"] = central_market_data_used
    df.attrs["cutie_market_data_cache_hit"] = market_data_cache_hit

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
        "provider_revision": PROVIDER_REVISION,
        "central_market_data_used": None,
        "central_market_data_auth_mode": _central_market_data_auth_mode(),
        "market_data_cache_hit": False,
        "error_type": error_type,
        "error_message": error_message,
        "raw_report": {
            "market_data_provenance": {
                "provider_revision": PROVIDER_REVISION,
                "source": None,
                "central_market_data_used": None,
                "auth_mode": _central_market_data_auth_mode(),
                "cache_hit": False,
            }
        },
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
        "provider_revision": PROVIDER_REVISION,
        "provider_run_id": f"bt_{run_id}",
        "engine_name": ENGINE_NAME,
        "engine_version": _engine_version(),
        "data_source": DATA_SOURCE,
        "central_market_data_used": None,
        "central_market_data_auth_mode": _central_market_data_auth_mode(),
        "market_data_cache_hit": False,
        "error_type": error_type,
        "error_message": error_message,
        "assumptions": {},
        "limitations": limitations,
        "raw_report": {
            "market_data_provenance": {
                "provider_revision": PROVIDER_REVISION,
                "source": None,
                "central_market_data_used": None,
                "auth_mode": _central_market_data_auth_mode(),
                "cache_hit": False,
            }
        },
    })


def _artifact_capability_pair() -> Optional[tuple[dict[str, Any], str]]:
    """Build the public capability only when the deployed revision is immutable."""
    if not re.fullmatch(r"[0-9a-f]{7,64}", PROVIDER_REVISION):
        return None
    payload = capability_payload(PROVIDER_REVISION)
    return payload, capability_hash(payload)


def _artifact_failure(error: StrategyContractError) -> JSONResponse:
    """Artifact errors expose only frozen symbols and redaction-safe structure."""
    return JSONResponse(
        content={
            "result_status": "failed",
            "provider_name": PROVIDER_NAME,
            "provider_revision": PROVIDER_REVISION,
            "engine_name": "strategy-kernel",
            "engine_version": "1",
            "error_type": error.code,
            "error_message": f"{error.path}: {error.message}",
            "error_detail": error.detail(),
        }
    )


def _artifact_catalog_tool(
    supported_symbols: list[str],
    capability_pair: Optional[tuple[dict[str, Any], str]],
) -> dict[str, Any]:
    tool: dict[str, Any] = {
        "tool_id": COMPILER_TOOL_ID,
        "kind": "external_http",
        "name": "StrategySpec v2 Compiler",
        "description": "Strict declarative StrategySpec v2 compiler and deterministic kernel.",
        "wrapper_type": "python_inprocess",
        "provider_name": PROVIDER_NAME,
        "engine_name": "strategy-kernel",
        "engine_version": "1",
        "data_source": {
            "type": "provider_reported",
            "name": "cutie_central_market_data",
            "description": "Declared-only platform K-lines and feature streams.",
            "coverage_hint": "Exact artifact data requirements; no exchange fallback.",
            "external_unverified": False,
        },
        "supported_symbols": supported_symbols,
        "markets": ["spot", "futures"],
        "timeframes": ["1h", "1d"],
        "is_default": False,
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
            "result_file_patterns": [],
            "upstream_auth_local_only": True,
        },
        "param_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        },
        "output_schema": {
            "metrics": ["total_return", "max_drawdown", "trade_count"],
            "artifacts": [],
            "series": ["equity_curve"],
            "tables": ["trades"],
        },
        "report_capabilities": {
            "report_url": False,
            "scope": "none",
            "formats": [],
            "retention_hint": "immutable callback evidence",
        },
        "failure_codes": [
            "MARKET_UNSUPPORTED",
            "ERR_STRATEGY_SPEC_INVALID",
            "ERR_STRATEGY_SPEC_UNSUPPORTED",
            "ERR_STRATEGY_CAPABILITY_MISMATCH",
            "ERR_STRATEGY_COVERAGE_INCOMPLETE",
            "ERR_STRATEGY_EXECUTION_BINDING_MISMATCH",
        ],
        "security": {
            "network_scope": "openclaw_hermes_local_or_private",
            "requires_user_secret": False,
            "secrets_stay_local": True,
            "live_trading": False,
            "filesystem_paths_exposed": False,
        },
    }
    if capability_pair is not None:
        tool["strategy_execution_capability"] = capability_pair[0]
        tool["strategy_execution_capability_hash"] = capability_pair[1]
    return tool


def _canonical_kline_rows(
    ohlcv: list[Any], start_at: int, end_at: int
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candle in ohlcv:
        if not isinstance(candle, (list, tuple)) or len(candle) < 6:
            raise StrategyContractError(
                ERR_COVERAGE_INCOMPLETE,
                "$.data_streams.kline",
                "central K-line row is malformed",
            )
        open_time = int(candle[0]) // 1000
        if not start_at <= open_time < end_at:
            continue
        rows.append(
            {
                "open_time": open_time,
                "open": canonical_decimal_str(str(candle[1])),
                "high": canonical_decimal_str(str(candle[2])),
                "low": canonical_decimal_str(str(candle[3])),
                "close": canonical_decimal_str(str(candle[4])),
                "volume": canonical_decimal_str(str(candle[5])),
            }
        )
    rows.sort(key=lambda item: item["open_time"])
    if not rows or len({item["open_time"] for item in rows}) != len(rows):
        raise StrategyContractError(
            ERR_COVERAGE_INCOMPLETE,
            "$.data_streams.kline",
            "central K-line stream is empty or contains duplicate timestamps",
        )
    return rows


def _assert_contiguous(
    rows: list[dict[str, Any]], timestamp_key: str, interval: str, path: str
) -> None:
    step = _timeframe_milliseconds(interval) // 1000
    for previous, current in zip(rows, rows[1:]):
        if current[timestamp_key] - previous[timestamp_key] != step:
            raise StrategyContractError(
                ERR_COVERAGE_INCOMPLETE,
                path,
                "declared stream contains a gap",
                required=previous[timestamp_key] + step,
                actual=current[timestamp_key],
            )


def _fetch_artifact_klines(
    requirement: dict[str, Any],
    symbol: str,
    start_at: int,
    end_at: int,
) -> list[dict[str, Any]]:
    ohlcv = _fetch_from_central(
        requirement["exchange"],
        requirement["market"],
        symbol,
        requirement["interval"],
        start_at * 1000,
        end_at * 1000,
    )
    if ohlcv is None:
        raise StrategyContractError(
            ERR_COVERAGE_INCOMPLETE,
            f"$.artifact_manifest.data_requirements.{requirement['stream_id']}",
            "declared central K-line source is unavailable; fallback is forbidden",
        )
    rows = _canonical_kline_rows(ohlcv, start_at, end_at)
    _assert_contiguous(
        rows, "open_time", requirement["interval"], "$.data_streams.kline"
    )
    return rows


def _fetch_artifact_metric_chunk(
    *,
    symbol: str,
    metric: str,
    interval: str,
    exchange: str,
    start_at: int,
    end_at: int,
) -> list[dict[str, Any]]:
    if not CENTRAL_MARKET_DATA_URL or not CENTRAL_MARKET_DATA_TOKEN:
        return []
    params = {
        "symbol": re.sub(r"(?:USDT|USDC|BUSD)$", "", symbol.upper()),
        "metric": metric,
        "interval": interval,
        "exchange": exchange,
        "start_ts": start_at,
        "end_ts": end_at,
        "limit": 5000,
    }
    url = f"{CENTRAL_MARKET_DATA_URL}/metrics?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {CENTRAL_MARKET_DATA_TOKEN}",
            "Accept": "application/json",
            "User-Agent": CENTRAL_MARKET_DATA_USER_AGENT,
        },
    )
    try:
        with _CENTRAL_HTTP_OPENER.open(
            request, timeout=CENTRAL_MARKET_DATA_TIMEOUT_SEC
        ) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
        OSError,
        ValueError,
    ):
        return []
    if body.get("err_code") != 100:
        return []
    return list((body.get("data") or {}).get("items") or [])


_KLINE_PRIMARY_PREFIX = "kline.primary."


def _fetch_artifact_features(
    requirement: dict[str, Any],
    strategy_spec: dict[str, Any],
    symbol: str,
    start_at: int,
    end_at: int,
) -> list[dict[str, Any]]:
    # Matcher aligned with the kernel's own convention (build_frames,
    # _kline_primary_field_for_requirement below): prefix *and* exact
    # interval match, not prefix alone. A requirement matched by prefix only
    # could bind a StrategySpec feature declared at a different interval —
    # for a kline.primary.* requirement specifically, that would otherwise
    # fall through to the central /metrics branch below and issue a live
    # external call for a "primary.<field>" metric that structurally never
    # exists (kline.primary streams are always resampled locally from the
    # primary K-line, never fetched), instead of failing closed before any
    # I/O.
    feature = next(
        (
            item
            for item in strategy_spec["features"]
            if requirement["stream_id"].startswith(item["source_stream"] + ".")
            and item["interval"] == requirement["interval"]
        ),
        None,
    )
    if feature is None:
        if requirement["stream_id"].startswith(_KLINE_PRIMARY_PREFIX):
            raise StrategyContractError(
                ERR_COVERAGE_INCOMPLETE,
                f"$.artifact_manifest.data_requirements.{requirement['stream_id']}",
                "kline.primary requirement does not bind a feature",
            )
        raise StrategyContractError(
            ERR_COVERAGE_INCOMPLETE,
            f"$.artifact_manifest.data_requirements.{requirement['stream_id']}",
            "feature requirement does not bind a StrategySpec feature",
        )
    metric = feature["source_stream"].split(".", 1)[1]
    exchange = (
        "AGGREGATED"
        if requirement["exchange"] == "all"
        else requirement["exchange"].title()
    )
    raw_rows: list[dict[str, Any]] = []
    chunk_seconds = 90 * 24 * 60 * 60
    cursor = start_at
    while cursor < end_at:
        chunk_end = min(end_at, cursor + chunk_seconds)
        # The central /metrics API uses inclusive bounds. Map the Provider's local
        # [cursor, chunk_end) slice to an inclusive request without boundary overlap.
        raw_rows.extend(
            _fetch_artifact_metric_chunk(
                symbol=symbol,
                metric=metric,
                interval=requirement["interval"],
                exchange=exchange,
                start_at=cursor,
                end_at=chunk_end - 1,
            )
        )
        cursor = chunk_end
    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        try:
            timestamp = int(raw["ts"])
            value = canonical_decimal_str(str(raw["value"]))
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise StrategyContractError(
                ERR_COVERAGE_INCOMPLETE,
                "$.data_streams.feature",
                "central feature row is malformed",
            ) from exc
        if start_at <= timestamp < end_at:
            rows.append({"ts": timestamp, "value": value})
    rows.sort(key=lambda item: item["ts"])
    if not rows or len({item["ts"] for item in rows}) != len(rows):
        raise StrategyContractError(
            ERR_COVERAGE_INCOMPLETE,
            "$.data_streams.feature",
            "declared feature stream is empty or contains duplicate timestamps",
        )
    _assert_contiguous(rows, "ts", requirement["interval"], "$.data_streams.feature")
    revision = canonical_json_sha256(rows)
    step = _timeframe_milliseconds(requirement["interval"]) // 1000
    return [
        {
            "ts": item["ts"],
            "value": item["value"],
            "available_at": item["ts"] + step,
            "revision": revision,
        }
        for item in rows
    ]


def _kline_primary_field_for_requirement(
    requirement: dict[str, Any], strategy_spec: dict[str, Any]
) -> Optional[str]:
    """None unless ``requirement`` backs a SPEC §5.5 coarse
    ``kline.primary.<field>`` derived feature stream — resampled locally from
    the already-fetched primary K-line, never fetched from an external
    source."""
    if requirement["kind"] != "feature":
        return None
    feature = next(
        (
            item
            for item in strategy_spec["features"]
            if requirement["stream_id"].startswith(item["source_stream"] + ".")
            and item["interval"] == requirement["interval"]
        ),
        None,
    )
    if feature is None or not feature["source_stream"].startswith(
        _KLINE_PRIMARY_PREFIX
    ):
        return None
    return feature["source_stream"][len(_KLINE_PRIMARY_PREFIX) :]


def _kline_primary_resample_source_start(
    start_at: int, target_step: int
) -> int:
    """One extra target interval earlier than a coarse requirement's own
    (already bucket-aligned, see ``kline_primary_bucket_required_start``)
    declared ``start_at``, purely as a defensive margin for bucket
    construction — the extra bars this reads are never exposed to
    ``build_frames`` as primary decision frames."""
    return max(0, start_at - target_step)


def _derive_kline_primary_feature_rows(
    primary_rows: list[dict[str, Any]],
    primary_step: int,
    primary_revision: str,
    requirement: dict[str, Any],
    field: str,
    start_at: int,
    end_at: int,
) -> list[dict[str, Any]]:
    """SPEC §7.3 ``ohlcv_resample.v1``: resample the primary K-line's own
    already-fetched rows (never a separate, independent fetch — see
    ``_run_artifact_backtest``'s single union-range primary fetch, which
    widens once up front to cover every derived requirement's own
    resample-source need, so this and the decision-frame primary stream
    always read the exact same underlying data) into the requirement's
    coarse interval, then project the single requested OHLC field into the
    feature-row shape ``build_frames`` consumes.

    Unlike a directly-fetched feature stream — whose own independent fetch can
    extend further back than the primary decision-frame range so the
    earliest primary row still finds a full window, see
    ``test_multiple_features_share_one_declared_source_without_overwrite`` —
    a derived bucket has no independent history of its own: it is
    reconstructed from primary bars. ``build_frames`` requires *every* given
    row (warmup included) to resolve every required feature — the frozen
    ``rolling_sum`` contract, unchanged here — so the manifest's declared
    ``requirement.warmup_bars`` (in this requirement's own coarse-bar units)
    must be set to at least the primitive's ``window_bars`` for the very
    first decision frame to have a completed bucket to as-of anchor on: an
    as-of anchor always needs one whole *prior* bucket (the current bucket
    cannot be complete yet), one more than a same-interval feature needs.

    Every returned row's own ``revision`` is ``primary_revision`` verbatim
    (the same value ``build_coverage_manifest`` inherits onto this derived
    stream's coverage entry, see ``_is_kline_primary_derived``) rather than a
    checksum of this stream's own projected rows: the bucket *values* were
    genuinely produced from that exact primary dataset, so pinning provenance
    to it — not to a re-derived hash of the output — is what makes the
    inherited revision true rather than merely consistent.
    """
    target_step = _timeframe_milliseconds(requirement["interval"]) // 1000
    buckets = ohlcv_resample(primary_rows, primary_step, requirement["interval"])
    filtered = [
        bucket for bucket in buckets if start_at <= bucket["open_time"] < end_at
    ]
    if not filtered:
        raise StrategyContractError(
            ERR_COVERAGE_INCOMPLETE,
            f"$.artifact_manifest.data_requirements.{requirement['stream_id']}",
            "derived kline.primary feature stream is empty; the primary "
            "source does not cover the resampled requirement's declared range",
        )
    return [
        {
            "ts": bucket["open_time"],
            "value": bucket[field],
            "available_at": bucket["open_time"] + target_step,
            "revision": primary_revision,
        }
        for bucket in filtered
    ]


def _run_artifact_backtest(
    body: dict[str, Any],
    capability_pair: tuple[dict[str, Any], str],
    connector_version: Optional[str],
) -> JSONResponse:
    try:
        if not connector_version or len(connector_version) > 100:
            raise StrategyContractError(
                ERR_CAPABILITY_MISMATCH,
                "$.headers.X-Cutie-Connector-Version",
                "artifact execution requires a bounded Connector version",
            )
        validated = validate_execution_request(
            body, capability_pair[0], capability_pair[1]
        )
        request = validated.request
        params = request["execution_params"]
        state = initial_state(validated.plan, params)
        symbol = params["symbol"]
        data_streams: dict[str, list[dict[str, Any]]] = {}
        coverage_inputs: list[CoverageInput] = []
        requirements = request["artifact_manifest"]["data_requirements"]
        primary_requirement = next(
            (
                item
                for item in requirements
                if item["execution_role"] == "primary_execution_kline"
            ),
            None,
        )
        if primary_requirement is None:
            raise StrategyContractError(
                ERR_COVERAGE_INCOMPLETE, "$.data_streams", "primary K-line is missing"
            )
        # Primary is always fetched first (regardless of data_requirements
        # order), and exactly once: §5.5 coarse kline.primary.<field> feature
        # requirements below are derived from it locally via ohlcv_resample,
        # never independently fetched, so the one fetch here is widened up
        # front to a union range covering every derived requirement's own
        # resample-source need too — a second, separate fetch for derivation
        # could race a live central source against this one and resample
        # from data the decision frames never actually saw, pinning a
        # coverage revision (build_coverage_manifest's `primary_item.revision`,
        # inherited verbatim onto every derived stream) that would not be
        # true.
        primary_step_seconds = (
            _timeframe_milliseconds(primary_requirement["interval"]) // 1000
        )
        primary_warmup_start = max(
            0,
            params["start_at"] - primary_requirement["warmup_bars"] * primary_step_seconds,
        )
        primary_fetch_start = primary_warmup_start
        for requirement in requirements:
            if requirement is primary_requirement:
                continue
            if _kline_primary_field_for_requirement(
                requirement, request["strategy_spec"]
            ) is None:
                continue
            derived_step = _timeframe_milliseconds(requirement["interval"]) // 1000
            derived_warmup_start = kline_primary_bucket_required_start(
                params["start_at"], requirement["warmup_bars"], derived_step
            )
            primary_fetch_start = min(
                primary_fetch_start,
                _kline_primary_resample_source_start(derived_warmup_start, derived_step),
            )
        primary_fetch_rows = _fetch_artifact_klines(
            primary_requirement, symbol, primary_fetch_start, params["end_at"]
        )
        primary_rows = [
            row
            for row in primary_fetch_rows
            if primary_warmup_start <= row["open_time"] < params["end_at"]
        ]
        data_streams[primary_requirement["stream_id"]] = primary_rows
        # The revision every kline.primary.* derived stream inherits must
        # reflect the exact rows resampled into its buckets — the full union
        # fetch, not just the decision-frame slice — or golden_replay would
        # pin a revision that does not correspond to the real derived data
        # (§7.4 "revision/checksum 固定").
        primary_fetch_checksum = canonical_json_sha256(primary_fetch_rows)
        coverage_inputs.append(
            CoverageInput(
                requirement=primary_requirement,
                checksum=primary_fetch_checksum,
                revision=primary_fetch_checksum,
                point_count=len(primary_rows),
                actual_start_at=primary_rows[0]["open_time"],
                actual_end_at=primary_rows[-1]["open_time"],
                available_through=primary_rows[-1]["open_time"] + primary_step_seconds,
            )
        )
        for requirement in requirements:
            if requirement is primary_requirement:
                continue
            requirement_step = _timeframe_milliseconds(requirement["interval"]) // 1000
            kline_primary_field = _kline_primary_field_for_requirement(
                requirement, request["strategy_spec"]
            )
            if kline_primary_field is not None:
                # Bucket-aligned: see kline_primary_bucket_required_start.
                # An unaligned start_at - warmup_bars*step can land inside a
                # bucket a decision frame needs in full, truncating it.
                warmup_start = kline_primary_bucket_required_start(
                    params["start_at"], requirement["warmup_bars"], requirement_step
                )
            else:
                warmup_start = max(
                    0, params["start_at"] - requirement["warmup_bars"] * requirement_step
                )
            if kline_primary_field is not None:
                rows = _derive_kline_primary_feature_rows(
                    primary_fetch_rows,
                    primary_step_seconds,
                    primary_fetch_checksum,
                    requirement,
                    kline_primary_field,
                    warmup_start,
                    params["end_at"],
                )
            elif requirement["kind"] == "kline":
                rows = _fetch_artifact_klines(
                    requirement,
                    symbol,
                    warmup_start,
                    params["end_at"],
                )
            else:
                rows = _fetch_artifact_features(
                    requirement,
                    request["strategy_spec"],
                    symbol,
                    warmup_start,
                    params["end_at"],
                )
            data_streams[requirement["stream_id"]] = rows
            checksum = canonical_json_sha256(rows)
            timestamp_key = "open_time" if requirement["kind"] == "kline" else "ts"
            coverage_inputs.append(
                CoverageInput(
                    requirement=requirement,
                    checksum=checksum,
                    revision=checksum,
                    point_count=len(rows),
                    actual_start_at=rows[0][timestamp_key],
                    actual_end_at=rows[-1][timestamp_key],
                    available_through=rows[-1][timestamp_key]
                    + _timeframe_milliseconds(requirement["interval"]) // 1000,
                )
            )
        expected_source = _CENTRAL_MARKET_SOURCES.get(primary_requirement["market"])
        if primary_requirement["result_source"] != expected_source:
            raise StrategyContractError(
                ERR_COVERAGE_INCOMPLETE,
                "$.artifact_manifest.data_requirements.primary.result_source",
                "declared result source differs from the actual central adapter",
                required=expected_source,
                actual=primary_requirement["result_source"],
            )
        # SPEC §7.5: result.v2 data_manifest proves the evaluation window only;
        # warmup_bars widens the fetch (above) so the kernel has lookback
        # history, but must not widen the nine-key evidence the Connector
        # cross-checks against coverage.actual_range for the primary stream.
        primary_evaluation_rows = [
            row
            for row in primary_rows
            if params["start_at"] <= row["open_time"] < params["end_at"]
        ]
        primary_checksum = canonical_json_sha256(primary_evaluation_rows)
        data_manifest = {
            "source": primary_requirement["result_source"],
            "symbol": symbol,
            "market": params["market"],
            "timeframe": params["timeframe"],
            "start_at": params["start_at"],
            "end_at": params["end_at"],
            "kline_count": len(primary_evaluation_rows),
            "checksum_algo": "sha256",
            "checksum": primary_checksum,
        }
        coverage = build_coverage_manifest(request, coverage_inputs, data_manifest)
        frames = build_frames(data_streams, coverage, validated.plan)
        simulation = simulate(validated.plan, frames, state)
        response = build_artifact_response(
            request=request,
            simulation=simulation,
            data_manifest=data_manifest,
            coverage_manifest=coverage,
            capability=capability_pair[0],
            provider_process_fingerprint=_process_fingerprint(),
            connector_version=connector_version,
            provider_name=PROVIDER_NAME,
        )
        return JSONResponse(content=response)
    except (StrategyContractError, KernelExecutionError) as exc:
        return _artifact_failure(exc)
    except Exception:
        logger.exception("Artifact strategy execution failed")
        return _artifact_failure(
            StrategyContractError(
                ERR_COVERAGE_INCOMPLETE,
                "$.execution",
                "artifact execution failed closed",
            )
        )


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


def _cci_series(high: Any, low: Any, close: Any, period: int):
    """Commodity Channel Index as a numpy array. NaN warmup / 0-deviation -> NaN/inf,
    guarded by isfinite() in next()."""
    tp = (
        pd.Series(high, dtype="float64")
        + pd.Series(low, dtype="float64")
        + pd.Series(close, dtype="float64")
    ) / 3.0
    sma = tp.rolling(period).mean()
    mad = (tp - sma).abs().rolling(period).mean()
    return ((tp - sma) / (0.015 * mad)).to_numpy()


def _build_cci_rsi(params: dict[str, Any]) -> dict[str, Any]:
    try:
        cci_period = int(params.get("cci_period", 20))
        rsi_period = int(params.get("rsi_period", 14))
        cci_oversold = float(params.get("cci_oversold", -100))
        cci_overbought = float(params.get("cci_overbought", 100))
        rsi_oversold = float(params.get("rsi_oversold", 30))
        rsi_overbought = float(params.get("rsi_overbought", 70))
    except (ValueError, TypeError):
        raise ValueError("INVALID_PARAMS:cci/rsi params must be numbers")
    if cci_period < 2 or rsi_period < 2:
        raise ValueError("INVALID_PARAMS:cci_period and rsi_period must be >= 2")
    if cci_oversold >= cci_overbought:
        raise ValueError("INVALID_PARAMS:cci_oversold must be < cci_overbought")
    if not (0 < rsi_oversold < rsi_overbought < 100):
        raise ValueError("INVALID_PARAMS:require 0 < rsi_oversold < rsi_overbought < 100")

    from backtesting import Strategy

    class CciRsiStrategy(Strategy):
        def init(self):
            self.cci = self.I(
                lambda h, l, c: _cci_series(h, l, c, cci_period),
                self.data.High,
                self.data.Low,
                self.data.Close,
                name=f"CCI({cci_period})",
            )
            self.rsi = self.I(
                lambda c: _rsi_series(c, rsi_period),
                self.data.Close,
                name=f"RSI({rsi_period})",
            )

        def next(self):
            cci = self.cci[-1]
            rsi = self.rsi[-1]
            if not (math.isfinite(cci) and math.isfinite(rsi)):
                return
            # Dual-indicator mean-reversion, long & short (KOL 'CCI+RSI 双指标超买超卖').
            if not self.position:
                if cci < cci_oversold and rsi < rsi_oversold:
                    self.buy()
                elif cci > cci_overbought and rsi > rsi_overbought:
                    self.sell()
            elif self.position.is_long:
                if cci > 0 or rsi > 50:
                    self.position.close()
            elif self.position.is_short:
                if cci < 0 or rsi < 50:
                    self.position.close()

    return {
        "strategy": CciRsiStrategy,
        "executed_name": f"CCI+RSI ({cci_period}/{rsi_period})",
        "min_bars": max(cci_period, rsi_period) * 3 + 1,
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
            "ema_fast": {"type": "integer", "default": 20, "minimum": 2, "maximum": 100},
            "ema_slow": {"type": "integer", "default": 60, "minimum": 3, "maximum": 300},
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
            "rsi_period": {"type": "integer", "default": 14, "minimum": 2, "maximum": 100},
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
            "bb_period": {"type": "integer", "default": 20, "minimum": 2, "maximum": 200},
            "bb_std": {"type": "number", "default": 2.0, "minimum": 0.1, "maximum": 10},
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
            "bb_period": {"type": "integer", "default": 20, "minimum": 2, "maximum": 200},
            "bb_std": {"type": "number", "default": 2.0, "minimum": 0.1, "maximum": 10},
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
            "lookback": {"type": "integer", "default": 20, "minimum": 2, "maximum": 200},
            "exit_lookback": {"type": "integer", "default": 10, "minimum": 1, "maximum": 200},
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
            "fast": {"type": "integer", "default": 12, "minimum": 2, "maximum": 100},
            "slow": {"type": "integer", "default": 26, "minimum": 3, "maximum": 300},
            "signal": {"type": "integer", "default": 9, "minimum": 1, "maximum": 100},
            "exchange": {"type": "string", "default": DEFAULT_EXCHANGE},
        },
    },
    "local.backtesting_py.cci_rsi": {
        "name": "Local Backtesting.py CCI+RSI Dual",
        "description": (
            "Dual-indicator mean-reversion, long AND short: go long when CCI and RSI "
            "are both oversold, short when both overbought; exit when either reverts "
            "past its midline. Maps to KOL 'CCI+RSI 双指标同步超买超卖'."
        ),
        "strategy_family": "mean_reversion",
        "is_default": False,
        "build": _build_cci_rsi,
        "param_schema_properties": {
            "cci_period": {"type": "integer", "default": 20, "minimum": 2, "maximum": 200},
            "rsi_period": {"type": "integer", "default": 14, "minimum": 2, "maximum": 100},
            "cci_oversold": {"type": "number", "default": -100, "minimum": -500, "maximum": 0},
            "cci_overbought": {"type": "number", "default": 100, "minimum": 0, "maximum": 500},
            "rsi_oversold": {"type": "number", "default": 30, "minimum": 1, "maximum": 49},
            "rsi_overbought": {"type": "number", "default": 70, "minimum": 51, "maximum": 99},
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
        "markets": ["spot", "futures"],
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
            "metrics": [
                "total_return_pct",
                "win_rate_pct",
                "max_drawdown_pct",
                "trade_count",
                "buy_hold_return_pct",
            ],
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
            "MARKET_UNSUPPORTED",
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
# result.v2（SPEC 62-1 §2 冻结结构）：trades / equity_curve / metrics / data_manifest
# ---------------------------------------------------------------------------

def _internal_cash_dec(initial_capital: Decimal, close_max: float) -> Decimal:
    """internal_cash 的 Decimal 版本（result.v2 qty/pnl 折算比例的分母）。

    review K1-M1/C-L8：不能从 run_backtest 里那个 float internal_cash 反向
    `Decimal(str(internal_cash))`——`close_max * 100_000.0` 这一步已经在 float 精度下
    算完，Decimal 化只是把舍入后的浮点结果字符串化，不是"全程 Decimal"（示例：
    close_max=55006.58864451 时 float 乘法产出 5500658864.450999，Decimal 精确乘法是
    5500658864.451——最后三位分道扬镳，qty/pnl 折算比例会带着这份不属于 SPEC 契约
    的误差）。

    分子分母各自独立走 Decimal：`initial_capital` 是用户输入原值，本身已是精确
    Decimal（不经过 float 往返）；`close_max` 本质是 float（行情数据自带），仍按项目
    一贯做法用 str(float) 最短往返表示转 Decimal，但 *100000 这一步换成 Decimal
    精确乘法（100000 是 10 的整数次幂，Decimal 乘法不产生任何舍入）。
    """
    return max(initial_capital, Decimal(str(close_max)) * Decimal("100000"))


def _data_manifest_source(market: str, exchange_id: str, df: pd.DataFrame) -> str:
    """result.v2 data_manifest.source：本次请求的真实数据出处（不是路由判据猜测）。

    _fetch_ohlcv 已把真实来源记在 df.attrs["cutie_central_market_data_used"]（central
    命中 / ccxt 回退 / 磁盘缓存复用时同样带着原始出处，_read_cache 的 provenance dict
    格式），这里只做 SPEC 命名映射：中心命中 → cutie-server 同款 binance_us/
    binance_futures；否则老实标 ccxt:<exchange_id>，不冒充中心数据（server 的身份核对
    还会用真实 checksum 内容兜底一次，标错也只是让本可升级的 run 误判
    evidence_mismatch，不会产生假阳性升级）。
    """
    if df.attrs.get("cutie_central_market_data_used") is True:
        return _CENTRAL_MARKET_SOURCES.get(market, f"ccxt:{exchange_id}")
    return f"ccxt:{exchange_id}"


def _kline_rows_to_canonical(df: pd.DataFrame, start_at: int, end_at: int) -> list[dict[str, Any]]:
    """K 线 checksum 输入（SPEC §2）：open_time 升序，每根仅含
    {open_time,open,high,low,close,volume}，OHLCV 为规范 Decimal 字符串（float 最短
    往返表示转 Decimal，与 cutie-server _kline_rows_to_canonical 同规则，跨语言可比）。

    开区间防御：只收 open_time ∈ [start_at, end_at] 双闭区间内的行——上游 ccxt/中心 API
    理论上已只返回该窗口，这里再显式收紧一次，保证与 SPEC §2 "K 线区间语义" 严格一致，
    不依赖上游实现细节。df.index 是 pandas DatetimeIndex（ms 精度整数对齐），用
    Timestamp.value（int64 纳秒）整除取秒，避免 .timestamp() 的浮点往返误差。
    """
    rows: list[dict[str, Any]] = []
    for idx, row in df.iterrows():
        open_time = int(idx.value // 10**9)
        if open_time < start_at or open_time > end_at:
            continue
        rows.append({
            "open_time": open_time,
            "open": canonical_decimal_str(str(float(row["Open"]))),
            "high": canonical_decimal_str(str(float(row["High"]))),
            "low": canonical_decimal_str(str(float(row["Low"]))),
            "close": canonical_decimal_str(str(float(row["Close"]))),
            "volume": canonical_decimal_str(str(float(row["Volume"]))),
        })
    return rows


def _build_result_v2_trades(
    stats_trades: Any,
    equity_scale_dec: Decimal,
    fee_bps: Decimal,
    slippage_bps: Decimal,
) -> list[dict[str, Any]]:
    """result.v2 trades（SPEC §2 冻结 10 键）：费用/滑点/pnl 按 SPEC 公式重算，不采用
    backtesting.py 自身的 commission 记账（它只记了 fee，没有 slippage 概念，且以
    Trade._commissions 形式单独存放）。

    entry_price/exit_price 取 backtesting.py 记录的原始成交价（Trade.entry_price/
    exit_price 本身不含 commission 调整——commission 单独记在 Trade._commissions,
    见 backtesting.py Trade.pl 实现），即 SPEC 要求的"扣滑点前、用于 K 线区间校验的
    参考成交价"。qty 是内部放大 cash 后的 Trade.size（整数份额）按 equity_scale_dec 折回
    用户真实资金规模的份额，见 run_backtest 里 internal_cash 放大的注释。

    fee_bps/slippage_bps 除以 10000 是幂等精确除法（10000=2^4*5^4，Decimal 不产生
    有效数字损失）；全程 Decimal、不做中间舍入，只在最终 canonical_decimal_str 时格式化。
    """
    if stats_trades is None or len(stats_trades) == 0:
        return []

    raw: list[dict[str, Any]] = []
    for _, trade in stats_trades.iterrows():
        entry_time = trade.get("EntryTime")
        exit_time = trade.get("ExitTime")
        if not hasattr(entry_time, "value") or not hasattr(exit_time, "value"):
            continue  # finalize_trades=True 下不应出现未平仓行，防御性跳过
        opened_at = int(entry_time.value // 10**9)
        closed_at = int(exit_time.value // 10**9)
        size = trade.get("Size", 0) or 0
        side = "long" if size > 0 else "short"
        entry_price_dec = Decimal(str(float(trade.get("EntryPrice"))))
        exit_price_dec = Decimal(str(float(trade.get("ExitPrice"))))
        qty_dec = Decimal(abs(int(size))) * equity_scale_dec
        fee_dec = (entry_price_dec + exit_price_dec) * qty_dec * fee_bps / Decimal(10000)
        slippage_dec = (entry_price_dec + exit_price_dec) * qty_dec * slippage_bps / Decimal(10000)
        if side == "long":
            gross_dec = (exit_price_dec - entry_price_dec) * qty_dec
        else:
            gross_dec = (entry_price_dec - exit_price_dec) * qty_dec
        pnl_dec = gross_dec - fee_dec - slippage_dec
        raw.append({
            "opened_at": opened_at,
            "closed_at": closed_at,
            "side": side,
            "qty": qty_dec,
            "entry_price": entry_price_dec,
            "exit_price": exit_price_dec,
            "fee": fee_dec,
            "slippage": slippage_dec,
            "pnl": pnl_dec,
        })

    # seq 从 1 连续递增，trades 按 seq 排序（SPEC §2）：先按 closed_at/opened_at 排定序。
    raw.sort(key=lambda t: (t["closed_at"], t["opened_at"]))
    trades: list[dict[str, Any]] = []
    for idx, t in enumerate(raw, start=1):
        trades.append({
            "seq": idx,
            "opened_at": t["opened_at"],
            "closed_at": t["closed_at"],
            "side": t["side"],
            "qty": canonical_decimal_str(t["qty"]),
            "entry_price": canonical_decimal_str(t["entry_price"]),
            "exit_price": canonical_decimal_str(t["exit_price"]),
            "fee": canonical_decimal_str(t["fee"]),
            "slippage": canonical_decimal_str(t["slippage"]),
            "pnl": canonical_decimal_str(t["pnl"]),
        })
    return trades


def _build_result_v2_equity_curve(
    trades_v2: list[dict[str, Any]],
    initial_capital: Decimal,
    start_at: int,
) -> list[dict[str, Any]]:
    """result.v2 equity_curve（SPEC §2）：以 start_at/initial_capital 起点开头，每笔
    trade 的 closed_at 追加计入该笔净 pnl 后的点；无交易时仍保留初始点。
    """
    curve = [{"ts": start_at, "equity": canonical_decimal_str(initial_capital)}]
    running = initial_capital
    for t in trades_v2:
        running = running + Decimal(t["pnl"])
        curve.append({"ts": t["closed_at"], "equity": canonical_decimal_str(running)})
    return curve


def _result_v2_max_drawdown(equity_curve: list[dict[str, Any]]) -> Decimal:
    """峰谷比例回撤：跟踪运行峰值，逐点取 (peak-equity)/peak 的最大值（SPEC §2）。"""
    peak: Optional[Decimal] = None
    max_dd = Decimal(0)
    for point in equity_curve:
        equity = Decimal(point["equity"])
        if peak is None or equity > peak:
            peak = equity
        if peak:
            dd = (peak - equity) / peak
            if dd > max_dd:
                max_dd = dd
    return max_dd


def _build_result_v2(
    *,
    stats_trades: Any,
    equity_scale_dec: Decimal,
    fee_bps: Decimal,
    slippage_bps: Decimal,
    initial_capital: Decimal,
    start_at: int,
    end_at: int,
    symbol: str,
    market: str,
    timeframe: str,
    exchange_id: str,
    df: pd.DataFrame,
) -> dict[str, Any]:
    """组装 SPEC §2 冻结的 result.v2 五键：schema_version/trades/equity_curve/metrics/
    data_manifest。metrics 恰好三键（total_return/max_drawdown/trade_count）——server
    端 _validate_result_v2 对多余键判 evidence_mismatch，不得在此加展示性字段。
    """
    trades_v2 = _build_result_v2_trades(stats_trades, equity_scale_dec, fee_bps, slippage_bps)
    equity_curve_v2 = _build_result_v2_equity_curve(trades_v2, initial_capital, start_at)
    final_equity = Decimal(equity_curve_v2[-1]["equity"])
    total_return = (final_equity - initial_capital) / initial_capital
    max_drawdown = _result_v2_max_drawdown(equity_curve_v2)

    kline_rows = _kline_rows_to_canonical(df, start_at, end_at)
    checksum = canonical_json_sha256(kline_rows)

    return {
        "schema_version": RESULT_V2_SCHEMA,
        "trades": trades_v2,
        "equity_curve": equity_curve_v2,
        "metrics": {
            "total_return": canonical_decimal_str(total_return),
            "max_drawdown": canonical_decimal_str(max_drawdown),
            "trade_count": len(trades_v2),
        },
        "data_manifest": {
            "source": _data_manifest_source(market, exchange_id, df),
            "symbol": symbol,
            "market": market,
            "timeframe": timeframe,
            "start_at": start_at,
            "end_at": end_at,
            "kline_count": len(kline_rows),
            "checksum_algo": "sha256",
            "checksum": checksum,
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

    capability_pair = _artifact_capability_pair()
    if ok:
        central_health = _central_health_snapshot()
        return JSONResponse(
            {
                "ok": True,
                "provider_id": PROVIDER_ID,
                "provider_revision": PROVIDER_REVISION,
                "process_fingerprint": _process_fingerprint(),
                "engine_name": ENGINE_NAME,
                "engine_version": _engine_version(),
                "data_ready": data_ready,
                "central_market_data_configured": bool(
                    CENTRAL_MARKET_DATA_URL and CENTRAL_MARKET_DATA_TOKEN
                ),
                "central_market_data_auth_mode": _central_market_data_auth_mode(),
                "strategy_execution_capability_available": capability_pair is not None,
                **central_health,
                "checked_at": int(time.time()),
            }
        )
    else:
        return JSONResponse(
            {
                "ok": False,
                "error_type": "DEPENDENCY_CHECK_FAILED",
                "error_message": f"Failed checks: {checks}",
            }
        )


@app.get("/catalog")
async def catalog(authorization: Optional[str] = Header(default=None)):
    """Return provider tool catalog (IMPL §5.1 cutie.backtest_provider_catalog.v1)."""
    _verify_bearer(authorization)
    supported_symbols = _supported_symbols()

    capability_pair = _artifact_capability_pair()
    return JSONResponse(
        {
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
            ]
            + (
                [_artifact_catalog_tool(supported_symbols, capability_pair)]
                if capability_pair is not None
                else []
            ),
        }
    )


@app.post("/cutie/backtest")
async def run_backtest(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_cutie_connector_version: Optional[str] = Header(default=None),
):
    """Execute backtest (IMPL §5.4 / §5.5)."""
    _verify_bearer(authorization)

    try:
        body = await request.json()
    except Exception:
        return _validation_failure(
            "INVALID_REQUEST", "Request body must be valid JSON", status_code=400
        )

    if is_strategy_execution_intent(body):
        pair = _artifact_capability_pair()
        if pair is None:
            return _artifact_failure(
                StrategyContractError(
                    ERR_CAPABILITY_MISMATCH,
                    "$.provider_revision",
                    "artifact capability is unavailable until an immutable revision is configured",
                )
            )
        return _run_artifact_backtest(body, pair, x_cutie_connector_version)

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

    # --- Validate market ---
    if market not in ("spot", "futures"):
        return _validation_failure(
            "MARKET_UNSUPPORTED",
            f"Unsupported market: {market}. Supported: ['spot', 'futures']",
        )

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
        df = _fetch_ohlcv(exchange_id, market, symbol, timeframe, start_at, end_at)
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
        # result.v2（SPEC §2）的 qty/pnl 公式全程 Decimal，折回用户真实资金规模的比例；
        # 传给 Backtest() 引擎的 internal_cash 仍是上面那个 float（引擎内部本来就是
        # float，不受此契约约束），两条路径分道扬镳，互不干扰。
        internal_cash_dec = _internal_cash_dec(initial_capital, float(df["Close"].max()))
        equity_scale_dec = initial_capital / internal_cash_dec
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
        # 62-1 result.v2（SPEC §2 冻结结构）：trades/equity_curve/metrics/data_manifest
        # 的权威形状，取代旧版自由格式 trades/equity_curve。旧展示性百分比指标全部移入
        # raw_report.legacy_metrics，不再留在顶层 metrics（server 端 _validate_result_v2
        # 对 metrics 做"恰好三键"严格校验，多一个键就判 evidence_mismatch）。
        result_v2 = _build_result_v2(
            stats_trades=getattr(stats, "_trades", None),
            equity_scale_dec=equity_scale_dec,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            initial_capital=initial_capital,
            start_at=start_at,
            end_at=end_at,
            symbol=symbol,
            market=market,
            timeframe=timeframe,
            exchange_id=exchange_id,
            df=df,
        )

        total_return_pct = _safe_float(stats, "Return [%]", 0.0)
        win_rate_pct = _safe_float(stats, "Win Rate [%]", 0.0)
        max_drawdown_pct = abs(_safe_float(stats, "Max. Drawdown [%]", 0.0))
        trade_count = _safe_int(stats, "# Trades", 0)
        # X3: backtesting.py 内置的"买入持有"对照基准，回答"策略到底有没有跑赢直接拿着"。
        buy_hold_return_pct = _safe_float(stats, "Buy & Hold Return [%]", 0.0)

        legacy_metrics = {
            "total_return_pct": round(total_return_pct, 2),
            "win_rate_pct": round(win_rate_pct, 2),
            "max_drawdown_pct": round(max_drawdown_pct, 2),
            "trade_count": trade_count,
            "buy_hold_return_pct": round(buy_hold_return_pct, 2),
        }

        legacy_metrics_json = json.dumps(legacy_metrics, sort_keys=True)
        result_hash = f"sha256:{hashlib.sha256(legacy_metrics_json.encode()).hexdigest()}"

        candle_count = len(df)
        if candle_count < 100:
            sample_size = "small"
        elif candle_count < 1000:
            sample_size = "medium"
        else:
            sample_size = "large"

        report_url = f"reports/{report_filename}"

        provider_summary = (
            f"{executed_name} on {symbol} {timeframe} ({market}), "
            f"{df.attrs.get('cutie_data_source', DATA_SOURCE)}, {candle_count} candles, "
            f"{trade_count} trades, return {total_return_pct:.2f}%"
        )
        if market == "futures":
            provider_summary += "; funding rate not included in PnL"
        strategy_assumptions, strategy_limitations, strategy_raw_report = _strategy_semantics(
            body,
            executed_name,
        )

        return JSONResponse(content=_json_safe({
            "schema": RESPONSE_SCHEMA,
            "result_status": "success",
            "provider_name": PROVIDER_NAME,
            "provider_revision": PROVIDER_REVISION,
            "provider_run_id": f"bt_{run_id}",
            "engine_name": ENGINE_NAME,
            "engine_version": _engine_version(),
            "data_source": df.attrs.get("cutie_data_source", DATA_SOURCE),
            "central_market_data_used": df.attrs.get("cutie_central_market_data_used"),
            "central_market_data_auth_mode": _central_market_data_auth_mode(),
            "market_data_cache_hit": bool(df.attrs.get("cutie_market_data_cache_hit", False)),
            "result_hash": result_hash,
            "report_url": report_url,
            "report_url_scope": "local_machine_only",
            "schema_version": result_v2["schema_version"],
            "metrics": result_v2["metrics"],
            "initial_capital": _decimal_str(initial_capital, places=2),
            "equity_curve": result_v2["equity_curve"],
            "trades": result_v2["trades"],
            "data_manifest": result_v2["data_manifest"],
            "assumptions": {
                "fee_bps": _decimal_str(fee_bps, places=4),
                "slippage_bps": _decimal_str(slippage_bps, places=4),
                "exchange": exchange_id,
                "market": market,
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
                **(
                    {
                        "funding_rate_included": False,
                        "funding_rate_note": (
                            "Futures backtest excludes perpetual funding rate "
                            "costs; PnL may be optimistic vs. live futures trading."
                        ),
                    }
                    if market == "futures"
                    else {}
                ),
            },
            "raw_report": {
                "provider_summary": provider_summary,
                "strategy_semantics": strategy_raw_report,
                # 旧版展示性百分比指标（result.v2 迁移前的 metrics 形状），保留兼容旧
                # 消费方；顶层 metrics 现在是 SPEC §2 冻结的三键结构，见上方 result_v2。
                "legacy_metrics": legacy_metrics,
                "market_data_provenance": {
                    "provider_revision": PROVIDER_REVISION,
                    "source": df.attrs.get("cutie_data_source", DATA_SOURCE),
                    "central_market_data_used": df.attrs.get("cutie_central_market_data_used"),
                    "auth_mode": _central_market_data_auth_mode(),
                    "cache_hit": bool(df.attrs.get("cutie_market_data_cache_hit", False)),
                },
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
