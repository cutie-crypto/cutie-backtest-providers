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

    rows, reason, attempted = provider._fetch_from_central_with_reason(
        "binance", "spot", "BTCUSDT", "1h", 0, 3600_000
    )
    assert rows is None
    assert reason == "central_timeout"
    assert attempted is True


def test_central_5xx_returns_none(central_configured, monkeypatch):
    def fake_urlopen(*_a, **_kw):
        raise urllib.error.HTTPError("url", 503, "Service Unavailable", {}, None)

    monkeypatch.setattr(provider._CENTRAL_HTTP_OPENER, "open", fake_urlopen)
    result = provider._fetch_from_central("binance", "spot", "BTCUSDT", "1h", 0, 3600_000)
    assert result is None


def test_central_empty_range_returns_explicit_no_data(central_configured, monkeypatch):
    """2010 这类空区间必须保留 no_data 证据，不能只留下 used=None。"""

    def fake_urlopen(*_a, **_kw):
        return _FakeResponse(
            {"err_code": 100, "data": {"available": True, "count": 0, "items": []}}
        )

    monkeypatch.setattr(provider._CENTRAL_HTTP_OPENER, "open", fake_urlopen)
    rows, reason, attempted = provider._fetch_from_central_with_reason(
        "binance", "spot", "BTCUSDT", "4h", 1262304000_000, 1264982400_000
    )

    assert rows is None
    assert reason == "no_data"
    assert attempted is True


def test_business_failure_exposes_redaction_safe_central_reason():
    response = provider._business_failure(
        "run-1",
        "NO_DATA",
        "No OHLCV data is available in the requested range",
        market_data_provenance={
            "provider_revision": "abc1234",
            "source": None,
            "central_market_data_used": False,
            "central_market_data_attempted": True,
            "central_failure_reason": "no_data",
            "auth_mode": "market_data_bearer",
            "cache_hit": False,
        },
    )
    body = json.loads(response.body)

    assert body["central_market_data_used"] is False
    assert body["raw_report"]["market_data_provenance"]["central_failure_reason"] == "no_data"
    assert "token" not in response.body.decode("utf-8").lower()


def test_empty_central_range_wins_over_ccxt_network_error(monkeypatch):
    """The exact 2010 failure shape must remain NO_DATA even if ccxt is geo-blocked."""
    import ccxt

    monkeypatch.setattr(
        provider,
        "_fetch_from_central_with_reason",
        lambda *_a, **_kw: (None, provider.CENTRAL_REASON_NO_DATA, True),
    )

    class FakeExchange:
        def __init__(self, _options):
            pass

        def fetch_ohlcv(self, *_a, **_kw):
            raise ccxt.NetworkError("HTTP 451 restricted location")

    monkeypatch.setattr(ccxt, "binance", FakeExchange)

    with pytest.raises(provider.MarketDataFetchError) as caught:
        provider._fetch_ohlcv(
            "binance", "spot", "BTCUSDT", "4h", 1262304000, 1264982400
        )
    assert caught.value.error_type == "NO_DATA"
    assert caught.value.provenance["central_failure_reason"] == "no_data"
    assert caught.value.provenance["central_market_data_attempted"] is True


def test_explicit_central_auth_failure_survives_ccxt_network_error(monkeypatch):
    import ccxt

    monkeypatch.setattr(
        provider,
        "_fetch_from_central_with_reason",
        lambda *_a, **_kw: (None, provider.CENTRAL_REASON_AUTH_FAILED, True),
    )

    class FakeExchange:
        def __init__(self, _options):
            pass

        def fetch_ohlcv(self, *_a, **_kw):
            raise ccxt.NetworkError("network unavailable")

    monkeypatch.setattr(ccxt, "binance", FakeExchange)

    with pytest.raises(provider.MarketDataFetchError) as caught:
        provider._fetch_ohlcv(
            "binance", "spot", "BTCUSDT", "4h", 1700000000, 1700086400
        )
    assert caught.value.error_type == "DATA_FETCH_FAILED"
    assert caught.value.provenance["central_failure_reason"] == "central_auth_failed"


