"""WS-7 Step 7.3：provider 中心行情数据优先级 + ccxt 回退 + 缓存 LRU 上限测试。

覆盖：
1. 未配置中心 API：直接走 ccxt（向后兼容，不发中心请求）
2. 中心 API 超时/网络错误：回退 ccxt
3. 中心 API 5xx：回退 ccxt
4. 中心 API 返回数据缺口：回退 ccxt
5. 中心 API 命中：跳过 ccxt，直接用中心数据（spot + futures，62-1 F1）
6. exchange/market 不在中心缓存覆盖范围（非 binance spot/futures）：跳过中心请求，直接 ccxt
7. 缓存 LRU 上限：超过 MAX_CACHE_FILES 时删最旧文件
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cutie_backtesting_provider as provider  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(provider, "CACHE_DIR", tmp_path / "cache" / "ohlcv")
    monkeypatch.setattr(provider, "_CENTRAL_FETCH_SUCCESS_COUNT", 0)
    monkeypatch.setattr(provider, "_CENTRAL_LAST_SUCCESS_AT", 0)
    return tmp_path


@pytest.fixture
def central_configured(monkeypatch):
    monkeypatch.setattr(provider, "CENTRAL_MARKET_DATA_URL", "https://server.example.com/v1/internal/market-data")
    monkeypatch.setattr(provider, "CENTRAL_MARKET_DATA_TOKEN", "test-market-data-token")


def test_central_redirect_handler_refuses_redirect():
    handler = provider._NoRedirectHandler()
    request = urllib.request.Request("https://server.example.com/v1/internal/market-data/klines")
    redirected = handler.redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://redirect.example.net/capture",
    )
    assert redirected is None


def test_central_not_configured_skips_request_returns_none():
    """未配置 URL/token：_fetch_from_central 直接返回 None，不发任何请求。"""
    result = provider._fetch_from_central("binance", "spot", "BTCUSDT", "1h", 0, 3600_000)
    assert result is None


def test_unsupported_exchange_skips_central_request(central_configured, monkeypatch):
    """exchange != binance：不在中心缓存范围内，跳过请求（不是失败，是范围外）。"""
    called = {"count": 0}

    def fake_urlopen(*_a, **_kw):
        called["count"] += 1
        raise AssertionError("should not call central API for non-binance exchange")

    monkeypatch.setattr(provider._CENTRAL_HTTP_OPENER, "open", fake_urlopen)
    result = provider._fetch_from_central("okx", "spot", "BTCUSDT", "1h", 0, 3600_000)
    assert result is None
    assert called["count"] == 0


def test_market_outside_central_coverage_skips_request(central_configured, monkeypatch):
    """spot/futures 之外的 market 值（provider 侧不该出现，但 _fetch_from_central 自身
    的覆盖范围判定要独立兜住）：跳过请求，不是失败。"""

    def fake_urlopen(*_a, **_kw):
        raise AssertionError("should not call central API for a market outside coverage")

    monkeypatch.setattr(provider._CENTRAL_HTTP_OPENER, "open", fake_urlopen)
    result = provider._fetch_from_central("binance", "margin", "BTCUSDT", "1h", 0, 3600_000)
    assert result is None


def test_futures_market_calls_central_when_configured(central_configured, monkeypatch):
    """62-1 F1：futures（Binance USDT 永续）现已纳入中心缓存覆盖范围，
    quote 提取要正确剥掉 _normalize_ohlcv_symbol 给 futures 加的 ":SETTLE" 后缀
    （BTC/USDT:USDT -> quote=USDT，不是 USDT:USDT），否则会被误判非 USDT 计价对。
    """
    items = [{"open_time": 0, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100.0}]
    seen_params = {}

    def fake_urlopen(request, **_kw):
        seen_params.update(urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query))
        return _FakeResponse({"err_code": 100, "data": {"available": True, "count": 1, "items": items}})

    monkeypatch.setattr(provider._CENTRAL_HTTP_OPENER, "open", fake_urlopen)
    result = provider._fetch_from_central("binance", "futures", "BTCUSDT", "1h", 0, 3600_000)
    assert result is not None
    assert len(result) == 1
    # 中心 API 只认裸 symbol（不含 /:SETTLE），market 原样透传为 "futures"。
    assert seen_params["symbol"] == ["BTC"]
    assert seen_params["market"] == ["futures"]


def test_futures_non_usdt_quote_still_skips_central_request(central_configured, monkeypatch):
    """futures 扩展不应连带放松 USDT-only 校验：非 USDT 计价对（如 ETH/BTC 永续）
    仍应回退 ccxt，不冒充 USDT 永续查中心缓存。"""

    def fake_urlopen(*_a, **_kw):
        raise AssertionError("should not call central API for non-USDT futures quote")

    monkeypatch.setattr(provider._CENTRAL_HTTP_OPENER, "open", fake_urlopen)
    result = provider._fetch_from_central("binance", "futures", "ETHBTC", "1h", 0, 3600_000)
    assert result is None


def test_non_usdt_quote_skips_central_request(central_configured, monkeypatch):
    """HIGH-3 回归：非 USDT 计价对（如 ETHBTC）不应走中心 API——中心 API 内部一律映射成
    USDT pair，若请求方要的是 ETH/BTC，中心 API 会静默返回 ETHUSDT 数据，脏数据无告警。
    """
    called = {"count": 0}

    def fake_urlopen(*_a, **_kw):
        called["count"] += 1
        raise AssertionError("should not call central API for non-USDT quote symbol")

    monkeypatch.setattr(provider._CENTRAL_HTTP_OPENER, "open", fake_urlopen)
    result = provider._fetch_from_central("binance", "spot", "ETHBTC", "1h", 0, 3600_000)
    assert result is None
    assert called["count"] == 0


def test_non_usdt_quote_with_slash_skips_central_request(central_configured, monkeypatch):
    """同上，覆盖已经带 '/' 的输入形式（BTC/USDC）。"""

    def fake_urlopen(*_a, **_kw):
        raise AssertionError("should not call central API for BTC/USDC")

    monkeypatch.setattr(provider._CENTRAL_HTTP_OPENER, "open", fake_urlopen)
    result = provider._fetch_from_central("binance", "spot", "BTC/USDC", "1h", 0, 3600_000)
    assert result is None


def test_usdt_quote_symbol_does_call_central(central_configured, monkeypatch):
    """USDT 计价对（如 ETHUSDT）应该正常走中心 API（确认 HIGH-3 修复没有连带把正常路径也堵死）。"""
    items = [{"open_time": 0, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100.0}]

    def fake_urlopen(*_a, **_kw):
        return _FakeResponse({"err_code": 100, "data": {"available": True, "count": 1, "items": items}})

    monkeypatch.setattr(provider._CENTRAL_HTTP_OPENER, "open", fake_urlopen)
    result = provider._fetch_from_central("binance", "spot", "ETHUSDT", "1h", 0, 3600_000)
    assert result is not None
    assert len(result) == 1


def test_central_timeout_returns_none(central_configured, monkeypatch):
    def fake_urlopen(*_a, **_kw):
        raise TimeoutError("timed out")

    monkeypatch.setattr(provider._CENTRAL_HTTP_OPENER, "open", fake_urlopen)
    result = provider._fetch_from_central("binance", "spot", "BTCUSDT", "1h", 0, 3600_000)
    assert result is None


def test_central_5xx_returns_none(central_configured, monkeypatch):
    def fake_urlopen(*_a, **_kw):
        raise urllib.error.HTTPError("url", 503, "Service Unavailable", {}, None)

    monkeypatch.setattr(provider._CENTRAL_HTTP_OPENER, "open", fake_urlopen)
    result = provider._fetch_from_central("binance", "spot", "BTCUSDT", "1h", 0, 3600_000)
    assert result is None


class _FakeResponse:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def test_central_data_gap_returns_none(central_configured, monkeypatch):
    """返回条数远少于预期（如 1h 区间 100 根只给 1 根）：视为缺口，回退 ccxt。"""

    def fake_urlopen(*_a, **_kw):
        return _FakeResponse(
            {
                "err_code": 100,
                "data": {
                    "available": True,
                    "count": 1,
                    "items": [{"open_time": 0, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}],
                },
            }
        )

    monkeypatch.setattr(provider._CENTRAL_HTTP_OPENER, "open", fake_urlopen)
    # 100 小时区间，理应有 ~100 根 1h K 线
    result = provider._fetch_from_central("binance", "spot", "BTCUSDT", "1h", 0, 100 * 3600 * 1000)
    assert result is None


def test_central_success_uses_bearer_and_records_canary_evidence(central_configured, monkeypatch):
    items = [
        {"open_time": i * 3600, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100.0} for i in range(3)
    ]

    seen_headers = {}

    def fake_urlopen(request, **_kw):
        seen_headers.update(dict(request.header_items()))
        return _FakeResponse({"err_code": 100, "data": {"available": True, "count": 3, "items": items}})

    monkeypatch.setattr(provider._CENTRAL_HTTP_OPENER, "open", fake_urlopen)
    result = provider._fetch_from_central("binance", "spot", "BTCUSDT", "1h", 0, 3 * 3600 * 1000)
    assert result is not None
    assert len(result) == 3
    assert result[0] == [0, 1.0, 2.0, 0.5, 1.5, 100.0]
    assert seen_headers["Authorization"] == "Bearer test-market-data-token"
    assert seen_headers["User-agent"] == provider.CENTRAL_MARKET_DATA_USER_AGENT
    assert "x-internal-key" not in {name.lower() for name in seen_headers}
    assert provider._central_health_snapshot()["central_fetch_success_count"] == 1
    assert provider._central_health_snapshot()["central_last_success_at"] > 0


def test_health_exposes_only_non_sensitive_central_evidence(central_configured, monkeypatch):
    monkeypatch.setattr(provider, "PROVIDER_REVISION", "0123456789abcdef")
    monkeypatch.setattr(provider, "_CENTRAL_FETCH_SUCCESS_COUNT", 2)
    monkeypatch.setattr(provider, "_CENTRAL_LAST_SUCCESS_AT", 1_700_000_000)

    response = asyncio.run(provider.health())
    body = json.loads(response.body)

    assert body["provider_revision"] == "0123456789abcdef"
    assert body["central_market_data_configured"] is True
    assert body["central_market_data_auth_mode"] == "market_data_bearer"
    assert body["central_fetch_success_count"] == 2
    assert body["central_last_success_at"] == 1_700_000_000
    serialized = response.body.decode("utf-8")
    assert provider.CENTRAL_MARKET_DATA_URL not in serialized
    assert provider.CENTRAL_MARKET_DATA_TOKEN not in serialized


def test_long_range_chunks_and_concatenates(central_configured, monkeypatch):
    """F7 回归（Kimi review）：server 单次请求跨度上限 90 天，回测区间可达 365 天——
    此前整段区间打一次请求会被 server 参数校验直接拒绝，长区间回测中心缓存零命中。
    改为按 CENTRAL_MAX_CHUNK_MS 分片请求再拼接，验证 200 天区间产生 3 次分片请求
    （90+90+20）且结果正确拼接。
    """
    monkeypatch.setattr(provider, "_expected_bar_count", lambda *_a, **_kw: 0)  # 聚焦分片逻辑，跳过缺口检测

    requested_ranges: list[tuple[int, int]] = []

    def fake_urlopen(req, timeout=None):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)
        start_ts = int(qs["start_ts"][0])
        end_ts = int(qs["end_ts"][0])
        requested_ranges.append((start_ts, end_ts))
        item = {"open_time": start_ts, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100.0}
        return _FakeResponse({"err_code": 100, "data": {"available": True, "count": 1, "items": [item]}})

    monkeypatch.setattr(provider._CENTRAL_HTTP_OPENER, "open", fake_urlopen)

    total_ms = 200 * 24 * 3600 * 1000  # 200 天，超过 90 天上限
    result = provider._fetch_from_central("binance", "spot", "BTCUSDT", "1d", 0, total_ms)

    assert result is not None
    assert len(requested_ranges) == 3  # 90 + 90 + 20 天 = 3 个分片
    assert len(result) == 3
    # 每个分片跨度不超过 90 天（server 上限）
    for start_ts, end_ts in requested_ranges:
        assert end_ts - start_ts <= 90 * 24 * 3600


def test_chunk_failure_aborts_whole_request_falls_back_to_ccxt(central_configured, monkeypatch):
    """任一分片失败/缺口即整体回退 ccxt，不拼"前半段中心 + 后半段 ccxt"的混合数据源。"""
    monkeypatch.setattr(provider, "_expected_bar_count", lambda *_a, **_kw: 0)

    call_count = {"n": 0}

    def fake_urlopen(req, timeout=None):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise TimeoutError("simulated timeout on second chunk")
        item = {"open_time": 0, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100.0}
        return _FakeResponse({"err_code": 100, "data": {"available": True, "count": 1, "items": [item]}})

    monkeypatch.setattr(provider._CENTRAL_HTTP_OPENER, "open", fake_urlopen)

    total_ms = 200 * 24 * 3600 * 1000
    result = provider._fetch_from_central("binance", "spot", "BTCUSDT", "1d", 0, total_ms)

    assert result is None  # 第二个分片失败 → 整体 None，触发上层回退 ccxt
    assert call_count["n"] == 2  # 第三个分片不应再被请求（提前中止）


def test_central_available_false_returns_none(central_configured, monkeypatch):
    """server 侧对不支持组合返回 available=false（非报错），provider 视同回退信号。"""

    def fake_urlopen(*_a, **_kw):
        return _FakeResponse({"err_code": 100, "data": {"available": False, "count": 0, "items": []}})

    monkeypatch.setattr(provider._CENTRAL_HTTP_OPENER, "open", fake_urlopen)
    result = provider._fetch_from_central("binance", "spot", "BTCUSDT", "1h", 0, 3600_000)
    assert result is None


def test_fetch_ohlcv_falls_back_to_ccxt_when_central_fails(central_configured, monkeypatch):
    """端到端：central 超时 → _fetch_ohlcv 仍能通过 ccxt 拿到数据（故障注入验证）。"""

    def fake_urlopen(*_a, **_kw):
        raise TimeoutError("timed out")

    monkeypatch.setattr(provider._CENTRAL_HTTP_OPENER, "open", fake_urlopen)

    fake_ccxt = MagicMock()
    fake_exchange_instance = MagicMock()
    fake_exchange_instance.fetch_ohlcv.return_value = [
        [0, 1.0, 2.0, 0.5, 1.5, 100.0],
        [3600_000, 1.5, 2.5, 1.0, 2.0, 200.0],
    ]
    fake_ccxt.binance = MagicMock(return_value=fake_exchange_instance)

    class _FakeBadSymbol(Exception):
        pass

    class _FakeRateLimitExceeded(Exception):
        pass

    class _FakeNetworkError(Exception):
        pass

    fake_ccxt.BadSymbol = _FakeBadSymbol
    fake_ccxt.RateLimitExceeded = _FakeRateLimitExceeded
    fake_ccxt.NetworkError = _FakeNetworkError

    monkeypatch.setitem(sys.modules, "ccxt", fake_ccxt)

    df = provider._fetch_ohlcv("binance", "spot", "BTCUSDT", "1h", 0, 7200)
    assert len(df) == 2
    assert df["Close"].iloc[0] == 1.5
    assert df.attrs["cutie_data_source"] == provider.DATA_SOURCE
    assert df.attrs["cutie_central_market_data_used"] is False
    assert df.attrs["cutie_market_data_cache_hit"] is False


def test_fetch_ohlcv_uses_central_and_skips_ccxt(central_configured, monkeypatch):
    """端到端：central 命中时不调用 ccxt。"""
    items = [{"open_time": 0, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100.0}]

    def fake_urlopen(*_a, **_kw):
        return _FakeResponse({"err_code": 100, "data": {"available": True, "count": 1, "items": items}})

    monkeypatch.setattr(provider._CENTRAL_HTTP_OPENER, "open", fake_urlopen)

    fake_ccxt = MagicMock()

    def _fail_if_called(*_a, **_kw):
        raise AssertionError("ccxt should not be called when central hits")

    fake_ccxt.binance = _fail_if_called
    monkeypatch.setitem(sys.modules, "ccxt", fake_ccxt)

    df = provider._fetch_ohlcv("binance", "spot", "BTCUSDT", "1h", 0, 3600)
    assert len(df) == 1
    assert df.attrs["cutie_data_source"] == "cutie_central_market_data"
    assert df.attrs["cutie_central_market_data_used"] is True
    assert df.attrs["cutie_market_data_cache_hit"] is False

    cached_df = provider._fetch_ohlcv("binance", "spot", "BTCUSDT", "1h", 0, 3600)
    assert cached_df.attrs["cutie_data_source"] == "cutie_central_market_data"
    assert cached_df.attrs["cutie_central_market_data_used"] is True
    assert cached_df.attrs["cutie_market_data_cache_hit"] is True
    assert provider._central_health_snapshot()["central_fetch_success_count"] == 1


def test_fetch_ohlcv_futures_central_hit_and_cache_isolated_from_spot(central_configured, monkeypatch):
    """62-1 F1 端到端：futures 中心命中（跳过 ccxt）+ 同 symbol/timeframe/窗口的
    spot/futures 磁盘缓存互不串扰——cache_key 含 market 维度，缓存文件必须分开，
    重复读取各自读回自己的数据，不会把 futures 价格喂成 spot 的（或反之）。
    """
    def fake_urlopen(request, **_kw):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
        market = qs["market"][0]
        close = 111.0 if market == "futures" else 100.0
        item = {"open_time": 0, "open": close, "high": close, "low": close, "close": close, "volume": 1.0}
        return _FakeResponse({"err_code": 100, "data": {"available": True, "count": 1, "items": [item]}})

    monkeypatch.setattr(provider._CENTRAL_HTTP_OPENER, "open", fake_urlopen)

    fake_ccxt = MagicMock()

    def _fail_if_called(*_a, **_kw):
        raise AssertionError("ccxt should not be called when central hits (spot or futures)")

    fake_ccxt.binance = _fail_if_called
    monkeypatch.setitem(sys.modules, "ccxt", fake_ccxt)

    spot_df = provider._fetch_ohlcv("binance", "spot", "BTCUSDT", "1h", 0, 3600)
    futures_df = provider._fetch_ohlcv("binance", "futures", "BTCUSDT", "1h", 0, 3600)

    assert spot_df.attrs["cutie_central_market_data_used"] is True
    assert futures_df.attrs["cutie_central_market_data_used"] is True
    assert spot_df["Close"].iloc[0] == 100.0
    assert futures_df["Close"].iloc[0] == 111.0

    spot_key = provider._cache_key("binance", "spot", "BTCUSDT", "1h", 0, 3600_000)
    futures_key = provider._cache_key("binance", "futures", "BTCUSDT", "1h", 0, 3600_000)
    assert spot_key != futures_key
    assert (provider.CACHE_DIR / spot_key).exists()
    assert (provider.CACHE_DIR / futures_key).exists()

    # 二次读取（走缓存）：各自读回自己的价格，缓存没有串市场。
    spot_cached = provider._fetch_ohlcv("binance", "spot", "BTCUSDT", "1h", 0, 3600)
    futures_cached = provider._fetch_ohlcv("binance", "futures", "BTCUSDT", "1h", 0, 3600)
    assert spot_cached["Close"].iloc[0] == 100.0
    assert spot_cached.attrs["cutie_market_data_cache_hit"] is True
    assert futures_cached["Close"].iloc[0] == 111.0
    assert futures_cached.attrs["cutie_market_data_cache_hit"] is True


def test_cache_lru_enforced(monkeypatch, tmp_path):
    import os

    monkeypatch.setattr(provider, "MAX_CACHE_FILES", 3)
    provider.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for i in range(5):
        provider._write_cache(f"file_{i}.json", [[i, 1.0, 1.0, 1.0, 1.0, 1.0]])
        # 强制不同 mtime（同一秒内写入多个文件时 mtime 可能相同，LRU 排序需要确定性）
        path = provider.CACHE_DIR / f"file_{i}.json"
        os.utime(path, (i * 10, i * 10))
    remaining = list(provider.CACHE_DIR.glob("*.json"))
    assert len(remaining) == 3
    # 最旧的两个（file_0, file_1）应该被删除
    remaining_names = {f.name for f in remaining}
    assert "file_0.json" not in remaining_names
    assert "file_1.json" not in remaining_names
