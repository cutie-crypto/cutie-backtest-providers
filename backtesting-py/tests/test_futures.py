"""Tests for futures (contract) K-line support in the backtesting.py provider.

Covers: symbol normalization for futures, cache-key market isolation, catalog
markets declaration, and the full /cutie/backtest request path for market=futures
with ccxt mocked out (no real exchange calls).
"""

from __future__ import annotations

import math
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cutie_backtesting_provider as provider  # noqa: E402


# ---------------------------------------------------------------------------
# _normalize_ohlcv_symbol
# ---------------------------------------------------------------------------


def test_normalize_symbol_spot_compact():
    assert provider._normalize_ohlcv_symbol("BTCUSDT", "spot") == "BTC/USDT"


def test_normalize_symbol_spot_lowercase():
    assert provider._normalize_ohlcv_symbol("btcusdt", "spot") == "BTC/USDT"


def test_normalize_symbol_spot_already_slash():
    assert provider._normalize_ohlcv_symbol("BTC/USDT", "spot") == "BTC/USDT"


def test_normalize_symbol_futures_compact_gets_settle_suffix():
    assert provider._normalize_ohlcv_symbol("BTCUSDT", "futures") == "BTC/USDT:USDT"


def test_normalize_symbol_futures_already_slash_gets_settle_suffix():
    assert provider._normalize_ohlcv_symbol("BTC/USDT", "futures") == "BTC/USDT:USDT"


def test_normalize_symbol_futures_already_has_settle_untouched():
    # Caller already passed a fully-qualified unified swap symbol; don't double-append.
    assert provider._normalize_ohlcv_symbol("BTC/USDT:USDT", "futures") == "BTC/USDT:USDT"


# ---------------------------------------------------------------------------
# _cache_key: market must be part of the cache key (spot/futures isolation)
# ---------------------------------------------------------------------------


def test_cache_key_differs_by_market():
    spot_key = provider._cache_key("okx", "spot", "BTCUSDT", "1h", 0, 1000)
    futures_key = provider._cache_key("okx", "futures", "BTCUSDT", "1h", 0, 1000)
    assert spot_key != futures_key
    assert "spot" in spot_key
    assert "futures" in futures_key


# ---------------------------------------------------------------------------
# Catalog: markets + failure_codes
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    return TestClient(provider.app)


def test_catalog_declares_futures_market(client):
    resp = client.get("/catalog")
    assert resp.status_code == 200
    body = resp.json()
    for tool in body["tools"]:
        assert "spot" in tool["markets"]
        assert "futures" in tool["markets"]
        assert "MARKET_UNSUPPORTED" in tool["failure_codes"]


def test_catalog_widened_tunable_ranges(client):
    """Guard against silently narrowing ranges back down."""
    resp = client.get("/catalog")
    body = resp.json()
    by_id = {t["tool_id"]: t for t in body["tools"]}

    ema = by_id["local.backtesting_py.ema_cross"]["param_schema"]["properties"]
    assert ema["ema_fast"]["maximum"] == 100
    assert ema["ema_slow"]["maximum"] == 300

    bb = by_id["local.backtesting_py.bollinger_reversal"]["param_schema"]["properties"]
    assert bb["bb_std"]["maximum"] == 10
    assert bb["bb_period"]["maximum"] == 200

    cci = by_id["local.backtesting_py.cci_rsi"]["param_schema"]["properties"]
    assert cci["cci_oversold"]["minimum"] == -500
    assert cci["cci_overbought"]["maximum"] == 500


# ---------------------------------------------------------------------------
# /cutie/backtest: market validation (no OHLCV fetch needed -> no ccxt mock)
# ---------------------------------------------------------------------------