def test_empty_ccxt_result_is_direct_no_data_evidence(monkeypatch):
    import ccxt

    monkeypatch.setattr(
        provider,
        "_fetch_from_central_with_reason",
        lambda *_a, **_kw: (None, provider.CENTRAL_REASON_AUTH_FAILED, True),
    )

    class FakeExchange:
        def __init__(self, _options):
            pass

        def fetch_ohlcv(self, *_a, **_kw):
            return []

    monkeypatch.setattr(ccxt, "binance", FakeExchange)

    with pytest.raises(provider.MarketDataFetchError) as caught:
        provider._fetch_ohlcv(
            "binance", "spot", "BTCUSDT", "4h", 1700000000, 1700086400
        )
    assert caught.value.error_type == "NO_DATA"
    assert caught.value.provenance["central_failure_reason"] == "central_auth_failed"
    assert caught.value.provenance["central_market_data_attempted"] is True


@pytest.mark.parametrize(
    "central_reason",
    [provider.CENTRAL_REASON_OUTSIDE_COVERAGE, provider.CENTRAL_REASON_DATA_GAP],
)
def test_ambiguous_central_range_reason_does_not_mask_ccxt_network_error(
    monkeypatch, central_reason
):
    import ccxt

    monkeypatch.setattr(
        provider,
        "_fetch_from_central_with_reason",
        lambda *_a, **_kw: (None, central_reason, True),
    )

    class FakeExchange:
        def __init__(self, _options):
            pass

        def fetch_ohlcv(self, *_a, **_kw):
            raise ccxt.NetworkError("network unavailable")

    monkeypatch.setattr(ccxt, "binance", FakeExchange)

    with pytest.raises(provider.MarketDataFetchError) as caught:
        provider._fetch_ohlcv(
            "binance", "spot", "BTCUSDT", "4h", 1700000000, 1700086400
        )
    assert caught.value.error_type == "DATA_FETCH_FAILED"
    assert caught.value.provenance["central_failure_reason"] == central_reason


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


# ---------------------------------------------------------------------------
# P1 治本：等分切片 + grid 对齐缺口判据 + 小样本下限
# 生产 run 359532680989114368（1d×365 天）失败根因：365 mod 90 = 5 天的末片，
# 期望按裸 (end-start)//step 算出 5 根，服务端起点非 UTC 零点实得 4 根，
# 4 < 5*0.9=4.5 被判 data_gap 整段回退 ccxt，该 runtime ccxt 出口不通 -> 硬失败。
# ---------------------------------------------------------------------------


