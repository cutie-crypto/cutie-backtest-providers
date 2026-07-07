"""WS-7 Step 7.3：provider 中心行情数据优先级 + ccxt 回退 + 缓存 LRU 上限测试。

覆盖：
1. 未配置中心 API：直接走 ccxt（向后兼容，不发中心请求）
2. 中心 API 超时/网络错误：回退 ccxt
3. 中心 API 5xx：回退 ccxt
4. 中心 API 返回数据缺口：回退 ccxt
5. 中心 API 命中：跳过 ccxt，直接用中心数据
6. exchange/market 不在中心缓存覆盖范围（非 binance spot）：跳过中心请求，直接 ccxt
7. 缓存 LRU 上限：超过 MAX_CACHE_FILES 时删最旧文件
"""

from __future__ import annotations

import json
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cutie_backtesting_provider as provider  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(provider, "CACHE_DIR", tmp_path / "cache" / "ohlcv")
    return tmp_path


@pytest.fixture
def central_configured(monkeypatch):
    monkeypatch.setattr(provider, "CENTRAL_MARKET_DATA_URL", "https://server.example.com/v1/internal/market-data")
    monkeypatch.setattr(provider, "CENTRAL_MARKET_DATA_KEY", "test-internal-key")


def test_central_not_configured_skips_request_returns_none():
    """未配置 URL/KEY：_fetch_from_central 直接返回 None，不发任何请求。"""
    result = provider._fetch_from_central("binance", "spot", "BTCUSDT", "1h", 0, 3600_000)
    assert result is None


def test_unsupported_exchange_skips_central_request(central_configured, monkeypatch):
    """exchange != binance：不在中心缓存范围内，跳过请求（不是失败，是范围外）。"""
    called = {"count": 0}

    def fake_urlopen(*_a, **_kw):
        called["count"] += 1
        raise AssertionError("should not call central API for non-binance exchange")

    monkeypatch.setattr(provider.urllib.request, "urlopen", fake_urlopen)
    result = provider._fetch_from_central("okx", "spot", "BTCUSDT", "1h", 0, 3600_000)
    assert result is None
    assert called["count"] == 0


def test_unsupported_market_skips_central_request(central_configured, monkeypatch):
    def fake_urlopen(*_a, **_kw):
        raise AssertionError("should not call central API for futures market")

    monkeypatch.setattr(provider.urllib.request, "urlopen", fake_urlopen)
    result = provider._fetch_from_central("binance", "futures", "BTCUSDT", "1h", 0, 3600_000)
    assert result is None


def test_central_timeout_returns_none(central_configured, monkeypatch):
    def fake_urlopen(*_a, **_kw):
        raise TimeoutError("timed out")

    monkeypatch.setattr(provider.urllib.request, "urlopen", fake_urlopen)
    result = provider._fetch_from_central("binance", "spot", "BTCUSDT", "1h", 0, 3600_000)
    assert result is None


def test_central_5xx_returns_none(central_configured, monkeypatch):
    def fake_urlopen(*_a, **_kw):
        raise urllib.error.HTTPError("url", 503, "Service Unavailable", {}, None)

    monkeypatch.setattr(provider.urllib.request, "urlopen", fake_urlopen)
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
                "data": {"available": True, "count": 1, "items": [{"open_time": 0, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]},
            }
        )

    monkeypatch.setattr(provider.urllib.request, "urlopen", fake_urlopen)
    # 100 小时区间，理应有 ~100 根 1h K 线
    result = provider._fetch_from_central("binance", "spot", "BTCUSDT", "1h", 0, 100 * 3600 * 1000)
    assert result is None


def test_central_success_returns_rows(central_configured, monkeypatch):
    items = [
        {"open_time": i * 3600, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100.0}
        for i in range(3)
    ]

    def fake_urlopen(*_a, **_kw):
        return _FakeResponse({"err_code": 100, "data": {"available": True, "count": 3, "items": items}})

    monkeypatch.setattr(provider.urllib.request, "urlopen", fake_urlopen)
    result = provider._fetch_from_central("binance", "spot", "BTCUSDT", "1h", 0, 3 * 3600 * 1000)
    assert result is not None
    assert len(result) == 3
    assert result[0] == [0, 1.0, 2.0, 0.5, 1.5, 100.0]


def test_central_available_false_returns_none(central_configured, monkeypatch):
    """server 侧对不支持组合返回 available=false（非报错），provider 视同回退信号。"""

    def fake_urlopen(*_a, **_kw):
        return _FakeResponse({"err_code": 100, "data": {"available": False, "count": 0, "items": []}})

    monkeypatch.setattr(provider.urllib.request, "urlopen", fake_urlopen)
    result = provider._fetch_from_central("binance", "spot", "BTCUSDT", "1h", 0, 3600_000)
    assert result is None


def test_fetch_ohlcv_falls_back_to_ccxt_when_central_fails(central_configured, monkeypatch):
    """端到端：central 超时 → _fetch_ohlcv 仍能通过 ccxt 拿到数据（故障注入验证）。"""

    def fake_urlopen(*_a, **_kw):
        raise TimeoutError("timed out")

    monkeypatch.setattr(provider.urllib.request, "urlopen", fake_urlopen)

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


def test_fetch_ohlcv_uses_central_and_skips_ccxt(central_configured, monkeypatch):
    """端到端：central 命中时不调用 ccxt。"""
    items = [{"open_time": 0, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100.0}]

    def fake_urlopen(*_a, **_kw):
        return _FakeResponse({"err_code": 100, "data": {"available": True, "count": 1, "items": items}})

    monkeypatch.setattr(provider.urllib.request, "urlopen", fake_urlopen)

    fake_ccxt = MagicMock()

    def _fail_if_called(*_a, **_kw):
        raise AssertionError("ccxt should not be called when central hits")

    fake_ccxt.binance = _fail_if_called
    monkeypatch.setitem(sys.modules, "ccxt", fake_ccxt)

    df = provider._fetch_ohlcv("binance", "spot", "BTCUSDT", "1h", 0, 3600)
    assert len(df) == 1


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
