"""WARM-1P-P：start_at 不在周期边界时，跨 start 那根 K 线归入预热尾。

中心行情只下发「开盘 >= since 且已收盘」的 K 线，所以主区间首根是 ceil(start)；预热段以前止于
未取整的 start，跨 start 那根（开盘 floor(start)、收盘 ceil(start)）两头都丢，预热尾与主区间首根
之间空一根。修法：预热取数以 ceil(start) 为基准和上界。主区间与资金曲线首点不动（首点仍是请求的
start_at）。合成 K 线，不联网，打桩 _fetch_ohlcv。
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cutie_backtesting_provider as provider  # noqa: E402

HOUR = 3600
H0 = 1735689600  # 2025-01-01 00:00 UTC（整点）
UNALIGNED_START = H0 + 1865  # 整点 + 31 分 5 秒
CEIL_START = H0 + HOUR
MAIN_COUNT = 100
PRE_COUNT = 200
BARS = 43  # rsi_reversal(14) min_bars = 3 * 14 + 1
RSI_TOOL = "local.backtesting_py.rsi_reversal"
RSI_PARAMS = {"rsi_period": 14, "oversold": 30, "overbought": 70}
LEDGER_TOOL = "local.backtesting_py.rsi_scale_in_out"
LEDGER_PARAMS = {"rsi_period": 14, "oversold": 30, "overbought": 70, "buy_notional": 100, "sell_notional": 100}


def _full_df(ceil_start: int) -> pd.DataFrame:
    """ceil_start 之前 PRE_COUNT 根、之后 MAIN_COUNT 根，1h，价格确定性波动。"""
    first = ceil_start - PRE_COUNT * HOUR
    closes = [100.0 + ((i * 7) % 11) - 5 + (0.01 * i) for i in range(PRE_COUNT + MAIN_COUNT)]
    idx = pd.DatetimeIndex([pd.to_datetime((first + i * HOUR) * 1000, unit="ms") for i in range(len(closes))])
    return pd.DataFrame(
        {"Open": closes, "High": [c + 1.5 for c in closes], "Low": [c - 1.5 for c in closes],
         "Close": closes, "Volume": [10.0] * len(closes)},
        index=idx,
    )


def _central_fetch(full: pd.DataFrame, calls: list | None = None):
    """中心行情口径：开盘 >= since 且已收盘（收盘 <= until）的 K 线；跨 until 的那根不给。"""
    def fetch(exchange, market, symbol, timeframe, start, end):
        if calls is not None:
            calls.append((start, end))
        lo = pd.to_datetime(start * 1000, unit="ms")
        hi = pd.to_datetime((end - HOUR) * 1000, unit="ms")
        out = full.loc[(full.index >= lo) & (full.index <= hi)].copy()
        out.attrs["cutie_data_source"] = "cutie_central_market_data"
        out.attrs["cutie_central_market_data_used"] = True
        out.attrs["cutie_market_data_cache_hit"] = False
        return out

    return fetch


def _epoch(ts) -> int:
    return int(ts.value // 10**9)


@pytest.fixture()
def client(monkeypatch, tmp_path):
    from backtesting import Backtest

    monkeypatch.setattr(provider, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(Backtest, "plot", lambda self, **kwargs: None)
    return TestClient(provider.app)


def _post(client, tool_id: str, params: dict, start_at: int, end_at: int) -> dict:
    resp = client.post("/cutie/backtest", json={"backtest": {
        "run_id": "warm1p_1",
        "provider_tool_id": tool_id,
        "provider_params": {"exchange": "binance", **params},
        "symbol": "BTCUSDT",
        "market": "spot",
        "timeframe": "1h",
        "start_at": start_at,
        "end_at": end_at,
        "initial_capital": "10000",
        "fee_bps": "10",
        "slippage_bps": "5",
    }})
    assert resp.status_code == 200
    body = resp.json()
    assert body["result_status"] == "success", body
    return body


def _direct_warmup(start_at: int, ceil_start: int):
    full = _full_df(ceil_start)
    calls: list = []
    fetch = _central_fetch(full, calls)
    main_df = fetch("binance", "spot", "BTCUSDT", "1h", start_at, ceil_start + MAIN_COUNT * HOUR)
    return full, calls, main_df


def test_warmup_includes_straddle_bar_when_start_unaligned(monkeypatch):
    full, calls, main_df = _direct_warmup(UNALIGNED_START, CEIL_START)
    # 中心行情只给开盘 >= start 的 K 线：主区间首根是 ceil(start)
    assert _epoch(main_df.index[0]) == CEIL_START
    monkeypatch.setattr(provider, "_fetch_ohlcv", _central_fetch(full, calls))
    calls.clear()
    warm = provider._fetch_template_warmup("binance", "spot", "BTCUSDT", "1h", UNALIGNED_START, BARS, main_df)
    assert len(warm) == BARS
    # 预热最后一根的开盘时间 = floor(start)（跨 start 的那根）
    assert _epoch(warm.index[-1]) == H0
    # 取数以 ceil(start) 为基准和上界
    assert calls == [(CEIL_START - (BARS + provider._TEMPLATE_WARMUP_FETCH_EXTRA_BARS) * HOUR, CEIL_START)]


# 对齐 start 的金样：在改动前的代码（floor 取整、止于 int(start)）上抓取。
ALIGNED_GOLDEN = {
    "rows": 43,
    "first_open": H0 - 43 * HOUR,
    "last_open": H0 - HOUR,
    "calls": [(H0 - 45 * HOUR, H0)],
    "close_sha256_8": "e7bc4cc3",
}


def test_warmup_unchanged_when_start_aligned(monkeypatch):
    full, calls, main_df = _direct_warmup(H0, H0)
    assert _epoch(main_df.index[0]) == H0
    monkeypatch.setattr(provider, "_fetch_ohlcv", _central_fetch(full, calls))
    calls.clear()
    warm = provider._fetch_template_warmup("binance", "spot", "BTCUSDT", "1h", H0, BARS, main_df)
    got = {
        "rows": len(warm),
        "first_open": _epoch(warm.index[0]),
        "last_open": _epoch(warm.index[-1]),
        "calls": list(calls),
        "close_sha256_8": hashlib.sha256(",".join(repr(float(v)) for v in warm["Close"]).encode()).hexdigest()[:8],
    }
    assert got == ALIGNED_GOLDEN


def test_warmup_contiguous_with_main_first_bar(monkeypatch):
    full, calls, main_df = _direct_warmup(UNALIGNED_START, CEIL_START)
    monkeypatch.setattr(provider, "_fetch_ohlcv", _central_fetch(full, calls))
    warm = provider._fetch_template_warmup("binance", "spot", "BTCUSDT", "1h", UNALIGNED_START, BARS, main_df)
    assert _epoch(warm.index[-1]) + HOUR == _epoch(main_df.index[0])
    steps = {int(d.total_seconds()) for d in warm.index[1:] - warm.index[:-1]}
    assert steps == {HOUR}, "预热段自身必须连续，不留缺口"


@pytest.mark.parametrize("tool_id,params", [(RSI_TOOL, RSI_PARAMS), (LEDGER_TOOL, LEDGER_PARAMS)],
                         ids=["engine", "ledger"])
def test_equity_curve_first_ts_equals_request_start_at(client, monkeypatch, tool_id, params):
    full = _full_df(CEIL_START)
    monkeypatch.setattr(provider, "_fetch_ohlcv", _central_fetch(full))
    end_at = CEIL_START + MAIN_COUNT * HOUR
    body = _post(client, tool_id, params, UNALIGNED_START, end_at)
    curve = body["equity_curve"]
    assert curve[0]["ts"] == UNALIGNED_START
    assert curve[0]["equity"] == "10000"
    assert all(point["ts"] > UNALIGNED_START for point in curve[1:])
    assert body["data_manifest"]["start_at"] == UNALIGNED_START
    # 主区间不动：首根仍是 ceil(start)，不含跨 start 那根
    assert body["data_manifest"]["kline_count"] == MAIN_COUNT


@pytest.mark.parametrize("tool_id,params", [(RSI_TOOL, RSI_PARAMS), (LEDGER_TOOL, LEDGER_PARAMS)],
                         ids=["engine", "ledger"])
def test_reported_warmup_bars_equals_fed_rows(client, monkeypatch, tool_id, params):
    full = _full_df(CEIL_START)
    monkeypatch.setattr(provider, "_fetch_ohlcv", _central_fetch(full))
    captured: dict = {}
    real = provider._fetch_template_warmup

    def spy(*args, **kwargs):
        warm = real(*args, **kwargs)
        captured["warm"] = warm
        captured["main_first"] = _epoch(args[6].index[0])
        return warm

    monkeypatch.setattr(provider, "_fetch_template_warmup", spy)
    fed: dict = {}
    real_wrap = provider._exit_kind_strategy

    def wrap(strategy_class):
        fed["cols"] = getattr(strategy_class, "_warmup_cols", None)
        return real_wrap(strategy_class)

    monkeypatch.setattr(provider, "_exit_kind_strategy", wrap)
    body = _post(client, tool_id, params, UNALIGNED_START, CEIL_START + MAIN_COUNT * HOUR)
    warm = captured["warm"]
    reported = body["assumptions"]["indicator_warmup_bars"]
    assert reported == len(warm) == BARS
    assert _epoch(warm.index[-1]) == H0 and _epoch(warm.index[-1]) + HOUR == captured["main_first"]
    if tool_id == RSI_TOOL:
        assert len(fed["cols"]["Close"]) == reported
        assert list(fed["cols"]["Close"]) == [float(v) for v in warm["Close"]]