def _grid_items(start_ts: int, end_ts: int, timeframe: str) -> list[dict]:
    """模拟服务端「只返回已收盘、grid 对齐 bar」的真实行为：不依赖被测的
    ``_expected_bar_count``，独立按 timeframe 的 step 重新计算 grid 边界，
    避免测试与实现用同一份公式互相自证。"""
    step_ms = provider._timeframe_milliseconds(timeframe)
    start_ms, end_ms = start_ts * 1000, end_ts * 1000
    first_open = -(-start_ms // step_ms) * step_ms  # ceil 到下一个 grid 点
    last_open = ((end_ms - step_ms) // step_ms) * step_ms  # 最后一个已收盘 grid 点
    if last_open < first_open:
        return []
    opens = range(first_open, last_open + step_ms, step_ms)
    return [
        {"open_time": o // 1000, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}
        for o in opens
    ]


@pytest.mark.parametrize(
    ("total_days", "timeframe"),
    [
        (365, "1d"),
        (181, "4h"),
        (95, "1h"),
        (364, "1d"),
    ],
)
def test_non_grid_aligned_start_no_longer_false_positive_gap(
    central_configured, monkeypatch, total_days, timeframe
):
    """回归 359532680989114368：起点非 UTC 零点、总跨度对 90 天取余数的窗口，
    只要服务端按「已收盘 grid bar」如实返回，不应再被判 data_gap。"""
    # 故意用一个不落在任何 timeframe grid 上的起点（不是 UTC 00:00 也不是任何
    # 4h/1h 边界），复现"起点非零点少 1 根"的真实场景。
    start_ts = 1_600_003_723
    end_ts = start_ts + total_days * 24 * 3600

    def fake_urlopen(req, timeout=None):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)
        chunk_start_ts = int(qs["start_ts"][0])
        chunk_end_ts = int(qs["end_ts"][0])
        items = _grid_items(chunk_start_ts, chunk_end_ts, timeframe)
        return _FakeResponse({"err_code": 100, "data": {"available": True, "count": len(items), "items": items}})

    monkeypatch.setattr(provider._CENTRAL_HTTP_OPENER, "open", fake_urlopen)

    result = provider._fetch_from_central(
        "binance", "spot", "BTCUSDT", timeframe, start_ts * 1000, end_ts * 1000
    )
    assert result is not None, "非 grid 对齐起点不应再被误判 data_gap"


def test_true_gap_still_detected_after_split(central_configured, monkeypatch):
    """真缺口（抠掉 20% 的 bar）仍要被抓到，不能因为治本改动放宽到抓不住真问题。"""
    start_ts = 1_600_000_000
    end_ts = start_ts + 200 * 3600  # 200 小时，1h 周期，单分片内（< 90 天上限）

    def fake_urlopen(req, timeout=None):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)
        chunk_start_ts = int(qs["start_ts"][0])
        chunk_end_ts = int(qs["end_ts"][0])
        items = _grid_items(chunk_start_ts, chunk_end_ts, "1h")
        keep = int(len(items) * 0.8)  # 抠掉 20%
        return _FakeResponse(
            {"err_code": 100, "data": {"available": True, "count": keep, "items": items[:keep]}}
        )

    monkeypatch.setattr(provider._CENTRAL_HTTP_OPENER, "open", fake_urlopen)

    result = provider._fetch_from_central("binance", "spot", "BTCUSDT", "1h", start_ts * 1000, end_ts * 1000)
    assert result is None, "抠掉 20% 的真缺口必须仍被判 data_gap"


@pytest.mark.parametrize(("missing", "expect_gap"), [(1, False), (2, True)])
def test_small_sample_floor_tolerates_at_most_one_bar(central_configured, monkeypatch, missing, expect_gap):
    """expected < 20 时用绝对下限 actual >= expected - 1：缺 1 根放行，缺 2 根仍判缺口。"""
    start_ts = 1_600_003_723  # 非 grid 对齐起点，真实 expected 会是 9（不是裸算的 10）
    end_ts = start_ts + 10 * 3600  # 10 小时窗口 -> expected < 20，落入小样本区间

    def fake_urlopen(req, timeout=None):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)
        chunk_start_ts = int(qs["start_ts"][0])
        chunk_end_ts = int(qs["end_ts"][0])
        items = _grid_items(chunk_start_ts, chunk_end_ts, "1h")
        keep = max(0, len(items) - missing)
        return _FakeResponse(
            {"err_code": 100, "data": {"available": True, "count": keep, "items": items[:keep]}}
        )

    monkeypatch.setattr(provider._CENTRAL_HTTP_OPENER, "open", fake_urlopen)

    result = provider._fetch_from_central("binance", "spot", "BTCUSDT", "1h", start_ts * 1000, end_ts * 1000)
    if expect_gap:
        assert result is None
    else:
        assert result is not None


# ---------------------------------------------------------------------------
# P1 返修（Codex 复审）：小样本下限只比数量会放过中段真缺 1 根，改按 open_time
# 集合比对，只有缺口落在 grid 首/尾才放行。expected=15（15 个 grid 点：
# open_time = 0, 3600, ..., 50400 秒，1h 周期，窗口 [0, 15h)）。
# ---------------------------------------------------------------------------


def _fifteen_hour_grid_items() -> list[dict]:
    return _grid_items(0, 15 * 3600, "1h")


def test_small_sample_mid_bar_missing_is_still_data_gap(central_configured, monkeypatch, caplog):
    """expected=15，缺第 8 根（中段，open_time=7*3600）：数量上只差 1 根，
    但不是 grid 首/尾边界，必须仍判 data_gap，不能被小样本下限放过。"""
    items = _fifteen_hour_grid_items()
    assert len(items) == 15
    missing_open_time = items[7]["open_time"]  # 第 8 根（0-indexed 7），中段
    kept = items[:7] + items[8:]
    assert len(kept) == 14

    def fake_urlopen(*_a, **_kw):
        return _FakeResponse({"err_code": 100, "data": {"available": True, "count": len(kept), "items": kept}})

    monkeypatch.setattr(provider._CENTRAL_HTTP_OPENER, "open", fake_urlopen)

    result = provider._fetch_from_central("binance", "spot", "BTCUSDT", "1h", 0, 15 * 3600 * 1000)
    assert result is None, f"中段缺失 open_time={missing_open_time} 必须仍判 data_gap"