def test_backtest_rejects_unknown_market(client):
    resp = client.post("/cutie/backtest", json={
        "backtest": {
            "run_id": "r1",
            "symbol": "BTCUSDT",
            "market": "options",
            "timeframe": "1h",
            "start_at": 1000,
            "end_at": 2000,
        },
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["result_status"] == "failed"
    assert body["error_type"] == "MARKET_UNSUPPORTED"
    assert body["provider_revision"] == provider.PROVIDER_REVISION
    assert body["central_market_data_used"] is None
    assert body["central_market_data_auth_mode"] == provider._central_market_data_auth_mode()
    assert body["raw_report"]["market_data_provenance"]["provider_revision"] == provider.PROVIDER_REVISION


def test_backtest_defaults_market_to_spot_when_missing(client, monkeypatch):
    """market omitted -> defaults to 'spot', must not hit MARKET_UNSUPPORTED."""
    calls = {}

    def fake_fetch_ohlcv(exchange_id, market, symbol, timeframe, start_sec, end_sec):
        calls["market"] = market
        raise ValueError("NO_DATA")

    monkeypatch.setattr(provider, "_fetch_ohlcv", fake_fetch_ohlcv)

    resp = client.post("/cutie/backtest", json={
        "backtest": {
            "run_id": "r2",
            "symbol": "BTCUSDT",
            "timeframe": "1h",
            "start_at": 1000,
            "end_at": 2000,
        },
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["error_type"] == "NO_DATA"
    assert calls["market"] == "spot"


# ---------------------------------------------------------------------------
# /cutie/backtest: full futures run with ccxt mocked (no real network calls)
# ---------------------------------------------------------------------------


class _FakeCcxtError(Exception):
    pass


def _install_fake_ccxt(monkeypatch, candles):
    """Install a fake `ccxt` module in sys.modules with one exchange class
    (`okx`) whose fetch_ohlcv serves the given pre-built candle list and
    records the options it was constructed with."""

    fake_module = types.ModuleType("ccxt")
    fake_module.BadSymbol = _FakeCcxtError
    fake_module.RateLimitExceeded = _FakeCcxtError
    fake_module.NetworkError = _FakeCcxtError

    constructed = []

    class FakeOkx:
        def __init__(self, options):
            self.options = options
            self.enableRateLimit = options.get("enableRateLimit")
            constructed.append(self)

        def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None, params=None):
            self.last_symbol = symbol
            batch = [c for c in candles if c[0] >= since][: limit or len(candles)]
            return batch

    fake_module.okx = FakeOkx
    monkeypatch.setitem(sys.modules, "ccxt", fake_module)
    return constructed


def _synthetic_candles(start_ms: int, count: int, step_ms: int):
    candles = []
    price = 100.0
    for i in range(count):
        ts = start_ms + i * step_ms
        price += (1 if i % 2 == 0 else -0.5)
        candles.append([ts, price, price + 1, price - 1, price + 0.5, 10.0])
    return candles


def test_backtest_futures_happy_path(client, monkeypatch, tmp_path):
    monkeypatch.setattr(provider, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(provider, "REPORTS_DIR", tmp_path / "reports")
    # bt.plot()'s HTML report is unrelated to the futures feature under test and
    # is finicky about small synthetic OHLCV series (resample/superimpose); stub
    # it out so the test exercises the request/response path, not plotting.
    from backtesting import Backtest
    monkeypatch.setattr(Backtest, "plot", lambda self, **kwargs: None)

    step_ms = 3600 * 1000  # 1h
    start_ms = 1_700_000_000_000
    count = 40
    candles = _synthetic_candles(start_ms, count, step_ms)
    constructed = _install_fake_ccxt(monkeypatch, candles)

    start_at = start_ms // 1000
    end_at = (start_ms + (count - 1) * step_ms) // 1000

    resp = client.post("/cutie/backtest", json={
        "backtest": {
            "run_id": "futures_run_1",
            "provider_tool_id": "local.backtesting_py.ema_cross",
            "provider_params": {"ema_fast": 3, "ema_slow": 5, "exchange": "okx"},
            "symbol": "BTCUSDT",
            "market": "futures",
            "timeframe": "1h",
            "start_at": start_at,
            "end_at": end_at,
            "initial_capital": "10000",
        },
    })

    assert resp.status_code == 200
    body = resp.json()
    assert body["result_status"] == "success", body
    assert body["provider_revision"] == provider.PROVIDER_REVISION
    assert body["data_source"] == provider.DATA_SOURCE
    assert body["central_market_data_used"] is False
    assert body["central_market_data_auth_mode"] == provider._central_market_data_auth_mode()
    assert body["market_data_cache_hit"] is False
    assert body["raw_report"]["market_data_provenance"] == {
        "provider_revision": provider.PROVIDER_REVISION,
        "source": provider.DATA_SOURCE,
        "central_market_data_used": False,
        "auth_mode": provider._central_market_data_auth_mode(),
        "cache_hit": False,
    }

    # ccxt was asked for the unified linear-perpetual-swap market, not spot.
    assert len(constructed) == 1
    assert constructed[0].options.get("options") == {"defaultType": "swap"}
    assert constructed[0].last_symbol == "BTC/USDT:USDT"

    assert body["assumptions"]["market"] == "futures"
    assert body["limitations"]["funding_rate_included"] is False
    assert "funding rate" in body["raw_report"]["provider_summary"]

    # result.v2（62-1 SPEC §2）：equity_curve[].ts/equity + metrics 恰好三键;
    # 旧展示性百分比指标迁到 raw_report.legacy_metrics。
    for point in body["equity_curve"]:
        assert isinstance(point["ts"], int)
        assert isinstance(point["equity"], str)
    assert set(body["metrics"].keys()) == {"total_return", "max_drawdown", "trade_count"}
    for metric_key in ("total_return_pct", "win_rate_pct", "max_drawdown_pct"):
        assert math.isfinite(body["raw_report"]["legacy_metrics"][metric_key])


def test_backtest_spot_path_unaffected_by_futures_changes(client, monkeypatch, tmp_path):
    monkeypatch.setattr(provider, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(provider, "REPORTS_DIR", tmp_path / "reports")
    from backtesting import Backtest
    monkeypatch.setattr(Backtest, "plot", lambda self, **kwargs: None)

    step_ms = 3600 * 1000
    start_ms = 1_700_000_000_000
    count = 40
    candles = _synthetic_candles(start_ms, count, step_ms)
    constructed = _install_fake_ccxt(monkeypatch, candles)

    start_at = start_ms // 1000
    end_at = (start_ms + (count - 1) * step_ms) // 1000

    resp = client.post("/cutie/backtest", json={
        "backtest": {
            "run_id": "spot_run_1",
            "provider_tool_id": "local.backtesting_py.ema_cross",
            "provider_params": {"ema_fast": 3, "ema_slow": 5, "exchange": "okx"},
            "symbol": "BTCUSDT",
            "market": "spot",
            "timeframe": "1h",
            "start_at": start_at,
            "end_at": end_at,
            "initial_capital": "10000",
        },
    })

    assert resp.status_code == 200
    body = resp.json()
    assert body["result_status"] == "success", body
    assert constructed[0].options.get("options") is None
    assert constructed[0].last_symbol == "BTC/USDT"
    assert body["assumptions"]["market"] == "spot"
    assert "funding_rate_included" not in body["limitations"]
