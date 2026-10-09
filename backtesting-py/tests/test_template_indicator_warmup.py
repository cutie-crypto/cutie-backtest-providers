"""1008：内置模板指标预热（start_at 之前取 min_bars 根，只用于算指标）。

生产实证：BTCUSDT 日线 RSI(14) 30/70，区间开头 43 根因 len(self.data) < min_bars 一律
不能开仓，11 月的超卖信号被丢掉；服务端实盘监听带预热，回测与实盘口径不一致。
这里用合成 K 线（不联网）覆盖：有/无预热两条守卫分支、预热取数失败退化、结果只覆盖
主区间、指标与「完整序列直接算」一致。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cutie_backtesting_provider as provider  # noqa: E402

DAY = 86400
START_AT = 1735689600  # 2025-01-01 00:00 UTC
WARMUP_COUNT = 120
MAIN_COUNT = 80
END_AT = START_AT + MAIN_COUNT * DAY


def _closes() -> list[float]:
    """预热段在 100 附近小幅震荡（RSI≈50）；主区间开头连跌（RSI<30），之后震荡上行。"""
    warm = [100.0 + (0.5 if i % 2 else -0.5) for i in range(WARMUP_COUNT)]
    main = [92.0, 85.0, 79.0, 74.0, 70.0]
    price = main[-1]
    while len(main) < MAIN_COUNT:
        price += 1.0 if len(main) % 2 else -0.5
        main.append(price)
    return warm + main


def _full_df() -> pd.DataFrame:
    closes = _closes()
    first = START_AT - WARMUP_COUNT * DAY
    idx = pd.DatetimeIndex([pd.to_datetime((first + i * DAY) * 1000, unit="ms") for i in range(len(closes))])
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [c + 1.5 for c in closes],
            "Low": [c - 1.5 for c in closes],
            "Close": closes,
            "Volume": [10.0] * len(closes),
        },
        index=idx,
    )


def _range_fetch(full: pd.DataFrame, calls: list | None = None):
    def fetch(exchange, market, symbol, timeframe, start, end):
        if calls is not None:
            calls.append((start, end))
        lo = pd.to_datetime(start * 1000, unit="ms")
        hi = pd.to_datetime(end * 1000, unit="ms")
        out = full.loc[(full.index >= lo) & (full.index < hi)].copy()
        out.attrs["cutie_data_source"] = "cutie_central_market_data"
        out.attrs["cutie_central_market_data_used"] = True
        out.attrs["cutie_market_data_cache_hit"] = False
        return out

    return fetch


@pytest.fixture()
def client(monkeypatch, tmp_path):
    from backtesting import Backtest

    monkeypatch.setattr(provider, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(Backtest, "plot", lambda self, **kwargs: None)
    return TestClient(provider.app)


def _post(client, tool_id: str, params: dict, run_id: str = "warmup_1") -> dict:
    resp = client.post("/cutie/backtest", json={
        "backtest": {
            "run_id": run_id,
            "provider_tool_id": tool_id,
            "provider_params": {"exchange": "binance", **params},
            "symbol": "BTCUSDT",
            "market": "spot",
            "timeframe": "1d",
            "start_at": START_AT,
            "end_at": END_AT,
            "initial_capital": "10000",
            "fee_bps": "10",
            "slippage_bps": "5",
        },
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["result_status"] == "success", body
    return body


def _to_epoch(value) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    return int(pd.Timestamp(value).timestamp())


RSI_TOOL = "local.backtesting_py.rsi_reversal"
RSI_PARAMS = {"rsi_period": 14, "oversold": 30, "overbought": 70}


def test_rsi_with_warmup_trades_in_first_bars_without_warmup_does_not(client, monkeypatch):
    full = _full_df()
    calls: list = []
    monkeypatch.setattr(provider, "_fetch_ohlcv", _range_fetch(full, calls))
    warm = _post(client, RSI_TOOL, RSI_PARAMS)

    min_bars = 3 * 14 + 1
    assert warm["assumptions"]["indicator_warmup_bars"] == min_bars
    # 预热段单独取，取数留两根余量；实际指标仍只使用最后min_bars根。
    assert (START_AT - (min_bars + provider._TEMPLATE_WARMUP_FETCH_EXTRA_BARS) * DAY, START_AT) in calls
    assert warm["trades"], "主区间开头 RSI<30，有预热时应在第一段就开仓"
    first_open = _to_epoch(warm["trades"][0]["opened_at"])
    assert START_AT <= first_open < START_AT + 6 * DAY

    # 新币：start_at 之前没有任何 K 线 -> 守卫退回旧行为，前 43 根不能交易
    main_only = full.loc[full.index >= pd.to_datetime(START_AT * 1000, unit="ms")]
    monkeypatch.setattr(provider, "_fetch_ohlcv", _range_fetch(main_only))
    cold = _post(client, RSI_TOOL, RSI_PARAMS)
    assert cold["assumptions"]["indicator_warmup_bars"] == 0
    assert cold["trades"] == []


def _comparable(body: dict) -> dict:
    return {
        "trades": body["trades"],
        "metrics": body["metrics"],
        "equity_curve": body["equity_curve"],
        "data_manifest": body["data_manifest"],
        "legacy_metrics": body["raw_report"]["legacy_metrics"],
    }


def test_warmup_fetch_failure_degrades_to_no_warmup(client, monkeypatch):
    full = _full_df()
    main_only = full.loc[full.index >= pd.to_datetime(START_AT * 1000, unit="ms")]
    monkeypatch.setattr(provider, "_fetch_ohlcv", _range_fetch(main_only))
    baseline = _post(client, RSI_TOOL, RSI_PARAMS)

    good = _range_fetch(full)

    def flaky(exchange, market, symbol, timeframe, start, end):
        if start < START_AT:
            raise RuntimeError("RATE_LIMITED: central market data gap")
        return good(exchange, market, symbol, timeframe, start, end)

    monkeypatch.setattr(provider, "_fetch_ohlcv", flaky)
    degraded = _post(client, RSI_TOOL, RSI_PARAMS)
    assert degraded["assumptions"]["indicator_warmup_bars"] == 0
    assert _comparable(degraded) == _comparable(baseline)


def test_range_ignoring_fetch_never_reuses_main_rows_as_warmup(client, monkeypatch):
    full = _full_df()
    main_only = full.loc[full.index >= pd.to_datetime(START_AT * 1000, unit="ms")].copy()
    monkeypatch.setattr(provider, "_fetch_ohlcv", lambda *a, **kw: main_only)
    body = _post(client, RSI_TOOL, RSI_PARAMS)
    assert body["assumptions"]["indicator_warmup_bars"] == 0
    assert body["trades"] == []


@pytest.mark.parametrize(
    "tool_id,params",
    [
        ("local.backtesting_py.ema_cross", {"ema_fast": 5, "ema_slow": 20}),
        (RSI_TOOL, RSI_PARAMS),
        ("local.backtesting_py.bollinger_reversal", {"bb_period": 20, "bb_std": 2.0}),
        ("local.backtesting_py.bollinger_breakout", {"bb_period": 20, "bb_std": 2.0}),
        ("local.backtesting_py.breakout", {"lookback": 20, "exit_lookback": 10}),
        ("local.backtesting_py.volume_breakout", {"lookback": 20, "volume_multiple": 2, "volume_avg_period": 20, "exit_ema": 20}),
        ("local.backtesting_py.macd", {"fast": 5, "slow": 10, "signal": 4}),
        ("local.backtesting_py.cci_rsi", {"cci_period": 10, "rsi_period": 10}),
        ("local.backtesting_py.ema_rsi_pullback", {"ema_period": 50, "rsi_period": 10, "rsi_entry": 40, "rsi_exit": 70}),
        ("local.backtesting_py.bias_reversion", {"ema_period": 20, "bias_entry_pct": 3}),
        ("local.backtesting_py.roc", {"roc_period": 12, "entry_threshold": 1, "exit_threshold": 0}),
        ("local.backtesting_py.supertrend", {"atr_period": 10, "multiplier": 3}),
        ("local.backtesting_py.ema_trend_rsi", {"ema_fast": 5, "ema_slow": 20, "rsi_period": 14}),
        ("local.backtesting_py.ema_pullback", {"ema_fast": 5, "ema_slow": 30}),
    ],
)
def test_warmup_results_cover_main_range_only(client, monkeypatch, tool_id, params):
    full = _full_df()
    monkeypatch.setattr(provider, "_fetch_ohlcv", _range_fetch(full))
    body = _post(client, tool_id, params)

    min_bars = int(provider.TOOL_SPECS[tool_id]["build"](params)["min_bars"])
    assert body["assumptions"]["indicator_warmup_bars"] == min_bars
    for trade in body["trades"]:
        assert _to_epoch(trade["opened_at"]) >= START_AT
        assert _to_epoch(trade["closed_at"]) <= END_AT
    curve = body["equity_curve"]
    assert curve, "权益曲线不能为空"
    point = curve[0]
    ts_key = next(k for k in ("ts", "timestamp", "time", "at", "bar_time") if k in point)
    assert _to_epoch(point[ts_key]) == START_AT
    assert body["data_manifest"]["kline_count"] == MAIN_COUNT
    assert body["data_manifest"]["start_at"] == START_AT
    assert f", {MAIN_COUNT} candles," in body["raw_report"]["provider_summary"]


def _run_strategy(tool_id: str, params: dict, main: pd.DataFrame, warmup: pd.DataFrame | None):
    from backtesting import Backtest

    strategy_class = provider.TOOL_SPECS[tool_id]["build"](params)["strategy"]
    if warmup is not None:
        strategy_class._warmup_bars = len(warmup)
        strategy_class._warmup_cols = {c: warmup[c].to_numpy(dtype="float64") for c in provider._WARMUP_COLUMNS}
    bt = Backtest(main, strategy_class, cash=10_000_000, commission=0.0, exclusive_orders=True, finalize_trades=True)
    return bt.run()._strategy


def test_ema_cross_and_macd_indicators_match_full_series():
    full = _full_df()
    start = pd.to_datetime(START_AT * 1000, unit="ms")
    main = full.loc[full.index >= start]
    warmup = full.loc[full.index < start].tail(30)
    joined = pd.concat([warmup, main])["Close"].astype("float64")

    ema = _run_strategy("local.backtesting_py.ema_cross", {"ema_fast": 5, "ema_slow": 20}, main, warmup)
    for attr, span in (("fast_ema", 5), ("slow_ema", 20)):
        expected = joined.ewm(span=span, adjust=False).mean().to_numpy()[-MAIN_COUNT:]
        np.testing.assert_allclose(np.asarray(getattr(ema, attr), dtype="float64"), expected, rtol=0, atol=1e-12)

    macd = _run_strategy("local.backtesting_py.macd", {"fast": 5, "slow": 10, "signal": 4}, main, warmup)
    line = joined.ewm(span=5, adjust=False).mean() - joined.ewm(span=10, adjust=False).mean()
    np.testing.assert_allclose(np.asarray(macd.macd, dtype="float64"), line.to_numpy()[-MAIN_COUNT:], atol=1e-12)
    np.testing.assert_allclose(
        np.asarray(macd.signal, dtype="float64"),
        line.ewm(span=4, adjust=False).mean().to_numpy()[-MAIN_COUNT:],
        atol=1e-12,
    )

    # 无预热：仍是主区间自身起算（与改动前同一公式）
    cold = _run_strategy("local.backtesting_py.ema_cross", {"ema_fast": 5, "ema_slow": 20}, main, None)
    expected_cold = main["Close"].astype("float64").ewm(span=5, adjust=False).mean().to_numpy()
    np.testing.assert_allclose(np.asarray(cold.fast_ema, dtype="float64"), expected_cold, atol=1e-12)
    assert not math.isclose(float(cold.fast_ema[0]), float(ema.fast_ema[0]))


def test_warm_is_identity_without_warmup():
    strategy_class = provider.TOOL_SPECS[RSI_TOOL]["build"](RSI_PARAMS)["strategy"]
    assert strategy_class._warmup_bars == 0
    func = lambda x: x  # noqa: E731
    assert strategy_class._warm(strategy_class, func, "Close") is func


def _fetch_daily_warmup_at_exchange_boundaries(monkeypatch, start_sec):
    """交易所按since向上取桶，中心数据只含end前完整收盘的K线。"""
    full, calls = _full_df(), []
    fetch = _range_fetch(full, calls)

    def closed_fetch(exchange, market, symbol, timeframe, since, end):
        rows = fetch(exchange, market, symbol, timeframe, since, end)
        return rows.loc[rows.index + pd.Timedelta(seconds=DAY) <= pd.to_datetime(end, unit="s")]

    monkeypatch.setattr(provider, "_fetch_ohlcv", closed_fetch)
    first = pd.to_datetime(((start_sec + DAY - 1) // DAY) * DAY, unit="s")
    main_df = full.loc[full.index >= first]
    warm = provider._fetch_template_warmup("binance", "spot", "BTCUSDT", "1d", start_sec, 43, main_df)
    return warm, main_df, calls


def test_unaligned_daily_start_fetches_all_43_warmup_candles(monkeypatch):
    """不对齐2722秒时也足43根，且所有预热行严格早于主区间首根。"""
    warm, main, _ = _fetch_daily_warmup_at_exchange_boundaries(monkeypatch, START_AT + 2722)
    assert len(warm) == 43
    assert (warm.index < main.index[0]).all()


def test_aligned_daily_start_keeps_exact_warmup_count(monkeypatch):
    """对齐起点取数即使有余量也只保留43根，不把主区间K线混进预热。"""
    warm, main, _ = _fetch_daily_warmup_at_exchange_boundaries(monkeypatch, START_AT)
    assert len(warm) == 43
    assert (warm.index < main.index[0]).all()


@pytest.mark.parametrize("offset", [0, 2722])
def test_warmup_since_is_no_later_than_aligned_bucket_minus_requested_bars(monkeypatch, offset):
    """取数起点必须不晚于对齐桶减43周期，不能仅靠tail裁剪补根数。"""
    _, _, calls = _fetch_daily_warmup_at_exchange_boundaries(monkeypatch, START_AT + offset)
    assert len(calls) == 1
    assert calls[0][0] <= START_AT - 43 * DAY