def test_small_sample_last_bar_missing_is_tolerated_and_logged(central_configured, monkeypatch, caplog):
    """expected=15，缺最后一根（grid 尾边界，如 closed_bound 略滞后）：放行，
    且要留下可审计的独立 INFO 日志。"""
    items = _fifteen_hour_grid_items()
    kept = items[:-1]
    assert len(kept) == 14

    def fake_urlopen(*_a, **_kw):
        return _FakeResponse({"err_code": 100, "data": {"available": True, "count": len(kept), "items": kept}})

    monkeypatch.setattr(provider._CENTRAL_HTTP_OPENER, "open", fake_urlopen)

    with caplog.at_level("INFO", logger=provider.logger.name):
        result = provider._fetch_from_central("binance", "spot", "BTCUSDT", "1h", 0, 15 * 3600 * 1000)
    assert result is not None, "缺末根应被小样本边界容忍放行"
    assert any("small-sample gap tolerated" in rec.message for rec in caplog.records)
    assert any("末根" in rec.message for rec in caplog.records)


def test_small_sample_first_bar_missing_is_tolerated(central_configured, monkeypatch):
    """expected=15，缺第一根（grid 首边界，如起点邻近效应）：放行。"""
    items = _fifteen_hour_grid_items()
    kept = items[1:]
    assert len(kept) == 14

    def fake_urlopen(*_a, **_kw):
        return _FakeResponse({"err_code": 100, "data": {"available": True, "count": len(kept), "items": kept}})

    monkeypatch.setattr(provider._CENTRAL_HTTP_OPENER, "open", fake_urlopen)

    result = provider._fetch_from_central("binance", "spot", "BTCUSDT", "1h", 0, 15 * 3600 * 1000)
    assert result is not None, "缺首根应被小样本边界容忍放行"


def test_small_sample_missing_both_ends_is_data_gap(central_configured, monkeypatch):
    """expected=15，首尾各缺 1 根（actual = expected - 2）：deficit>=2 一律判
    data_gap，不进边界检查——即使两个缺口都恰好是边界，也不放行。"""
    items = _fifteen_hour_grid_items()
    kept = items[1:-1]
    assert len(kept) == 13

    def fake_urlopen(*_a, **_kw):
        return _FakeResponse({"err_code": 100, "data": {"available": True, "count": len(kept), "items": kept}})

    monkeypatch.setattr(provider._CENTRAL_HTTP_OPENER, "open", fake_urlopen)

    result = provider._fetch_from_central("binance", "spot", "BTCUSDT", "1h", 0, 15 * 3600 * 1000)
    assert result is None, "缺 2 根必须仍判 data_gap，不因为都在边界就放行"


def test_split_central_range_365d_1d_produces_five_equal_chunks():
    """365 天/1d 切成 5 片，每片恰好 73 天（365/5 整除，无需并余数），片间无重叠无空洞。"""
    start_ms = 0
    end_ms = 365 * 24 * 3600 * 1000
    chunks = provider._split_central_range(start_ms, end_ms, "1d", provider.CENTRAL_MAX_CHUNK_MS)

    assert len(chunks) == 5
    day_ms = 24 * 3600 * 1000
    for chunk_start, chunk_end in chunks:
        assert (chunk_end - chunk_start) // day_ms >= 73

    # 无重叠无空洞：前一片的 end 恰好是后一片的 start，首尾覆盖整个区间。
    assert chunks[0][0] == start_ms
    assert chunks[-1][1] == end_ms
    for (prev_start, prev_end), (next_start, _next_end) in zip(chunks, chunks[1:]):
        assert prev_end == next_start


def test_split_central_range_364d_1d_no_tiny_tail():
    """364 mod 90 = 4 天的余数不再单独成片：5 片里最小的一片仍有 72 天，不是 4 天。"""
    start_ms = 0
    end_ms = 364 * 24 * 3600 * 1000
    chunks = provider._split_central_range(start_ms, end_ms, "1d", provider.CENTRAL_MAX_CHUNK_MS)

    day_ms = 24 * 3600 * 1000
    sizes_days = [(chunk_end - chunk_start) // day_ms for chunk_start, chunk_end in chunks]
    assert min(sizes_days) >= 72
    assert sum(sizes_days) == 364
    assert chunks[0][0] == start_ms
    assert chunks[-1][1] == end_ms


def test_split_central_range_short_span_unchanged():
    """跨度本就 <= 90 天：单片直通，不引入不必要的分片。"""
    start_ms = 0
    end_ms = 30 * 24 * 3600 * 1000
    chunks = provider._split_central_range(start_ms, end_ms, "1d", provider.CENTRAL_MAX_CHUNK_MS)
    assert chunks == [(start_ms, end_ms)]


# ---------------------------------------------------------------------------
# P1 返修（亲审）：服务端单次请求跨度硬上限 90 天（MAX_KLINE_RANGE_SECONDS），
# 90 天不是 1w 的整数倍（90/7=12.86）时，纯按 bar 数均分会算出 13 周=91 天的
# 分片，被 ERR_INVALID_PARAMS 拒绝整体回退 ccxt。num_chunks 必须取「按毫秒算」
# 与「按 bar 数上限算」两者较大值，见 _split_central_range 修复。
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("total_days", "timeframe"),
    [
        (175, "1w"),  # 团队亲审给出的复现例：25 周，naive ceil(25/2)=13 周=91 天超限
        (180, "1w"),
        (179, "1d"),  # 179/90=1.988，边界恰好卡在 2 片上，验证 90 天精确不超限
        (365, "1d"),
        (364, "1d"),
        (181, "4h"),
        (95, "1h"),
    ],
)
def test_split_central_range_never_exceeds_server_hard_cap(total_days, timeframe):
    """任一分片跨度都不得超过 max_chunk_ms（服务端硬上限），且片间无重叠无空洞。"""
    start_ms = 0
    end_ms = total_days * 24 * 3600 * 1000
    chunks = provider._split_central_range(start_ms, end_ms, timeframe, provider.CENTRAL_MAX_CHUNK_MS)

    assert chunks, "非空跨度必须产生至少一片"
    for chunk_start, chunk_end in chunks:
        assert chunk_end - chunk_start <= provider.CENTRAL_MAX_CHUNK_MS, (
            f"chunk [{chunk_start},{chunk_end}) span exceeds CENTRAL_MAX_CHUNK_MS "
            f"for total_days={total_days} timeframe={timeframe!r}"
        )
    assert chunks[0][0] == start_ms
    assert chunks[-1][1] == end_ms
    for (prev_start, prev_end), (next_start, _next_end) in zip(chunks, chunks[1:]):
        assert prev_end == next_start


def test_split_central_range_1w_175d_matches_reviewed_repro():
    """175 天/1w（25 周）：亲审给出的具体复现例，确认修复后不再产生 91 天分片。"""
    start_ms = 0
    end_ms = 175 * 24 * 3600 * 1000
    chunks = provider._split_central_range(start_ms, end_ms, "1w", provider.CENTRAL_MAX_CHUNK_MS)

    week_ms = 7 * 24 * 3600 * 1000
    day_ms = 24 * 3600 * 1000
    for chunk_start, chunk_end in chunks:
        span = chunk_end - chunk_start
        assert span <= 90 * day_ms
    # 123 B2b：切点对齐周线真实网格（Binance 周一 00:00 UTC 开盘，epoch 0 是周四），
    # 不再是「从 start 起整数周」——start=0 不在周线网格上，首片比整周短。
    for _chunk_start, chunk_end in chunks[:-1]:
        assert (chunk_end - 4 * day_ms) % week_ms == 0
    assert chunks[0][0] == start_ms
    assert sum(chunk_end - chunk_start for chunk_start, chunk_end in chunks) == end_ms - start_ms


def test_expected_bar_count_grid_aligns_non_zero_start():
    """非 UTC 零点起点：期望条数按下一个 grid 开盘点算，不多算起点到下一个
    grid 点之间那一小段——这正是 359532680989114368 少算出来的那 1 根。"""
    day_ms = 24 * 3600 * 1000
    # 起点比零点晚 3 小时，跨度恰好 5 天：naive 公式给 5，grid 对齐给 4。
    start_ms = 3 * 3600 * 1000
    end_ms = start_ms + 5 * day_ms
    expected = provider._expected_bar_count("1d", start_ms, end_ms)
    assert expected == 4


# ---------------------------------------------------------------------------
# 123 B2b（D5）：切点必须落在 K 线绝对网格上。此前按「从 start 起整数倍 bar」切，
# start 不在网格上时跨切点那根 bar 在前后两片都不被服务端返回（服务端只给
# start_ts <= open 且 open+step <= end_ts 的 bar），拼接少一根。
# ---------------------------------------------------------------------------

_WEEK_OFFSET_SEC = 4 * 24 * 3600  # Binance 周线周一开盘，epoch 0 是周四


def _server_grid_opens(start_ts: int, end_ts: int, timeframe: str) -> list[int]:
    """独立复刻服务端 /klines 口径（market_kline_cache_service：start_ts <= open 且
    open+step <= end_ts），网格按交易所真实开盘时间（周线周一），不调被测实现。"""
    step = {"1h": 3600, "4h": 4 * 3600, "1d": 86400, "1w": 7 * 86400}[timeframe]
    offset = _WEEK_OFFSET_SEC if timeframe == "1w" else 0
    first = start_ts + (-(start_ts - offset)) % step
    return [o for o in range(first, end_ts, step) if o + step <= end_ts]


def _install_grid_server(monkeypatch, timeframe: str, requested: list[tuple[int, int]]):
    def fake_urlopen(req, timeout=None):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)
        start_ts = int(qs["start_ts"][0])
        end_ts = int(qs["end_ts"][0])
        assert end_ts - start_ts <= 90 * 24 * 3600, "服务端单次跨度硬上限 90 天"
        requested.append((start_ts, end_ts))
        items = [
            {"open_time": o, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}
            for o in _server_grid_opens(start_ts, end_ts, timeframe)
        ]
        return _FakeResponse({"err_code": 100, "data": {"available": True, "count": len(items), "items": items}})

    monkeypatch.setattr(provider._CENTRAL_HTTP_OPENER, "open", fake_urlopen)


@pytest.mark.parametrize(
    ("timeframe", "start_ts", "total_days"),
    [
        ("1h", 1_700_000_123, 95),
        ("4h", 1_782_323_503, 92),  # 本机真链路 D5 复现起点（08-09 20:00 那根跨切点）
        ("4h", 1_700_000_123, 200),
        ("1d", 1_700_000_123, 365),
    ],
)
def test_unaligned_start_chunked_fetch_matches_one_shot(central_configured, monkeypatch, timeframe, start_ts, total_days):
    """起点非整点、跨度 > 90 天：分片拼接的 bar 序列与一次性取全量逐根一致
    （open_time 连续、无重复、无缺失），且切点落在网格上。"""
    end_ts = start_ts + total_days * 86400
    requested: list[tuple[int, int]] = []
    _install_grid_server(monkeypatch, timeframe, requested)

    rows = provider._fetch_from_central(
        "binance", "futures", "BTCUSDT", timeframe, start_ts * 1000, end_ts * 1000
    )

    assert len(requested) >= 2, "跨度 > 90 天必须分片"
    assert rows is not None
    got = [row[0] // 1000 for row in rows]
    assert got == _server_grid_opens(start_ts, end_ts, timeframe)
    step = provider._timeframe_milliseconds(timeframe) // 1000
    assert all(b - a == step for a, b in zip(got, got[1:]))
    assert requested[0][0] == start_ts and requested[-1][1] == end_ts
    for _chunk_start, chunk_end in requested[:-1]:
        assert chunk_end % step == 0


def test_unaligned_start_1w_split_cuts_on_monday_grid():
    """周线：切点落在周一网格上，拼接后与一次性取全量逐根一致（只验切点，
    周线的缺口期望条数另走 _expected_bar_grid，不在本批范围）。"""
    start_ts = 1_700_000_123
    end_ts = start_ts + 200 * 86400
    chunks = provider._split_central_range(start_ts * 1000, end_ts * 1000, "1w", provider.CENTRAL_MAX_CHUNK_MS)
    assert len(chunks) >= 2
    for chunk_start, chunk_end in chunks:
        assert chunk_end - chunk_start <= provider.CENTRAL_MAX_CHUNK_MS
    for (_a, prev_end), (next_start, _b) in zip(chunks, chunks[1:]):
        assert prev_end == next_start
        assert (prev_end // 1000 - _WEEK_OFFSET_SEC) % (7 * 86400) == 0
    concatenated = [o for a, b in chunks for o in _server_grid_opens(a // 1000, b // 1000, "1w")]
    assert concatenated == _server_grid_opens(start_ts, end_ts, "1w")
