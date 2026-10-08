"""132 RSI 定额分批（IMPL §3.1）：专用 Decimal 现货账本 + 新 tool 分发。

覆盖：Jessie 形态逐字段手算 3 根、朴素 float 参照实现对拍、现金不足跳过、卖完为止、
期末强平 closed_at == end_at、五条不变量逐根断言（含注入违规必须报错）、拒绝条件、
预热不足失败、catalog 不带固定风控字段。合成 K 线，不联网。
"""
from __future__ import annotations

import sys
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cutie_backtesting_provider as provider  # noqa: E402
from scale_in_out_ledger import (  # noqa: E402
    LedgerBar,
    LedgerInvariantError,
    ScaleInOutLedger,
    floor_qty,
    round_cash,
    run_scale_in_out,
    threshold_signal,
)

TOOL = "local.backtesting_py.rsi_scale_in_out"
DAY = 86400
START_AT = 1735689600  # 2025-01-01 00:00 UTC
WARM = 43  # 3 * 14 + 1
PAD = 30  # 主区间开头的中性段（主区间要满 min_bars 根）
D = Decimal


# ---------------------------------------------------------------------------
# 数据
# ---------------------------------------------------------------------------

def _osc(n: int) -> list[float]:
    return [100.0 + (0.5 if i % 2 else -0.5) for i in range(n)]


# Jessie 形态：设计段第 3/4/5 根 RSI(14) < 30，第 10..14 根 > 70（见 test 内断言）。
DESIGNED_CLOSES = [100, 96, 92, 88, 86, 85, 90, 97, 105, 112, 120, 126, 128, 129, 127, 124]
# 成交那几根的开盘价取整数，数量 = 100 / 开盘价可整除，便于手算。
DESIGNED_OPENS = {4: 80, 5: 100, 6: 125, 11: 125, 12: 200, 13: 250, 14: 400, 15: 500}


def _jessie_frame() -> pd.DataFrame:
    closes = [float(c) for c in _osc(WARM + PAD) + DESIGNED_CLOSES]
    opens = list(closes)
    for d, value in DESIGNED_OPENS.items():
        opens[WARM + PAD + d] = float(value)
    first = START_AT - WARM * DAY
    idx = pd.DatetimeIndex([pd.to_datetime((first + i * DAY) * 1000, unit="ms") for i in range(len(closes))])
    return pd.DataFrame(
        {
            "Open": opens,
            "High": [max(o, c) + 1 for o, c in zip(opens, closes)],
            "Low": [min(o, c) - 1 for o, c in zip(opens, closes)],
            "Close": closes,
            "Volume": [10.0] * len(closes),
        },
        index=idx,
    )


MAIN_COUNT = PAD + len(DESIGNED_CLOSES)
END_AT = START_AT + MAIN_COUNT * DAY  # 最后一根的收盘时刻（与 test_template_indicator_warmup 同口径）


def _open_ts(main_index: int) -> int:
    return START_AT + main_index * DAY


def _range_fetch(full: pd.DataFrame):
    def fetch(exchange, market, symbol, timeframe, start, end):
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
    monkeypatch.setattr(provider, "REPORTS_DIR", tmp_path / "reports")
    return TestClient(provider.app)


JESSIE_PARAMS = {"rsi_period": 14, "oversold": 30, "overbought": 70, "buy_notional": 100, "sell_notional": 100}


def _post(client, params=None, *, market="spot", extra=None, tool=TOOL) -> dict:
    payload = {
        "run_id": "scale_1",
        "provider_tool_id": tool,
        "provider_params": {"exchange": "binance", **(JESSIE_PARAMS if params is None else params)},
        "symbol": "BTCUSDT",
        "market": market,
        "timeframe": "1d",
        "start_at": START_AT,
        "end_at": END_AT,
        "initial_capital": "10000",
        "fee_bps": "10",
        "slippage_bps": "5",
        **(extra or {}),
    }
    resp = client.post("/cutie/backtest", json={"backtest": payload})
    assert resp.status_code == 200
    return resp.json()


def _bars(opens, closes, start=START_AT) -> list[LedgerBar]:
    return [
        LedgerBar(open_time=start + i * DAY, close_time=start + (i + 1) * DAY, open=D(str(o)), close=D(str(c)))
        for i, (o, c) in enumerate(zip(opens, closes))
    ]


def _script(signals: list[str]):
    return lambda i: signals[i]


def _run(bars, signals, *, capital="10000", buy="100", sell="100", fee="10", slip="5", end_at=None):
    return run_scale_in_out(
        bars, _script(signals) if isinstance(signals, list) else signals,
        initial_capital=D(capital), buy_notional=D(buy), sell_notional=D(sell),
        fee_bps=D(fee), slippage_bps=D(slip),
        start_at=bars[0].open_time, end_at=bars[-1].close_time if end_at is None else end_at,
    )


# ---------------------------------------------------------------------------
# Jessie 形态：RSI(14) 低于 30 买 100、高于 70 卖 100，日线，手算 3 根逐字段
# ---------------------------------------------------------------------------

def test_jessie_shape_three_bars_hand_computed(client, monkeypatch):
    full = _jessie_frame()
    rsi = provider._rsi_series(full["Close"].to_numpy(), 14)[WARM:]
    assert [i - PAD for i, r in enumerate(rsi) if r < 30] == [3, 4, 5]
    assert [i - PAD for i, r in enumerate(rsi[:-1]) if r > 70] == [10, 11, 12, 13, 14]
    monkeypatch.setattr(provider, "_fetch_ohlcv", _range_fetch(full))
    body = _post(client)
    assert body["result_status"] == "success", body

    a = body["assumptions"]
    assert a["position_mode"] == "scale_in_out"
    assert (a["buy_fills"], a["sell_fills"], a["skipped_buys_insufficient_cash"]) == (3, 5, 0)
    assert a["indicator_warmup_bars"] == WARM

    trades = body["trades"]
    assert [t["seq"] for t in trades] == list(range(1, 9))
    assert all(set(t) == {"seq", "opened_at", "closed_at", "side", "qty", "entry_price",
                          "exit_price", "fee", "slippage", "pnl"} for t in trades)
    # 定点口径（数量 12 位、金额 8 位）：本段成交价都让数量与费用恰好落在格点上，所以量化不改值。
    # 买入：开盘 80 → 数量 1.250000000000，成交额 100，开仓费 0.10000000、滑点 0.05000000，
    #   现金 10000 → 9899.85000000；开盘 100 → 1.000000000000，现金 9799.70000000；
    #   开盘 125 → 0.800000000000，现金 9699.55000000。
    # 手算第 1 根（设计段第 11 根开盘 125 卖 100 美元 = 0.800000000000 个，FIFO 拆第一批 1.25 个）：
    #   分摊开仓费 round(80*0.8*0.001)=0.06400000、平仓费 round(125*0.8*0.001)=0.10000000 → fee 0.164；
    #   滑点 0.03200000 + 0.05000000 = 0.082；pnl = 45*0.8 - 0.246 = 35.754；
    #   现金 + 100 - 0.15 = 9799.40000000。
    assert trades[0] == {
        "seq": 1, "opened_at": _open_ts(PAD + 4), "closed_at": _open_ts(PAD + 11), "side": "long",
        "qty": "0.8", "entry_price": "80", "exit_price": "125",
        "fee": "0.164", "slippage": "0.082", "pnl": "35.754",
    }
    # 手算第 2 根（设计段第 12 根开盘 200 卖 0.500000000000 个：第一批剩 0.45 + 第二批 0.05）
    #   第一批最后一部分拿走剩余开仓费 0.1-0.064=0.03600000、滑点 0.05-0.032=0.01800000；
    #   平仓费 round(200*0.45*0.001)=0.09000000、滑点 0.04500000 → fee 0.126、slippage 0.063；
    #   第二批 0.05：开仓分摊 0.00500000 / 0.00250000，平仓 0.01000000 / 0.00500000；
    #   现金 + 100 - 0.15 = 9899.25000000。
    assert trades[1] == {
        "seq": 2, "opened_at": _open_ts(PAD + 4), "closed_at": _open_ts(PAD + 12), "side": "long",
        "qty": "0.45", "entry_price": "80", "exit_price": "200",
        "fee": "0.126", "slippage": "0.063", "pnl": "53.811",
    }
    assert trades[2] == {
        "seq": 3, "opened_at": _open_ts(PAD + 5), "closed_at": _open_ts(PAD + 12), "side": "long",
        "qty": "0.05", "entry_price": "100", "exit_price": "200",
        "fee": "0.015", "slippage": "0.0075", "pnl": "4.9775",
    }
    # 设计段第 15 根（最后一根）开盘 500 照常成交卖 0.2 个；之后第二批剩 0.1、第三批 0.8，
    # 按最后一根收盘 124 强平，closed_at == end_at，且只有强平批次落在 end_at 上
    tail = [t for t in trades if t["closed_at"] == END_AT]
    assert [(t["opened_at"], t["qty"], t["exit_price"]) for t in tail] == [
        (_open_ts(PAD + 5), "0.1", "124"), (_open_ts(PAD + 6), "0.8", "124"),
    ]

    curve = {p["ts"]: p["equity"] for p in body["equity_curve"]}
    assert body["equity_curve"][0] == {"ts": START_AT, "equity": "10000"}
    # 手算第 3 根：设计段第 4 根开盘 80 买入 1.25 个后，权益 = 10000 - 100*0.0015
    assert curve[_open_ts(PAD + 4)] == "9999.85"
    # 该根收盘点（ts 与下一根开盘相同）被下一根开盘买入成交点覆盖（成交点优先）：
    # 开盘 100 买 1 个后 = 10000 + (100-80)*1.25 - 0.15 - 0.15
    assert curve[_open_ts(PAD + 5)] == "10024.7"
    # 卖出时刻：已实现 35.754 + 剩余批次按同一开盘价 125 估值（D8）
    assert curve[_open_ts(PAD + 11)] == "10080.65"
    assert curve[_open_ts(PAD + 12)] == "10249.25"
    ts_list = [p["ts"] for p in body["equity_curve"]]
    assert ts_list == sorted(set(ts_list))
    final = D("10000") + sum(D(t["pnl"]) for t in trades)
    assert body["equity_curve"][-1] == {"ts": END_AT, "equity": format(final.normalize(), "f")}
    assert body["metrics"]["trade_count"] == 8
    assert D(body["metrics"]["total_return"]) == (final - D("10000")) / D("10000")
    assert body["raw_report"]["legacy_metrics"]["trade_count"] == 8
    assert "strategy_signal_result" not in body["raw_report"]


def test_bar_close_point_is_mark_to_market():
    """无下一根成交覆盖时，收盘点 = 现金 + 持仓 × 收盘价。"""
    bars = _bars([100, 80, 90, 95], [100, 86, 92, 96])
    result = _run(bars, ["buy", "hold", "hold", "hold"])
    points = dict(result.equity_points)
    # 第 1 根开盘 80 买 1.25 个：现金 9899.85，收盘 86 → 9899.85 + 107.5
    assert points[bars[1].close_time] == D("10007.35")
    assert points[bars[2].close_time] == D("9899.85") + D("1.25") * D("92")


# ---------------------------------------------------------------------------
# 朴素 float 参照实现对拍（逐根循环，≤20 行）
# ---------------------------------------------------------------------------

def _naive(opens, closes, rsi, os_, ob, cash, buy, sell, b):
    lots, trades, pend = [], [], "hold"
    for i, (o, c) in enumerate(zip(opens, closes)):
        if pend == "buy" and cash >= buy * (1 + b):
            lots.append([i, o, buy / o])
            cash -= buy * (1 + b)
        elif pend == "sell" and lots:
            left = min(sell / o, sum(lot[2] for lot in lots))
            while left > 1e-12 and lots:
                lot = lots[0]
                q = min(left, lot[2])
                left, lot[2] = left - q, lot[2] - q
                trades.append((lot[0], i, q, lot[1], o))
                cash += q * o * (1 - b)
                if lot[2] <= 1e-12:
                    lots.pop(0)
        last = i == len(opens) - 1
        pend = "hold" if last else ("buy" if rsi[i] < os_ else "sell" if rsi[i] > ob else "hold")
    for lot in lots:
        trades.append((lot[0], len(opens), lot[2], lot[1], closes[-1]))
        cash += lot[2] * closes[-1] * (1 - b)
    return trades, cash


@pytest.mark.parametrize("seed", [7, 11, 2026])
def test_ledger_matches_naive_reference(seed):
    rng = np.random.RandomState(seed)
    closes = list(np.round(100 * np.cumprod(1 + rng.normal(0, 0.03, 400)), 4))
    opens = [round(c * (1 + rng.normal(0, 0.005)), 4) for c in [closes[0]] + closes[:-1]]
    rsi = provider._rsi_series(closes, 14)
    bars = _bars(opens, closes)
    end_at = bars[-1].open_time
    result = _run(bars, threshold_signal(rsi, buy_below=40, sell_above=60),
                  capital="1500", buy="100", sell="150", end_at=end_at)
    trades, cash = _naive(opens, closes, rsi, 40, 60, 1500.0, 100.0, 150.0, 0.0015)
    assert len(trades) > 20 and result.skipped_buys_insufficient_cash > 0
    assert len(result.trades) == len(trades)
    for got, (oi, ci, q, e, x) in zip(result.trades, trades):
        assert got["opened_at"] == bars[oi].open_time
        assert got["closed_at"] == (end_at if ci == len(bars) else bars[ci].open_time)
        assert float(got["qty"]) == pytest.approx(q, rel=1e-9)
        assert (float(got["entry_price"]), float(got["exit_price"])) == (e, x)
    # 参照实现是不量化的 float：数量差不到 1e-12、费用每侧差不到 0.5e-8，现金按 1e-9 相对误差对
    assert float(D("1500") + result.realized_pnl) == pytest.approx(cash, rel=1e-9)
    assert result.final_cash == D("1500") + result.realized_pnl


# ---------------------------------------------------------------------------
# 现金不足 / 卖完为止 / 期末强平
# ---------------------------------------------------------------------------

def test_insufficient_cash_skips_buy_and_counts():
    bars = _bars([100] * 5, [100] * 5)
    # 250 本金：每笔 100 + 0.15 费用，买两笔剩 49.7，第三、四笔跳过
    result = _run(bars, ["buy", "buy", "buy", "buy", "hold"], capital="250")
    assert (result.buy_fills, result.skipped_buys_insufficient_cash) == (2, 2)
    assert all(s.cash >= 0 for s in result.snapshots)
    assert result.snapshots[-1].cash == D("49.70")


def test_sell_all_when_less_than_one_sell_remains():
    bars = _bars([100, 100, 150, 150], [100, 100, 150, 150])
    # 持有 1 个（价值 150）< 卖出金额 300：一次全部卖完，之后的卖信号无持仓不动
    result = _run(bars, ["buy", "sell", "sell", "hold"], sell="300")
    assert [(t["qty"], t["exit_price"], t["closed_at"]) for t in result.trades] == [
        (D("1"), D("150"), bars[2].open_time),
    ]
    assert result.sell_fills == 1
    assert result.snapshots[-1].lot_qty_sum == 0


def test_end_liquidation_uses_last_close_and_end_at():
    bars = _bars([100, 100, 120, 130], [101, 110, 125, 140])
    end_at = bars[-1].open_time + 3600  # end_at 落在最后一根中间
    result = _run(bars, ["buy", "buy", "hold", "hold"], end_at=end_at)
    assert [t["closed_at"] for t in result.trades] == [end_at, end_at]
    assert {t["exit_price"] for t in result.trades} == {D("140")}
    assert result.equity_points[-1] == (end_at, D("10000") + result.realized_pnl)
    assert result.sell_fills == 0  # 期末强平不计入卖出信号成交次数


def test_last_bar_does_not_judge_new_signal():
    calls: list[int] = []

    def signal(i):
        calls.append(i)
        return "buy"

    bars = _bars([100] * 4, [100] * 4)
    _run(bars, signal)
    assert calls == [0, 1, 2]


def test_end_at_on_last_open_skips_fill_that_would_collide_with_liquidation():
    bars = _bars([100, 100, 120, 130], [101, 110, 125, 140])
    result = _run(bars, ["buy", "hold", "sell", "hold"], end_at=bars[-1].open_time)
    # 第 2 根的卖信号要在 end_at 那一刻成交，不判；唯一一条 trade 是强平批次
    assert [(t["closed_at"], t["exit_price"]) for t in result.trades] == [(bars[-1].open_time, D("140"))]
    assert result.sell_fills == 0


# ---------------------------------------------------------------------------
# 五条不变量：逐根断言 + 注入违规必须报错
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seed", [3, 5])
def test_five_invariants_hold_on_every_bar(seed):
    rng = np.random.RandomState(seed)
    closes = list(np.round(50 * np.cumprod(1 + rng.normal(0, 0.04, 300)), 6))
    opens = [closes[0]] + closes[:-1]
    rsi = provider._rsi_series(closes, 6)
    bars = _bars(opens, closes)
    result = _run(bars, threshold_signal(rsi, buy_below=45, sell_above=55), capital="800", sell="130")
    assert result.buy_fills > 5 and result.sell_fills > 5
    for snap in result.snapshots:
        # ① 现金 + Σ批次数量 × 当根价格 = 权益（按已实现 + 浮动净值独立算），精确相等
        assert snap.cash + snap.lot_qty_sum * snap.price == snap.equity_by_pnl == snap.equity_by_cash
        # ② 批次数量和 = 累计买入 − 累计卖出，精确相等
        assert snap.lot_qty_sum == snap.cum_bought - snap.cum_sold
        # ④ 现金永不为负
        assert snap.cash >= 0
        # ⑤ 同根不得既买又卖
        assert not (snap.buys_this_bar and snap.sells_this_bar)
    # ③ FIFO：按成交生成顺序，被卖出批次的 opened_at 单调不降
    opened = [t["opened_at"] for t in result.trades]
    assert opened == sorted(opened)


def _ledger() -> ScaleInOutLedger:
    ledger = ScaleInOutLedger(initial_capital=D("1000"), buy_notional=D("100"), sell_notional=D("100"),
                              fee_bps=D("10"), slippage_bps=D("5"))
    ledger.begin_bar()
    ledger.buy(START_AT, D("100"))
    ledger.buy(START_AT + DAY, D("50"))
    ledger.check_invariants(D("60"), "setup")
    return ledger


def test_invariant_violations_are_raised():
    ledger = _ledger()
    ledger.cash += D("1")  # ① 现金与已实现/浮动不一致
    with pytest.raises(LedgerInvariantError, match="cash\\+holdings"):
        ledger.check_invariants(D("60"), "inject")

    ledger = _ledger()
    ledger.cum_bought += D("0.5")  # ②
    with pytest.raises(LedgerInvariantError, match="lot qty"):
        ledger.check_invariants(D("60"), "inject")

    ledger = _ledger()
    ledger.lots.reverse()  # ③ 后买的批次排到前面
    with pytest.raises(LedgerInvariantError, match="FIFO"):
        ledger.sell(START_AT + 2 * DAY, D("60"))
        ledger.sell(START_AT + 3 * DAY, D("60"))

    ledger = _ledger()
    ledger.initial_capital += D("-1") - ledger.cash  # 同步平移本金，让 ① 仍成立，只剩 ④ 触发
    ledger.cash = D("-1")  # ④
    with pytest.raises(LedgerInvariantError, match="negative"):
        ledger.check_invariants(D("60"), "inject")

    ledger = _ledger()
    ledger.sell(START_AT + 2 * DAY, D("60"))  # ⑤ 同一根里已买过又卖
    with pytest.raises(LedgerInvariantError, match="same bar"):
        ledger.check_invariants(D("60"), "inject")


def test_threshold_signal_rejects_overlapping_conditions():
    signal = threshold_signal([50.0], buy_below=60, sell_above=40)
    with pytest.raises(LedgerInvariantError, match="both hold"):
        signal(0)


# ---------------------------------------------------------------------------
# 参数校验与拒绝条件
# ---------------------------------------------------------------------------

def test_oversold_not_below_overbought_is_rejected():
    for oversold, overbought in [(70, 70), (80, 30)]:
        with pytest.raises(ValueError, match="INVALID_PARAMS:require 0 < oversold < overbought"):
            provider._build_rsi_scale_in_out({**JESSIE_PARAMS, "oversold": oversold, "overbought": overbought})


@pytest.mark.parametrize("params", [
    {**JESSIE_PARAMS, "oversold": 60},  # schema：oversold ≤ 49
    {k: v for k, v in JESSIE_PARAMS.items() if k != "buy_notional"},
    {**JESSIE_PARAMS, "sell_notional": 0},
])
def test_invalid_params_over_http(client, monkeypatch, params):
    monkeypatch.setattr(provider, "_fetch_ohlcv", _range_fetch(_jessie_frame()))
    body = _post(client, params)
    assert (body["result_status"], body["error_type"]) == ("failed", "INVALID_PARAMS")


@pytest.mark.parametrize("market,params,extra,needle", [
    ("futures", None, None, "spot market only"),
    ("spot", {**JESSIE_PARAMS, "stop_loss_pct": 5}, None, "stop_loss_pct"),
    ("spot", {**JESSIE_PARAMS, "position_size_notional": 500}, None, "position_size_notional"),
    ("spot", None, {"risk_policy": {"schema": "x"}}, "risk_policy"),
    ("spot", None, {"signal_execution": {"schema": "x"}}, "signal_execution"),
])
def test_rejections_use_failure_contract(client, monkeypatch, market, params, extra, needle):
    monkeypatch.setattr(provider, "_fetch_ohlcv", _range_fetch(_jessie_frame()))
    body = _post(client, params, market=market, extra=extra)
    assert (body["result_status"], body["error_type"]) == ("failed", "INVALID_PARAMS")
    assert needle in body["error_message"]
    assert {"assumptions", "limitations", "raw_report"} <= set(body)


def test_missing_warmup_fails_instead_of_running_cold(client, monkeypatch):
    full = _jessie_frame()
    main_only = full.loc[full.index >= pd.to_datetime(START_AT * 1000, unit="ms")]
    monkeypatch.setattr(provider, "_fetch_ohlcv", _range_fetch(main_only))
    body = _post(client)
    assert (body["result_status"], body["error_type"]) == ("failed", "INSUFFICIENT_DATA")
    assert body["limitations"]["reason"] == "indicator_warmup_unavailable"

    partial = full.iloc[5:]  # 只拿到 38 / 43 根预热
    monkeypatch.setattr(provider, "_fetch_ohlcv", _range_fetch(partial))
    body = _post(client)
    assert body["error_type"] == "INSUFFICIENT_DATA" and "38 of 43" in body["error_message"]


# ---------------------------------------------------------------------------
# catalog
# ---------------------------------------------------------------------------

def test_catalog_entry_is_spot_only_without_fixed_risk_fields():
    spec = provider.TOOL_SPECS[TOOL]
    entry = provider._catalog_tool(TOOL, spec, ["BTCUSDT"])
    props = entry["param_schema"]["properties"]
    assert set(props) == {"rsi_period", "oversold", "overbought", "buy_notional", "sell_notional", "exchange"}
    assert not set(props) & set(provider._FIXED_RISK_PARAM_SCHEMA_PROPERTIES)
    assert entry["markets"] == ["spot"]
    old = provider._catalog_tool("local.backtesting_py.rsi_reversal",
                                 provider.TOOL_SPECS["local.backtesting_py.rsi_reversal"], ["BTCUSDT"])
    assert old["markets"] == ["spot", "futures"]
    assert set(provider._FIXED_RISK_PARAM_SCHEMA_PROPERTIES) <= set(old["param_schema"]["properties"])


# ---------------------------------------------------------------------------
# Decimal 边界回归（Codex review 四条 P2）
# ---------------------------------------------------------------------------

def test_fifo_boundary_is_exact_under_quantization():
    """价恒 3：买两笔 100、卖两笔 50（一轮 Codex 反例）。定点口径下每批 33.333333333333、每次卖
    16.666666666666，两次合计比整批少一个数量最小单位，首批精确剩 0.000000000001，期末强平成
    一条——这是真实持仓（量化余量），不是舍入尾数；没有任何一次卖出跨到第二批。"""
    bars = _bars([3] * 5, [3, 3, 3, 3, 4])
    result = _run(bars, ["buy", "buy", "sell", "sell", "hold"], capital="1000", sell="50")
    lot = D("33.333333333333")
    half = D("16.666666666666")
    assert [(t["opened_at"], t["closed_at"], t["qty"]) for t in result.trades] == [
        (bars[1].open_time, bars[3].open_time, half),
        (bars[1].open_time, bars[4].open_time, half),
        (bars[1].open_time, bars[4].close_time, lot - 2 * half),
        (bars[2].open_time, bars[4].close_time, lot),
    ]
    assert lot - 2 * half == D("0.000000000001")
    # 开仓费用分摊之和精确等于该批实付（0.09999999999999 → 0.1 / 0.05）
    first_lot = [t for t in result.trades if t["opened_at"] == bars[1].open_time]
    exit_fees = [round_cash(t["exit_price"] * t["qty"] * D("0.001")) for t in first_lot]
    assert sum(t["fee"] for t in first_lot) - sum(exit_fees) == D("0.1")


def test_exact_cash_for_one_buy_is_not_treated_as_insufficient():
    bars = _bars([26134.51] * 7, [26134.51] * 7)
    result = _run(bars, ["buy"] * 6 + ["hold"], capital="540.58", buy="540.58", fee="0", slip="0")
    assert (result.buy_fills, result.skipped_buys_insufficient_cash) == (1, 5)
    qty = floor_qty(D("540.58"), D("26134.51"))
    assert qty == D("0.020684527852")
    # 实际成交额 = 数量 × 价格，比参数金额少不到 价格 × 1e-12；剩余现金精确
    assert result.snapshots[1].cash == D("540.58") - qty * D("26134.51")
    assert 0 <= result.snapshots[1].cash < D("26134.51") * D("1e-12")
    assert len(result.trades) == 1


def test_tiny_notional_partial_sell_is_not_turned_into_full_exit():
    """一轮反例原数字（买 5e-13 个）低于数量最小单位、不成交，见 test_quantized_qty_and_cash_rules；
    这里在最小单位上重述：持 5e-12、卖 1e-12，只卖 1e-12。"""
    bars = _bars([100000] * 3, [100000] * 3)
    result = _run(bars, ["buy", "sell", "hold"], capital="1", buy="0.0000005", sell="0.0000001",
                  fee="0", slip="0")
    assert [t["qty"] for t in result.trades] == [D("1E-12"), D("4E-12")]
    assert result.trades[1]["closed_at"] == bars[-1].close_time


def test_huge_notional_does_not_trip_scaled_invariants():
    bars = _bars([3.14, 3.14, 2.97, 2.97], [3.14, 3.14, 2.97, 2.97])
    result = _run(bars, ["buy", "sell", "hold", "hold"], capital="1E17", buy="1E17", fee="0", slip="0")
    assert result.trades[0]["qty"] == floor_qty(D("100"), D("2.97")) == D("33.670033670033")
    assert len(result.trades) == 2


# ---------------------------------------------------------------------------
# 容差按 ulp 推导（Codex 二轮复核两条 P2）：可精确表示的真实差额不得被吞掉
# ---------------------------------------------------------------------------

def test_decision_tolerance_keeps_exactly_representable_remainder():
    big = D("1E18")
    bars = _bars([1, 1, 1, 2], [1, 1, 1, 2])
    result = _run(bars, ["buy", "sell", "hold", "hold"], capital=str(big + 1), buy=str(big + 1),
                  sell=str(big), fee="0", slip="0")
    assert [(t["qty"], t["exit_price"]) for t in result.trades] == [(big, D("1")), (D("1"), D("2"))]
    assert sum(1 for t in result.trades if t["pnl"] > 0) == 1


def test_split_boundary_sells_exact_target_across_lots():
    big = D("1E18")
    bars = _bars([1] * 5, [1] * 5)
    result = _run(bars, ["buy", "buy", "sell", "hold", "hold"], capital=str(2 * big), buy=str(big - 1),
                  sell=str(big), fee="0", slip="0")
    sold = [t for t in result.trades if t["closed_at"] == bars[3].open_time]
    assert [t["qty"] for t in sold] == [big - 1, D("1")]
    assert result.trades[-1]["qty"] == big - 2


def _big_ledger() -> ScaleInOutLedger:
    ledger = ScaleInOutLedger(initial_capital=D("1E17"), buy_notional=D("1E17"), sell_notional=D("100"),
                              fee_bps=D("0"), slippage_bps=D("0"))
    ledger.begin_bar()
    ledger.buy(START_AT, D("1"))
    ledger.check_invariants(D("1"), "setup")
    return ledger


def test_scaled_tolerance_still_catches_cent_level_errors_at_1e17():
    ledger = _big_ledger()
    ledger.cash += D("0.01")
    with pytest.raises(LedgerInvariantError, match="cash\\+holdings"):
        ledger.check_invariants(D("1"), "inject")
    ledger = _big_ledger()
    ledger.cum_bought += D("0.01")
    with pytest.raises(LedgerInvariantError, match="lot qty"):
        ledger.check_invariants(D("1"), "inject")


# ---------------------------------------------------------------------------
# 定点量化（统领三轮定：数量 1e-12 向下取整、金额 1e-8、精度 60、比较全部精确）
# ---------------------------------------------------------------------------

def test_zero_fee_small_sell_out_of_huge_holding_is_exact():
    bars = _bars([1] * 4, [1] * 4)
    result = _run(bars, ["buy", "sell", "hold", "hold"], capital="1E25", buy="1E25", sell="0.01",
                  fee="0", slip="0")
    assert [(t["closed_at"], t["qty"]) for t in result.trades] == [
        (bars[2].open_time, D("0.01")), (bars[-1].close_time, D("1E25") - D("0.01")),
    ]


def test_hundred_unit_sells_consume_first_lot_exactly():
    """价恒 3、每批买 300（数量 100）、连卖 100 笔各 3 美元（数量 1）：首批恰好卖光。"""
    sig = ["buy", "buy"] + ["sell"] * 100 + ["hold"]
    bars = _bars([3] * len(sig), [3] * len(sig))
    result = _run(bars, sig, capital="1000", buy="300", sell="3")
    assert len(result.trades) == 101
    first = [t for t in result.trades if t["opened_at"] == bars[1].open_time]
    assert len(first) == 100 and all(t["qty"] == 1 for t in first)
    assert D(100) - sum(t["qty"] for t in first) == 0  # 首批余量精确为 0
    assert [(t["qty"], t["closed_at"]) for t in result.trades[100:]] == [(D(100), bars[-1].close_time)]


def test_hundred_notional_sells_leave_exact_quantum_residual():
    """价恒 3、每批买 100、连卖 100 笔各 1 美元：每批 33.333333333333、每笔 0.333333333333，
    首批精确剩 0.000000000033（量化余量，是真实持仓），期末强平成一条，不是舍入尾数。"""
    sig = ["buy", "buy"] + ["sell"] * 100 + ["hold"]
    bars = _bars([3] * len(sig), [3] * len(sig))
    result = _run(bars, sig, capital="1000", buy="100", sell="1")
    first = [t for t in result.trades if t["opened_at"] == bars[1].open_time]
    assert [t["qty"] for t in first[:100]] == [D("0.333333333333")] * 100
    assert [(t["qty"], t["closed_at"]) for t in first[100:]] == [(D("0.000000000033"), bars[-1].close_time)]
    assert len(result.trades) == 102


def test_exact_invariants_catch_tiny_errors_after_100k_bars():
    ledger = ScaleInOutLedger(initial_capital=D("1E17"), buy_notional=D("1E17"), sell_notional=D("1E17"),
                              fee_bps=D("0"), slippage_bps=D("0"))
    ts = START_AT
    for _ in range(25000):  # 买、卖、hold、hold
        for action in ("buy", "sell", "hold", "hold"):
            ledger.begin_bar()
            if action == "buy":
                assert ledger.buy(ts, D("1"))
            elif action == "sell":
                assert ledger.sell(ts, D("1"))
            ledger.check_invariants(D("1"), "loop")
            ts += DAY
    assert (ledger.buy_fills, ledger.sell_fills) == (25000, 25000)
    ledger.cum_bought += D("0.01")
    with pytest.raises(LedgerInvariantError, match="lot qty"):
        ledger.check_invariants(D("1"), "inject")
    ledger.cum_bought -= D("0.01")
    ledger.check_invariants(D("1"), "restored")
    ledger.cash += D("0.00001")
    with pytest.raises(LedgerInvariantError, match="cash\\+holdings"):
        ledger.check_invariants(D("1"), "inject")


def test_extreme_price_1e_minus_9():
    bars = _bars(["1E-9"] * 4, ["1E-9", "1E-9", "2E-9", "2E-9"])
    result = _run(bars, ["buy", "sell", "hold", "hold"], buy="100", sell="50")
    assert [(t["qty"], t["exit_price"]) for t in result.trades] == [
        (D("5E10"), D("1E-9")), (D("5E10"), D("2E-9")),
    ]
    assert result.final_cash == D("10000") + result.realized_pnl


def test_quantized_qty_and_cash_rules():
    assert floor_qty(D("100"), D("3")) == D("33.333333333333")
    assert floor_qty(D("0.00000005"), D("100000")) == 0  # 低于数量最小单位，不成交
    assert round_cash(D("0.000000005")) == D("0.00000001")  # 金额四舍五入（ROUND_HALF_UP）
    bars = _bars([100000] * 3, [100000] * 3)
    result = _run(bars, ["buy", "sell", "hold"], capital="1", buy="0.00000005", sell="0.00000001",
                  fee="0", slip="0")
    assert (result.buy_fills, result.skipped_buys_insufficient_cash, result.trades) == (0, 0, [])


def test_arithmetic_beyond_precision_raises_instead_of_rounding():
    """精确上下文把 Inexact 设为陷阱：超出 60 位精度的乘法不悄悄舍入，转成 LedgerInvariantError。"""
    bars = _bars(["1.2345678901234567E-5"] * 3, ["1.2345678901234567E-5"] * 3)
    with pytest.raises(LedgerInvariantError, match="inexact"):
        _run(bars, ["buy", "hold", "hold"], capital="1E40", buy="1E40", fee="0", slip="0")


# ---------------------------------------------------------------------------
# 四轮（Codex c511282 增量复核）：费用量化前不得被精度陷阱误杀；卖出费用不得超过成交额
# ---------------------------------------------------------------------------

def _half_up_cash(value: Fraction) -> D:
    """独立参照：Fraction 精确值按 1e-8 四舍五入（非负）。"""
    units = value * 10**8
    whole = units.numerator // units.denominator
    if (units - whole) * 2 >= 1:
        whole += 1
    return D(whole).scaleb(-8)


def test_fee_on_big_capital_low_price_fractional_bps_is_exact():
    price = D("1.2345678901234567E-9")
    bars = _bars([price] * 3, [price] * 3)
    result = _run(bars, ["buy", "hold", "hold"], capital="1E17", buy="1E16", fee="0.12345678", slip="0")
    assert result.buy_fills == 1
    qty = floor_qty(D("1E16"), price)
    gross = Fraction(qty) * Fraction(price)
    fee = _half_up_cash(gross * Fraction("0.12345678") / 10000)
    assert fee == D("123456780000.00000000")
    assert result.snapshots[0].cash == D("1E17")
    assert Fraction(result.snapshots[1].cash) == Fraction(D("1E17")) - gross - Fraction(fee)


def test_exit_costs_never_exceed_exit_gross():
    """各 4999 bps、期末价 1.01e-8：两项各量化成 1e-8 会超过成交额 1.01e-8；fee 先取、滑点取剩余。"""
    bars = _bars([1, 1, 1], [1, 1, D("1.01E-8")])
    result = _run(bars, ["buy", "hold", "hold"], capital="1.9998", buy="1", fee="4999", slip="4999")
    assert result.buy_fills == 1 and result.snapshots[1].cash == 0
    (trade,) = result.trades
    assert trade["exit_price"] == D("1.01E-8") and trade["qty"] == 1
    # 卖出侧：fee = R(1.01e-8 × 0.4999) = 1e-8；滑点 = min(1e-8, 1.01e-8 − 1e-8) = 1e-10
    assert trade["fee"] == D("0.4999") + D("1E-8")
    assert trade["slippage"] == D("0.4999") + D("1E-10")
    assert result.final_cash == 0


# ---------------------------------------------------------------------------
# R3-T1 账本泛化前的结果基线：端到端 payload 的 canonical 哈希逐字节钉住
# ---------------------------------------------------------------------------

HASH_PIN_MAIN = 320


def _hash_pin_frame() -> pd.DataFrame:
    rng = np.random.RandomState(20261009)
    n = WARM + HASH_PIN_MAIN
    closes = [float(c) for c in np.round(100 * np.cumprod(1 + rng.normal(0, 0.03, n)), 4)]
    opens = [round(c * (1 + rng.normal(0, 0.005)), 4) for c in [closes[0]] + closes[:-1]]
    first = START_AT - WARM * DAY
    idx = pd.DatetimeIndex([pd.to_datetime((first + i * DAY) * 1000, unit="ms") for i in range(n)])
    return pd.DataFrame(
        {
            "Open": opens,
            "High": [max(o, c) + 1 for o, c in zip(opens, closes)],
            "Low": [max(min(o, c) - 1, 0.0001) for o, c in zip(opens, closes)],
            "Close": closes,
            "Volume": [10.0] * n,
        },
        index=idx,
    )


def test_rsi_scale_in_out_canonical_hash_pinned_before_ledger_generalization(client, monkeypatch):
    from canonical_json import canonical_json_sha256

    full = _hash_pin_frame()
    end_at = START_AT + HASH_PIN_MAIN * DAY
    monkeypatch.setattr(provider, "_fetch_ohlcv", _range_fetch(full))
    params = {"rsi_period": 14, "oversold": 45, "overbought": 55, "buy_notional": 100, "sell_notional": 150}
    body = _post(client, params, extra={"end_at": end_at, "initial_capital": "1500"})
    assert body["result_status"] == "success", body

    a = body["assumptions"]
    trades = body["trades"]
    # 造数不得退化：多次买入、跨批次拆分的部分卖出、现金不足跳过、期末强平都要出现
    assert (a["buy_fills"], a["sell_fills"], a["skipped_buys_insufficient_cash"]) == (68, 43, 96)
    assert any(t["closed_at"] == end_at for t in trades)
    closed_by_ts: dict[int, set[int]] = {}
    for t in trades:
        closed_by_ts.setdefault(t["closed_at"], set()).add(t["opened_at"])
    assert any(len(v) > 1 for ts, v in closed_by_ts.items() if ts != end_at)

    pinned = {
        "trades": trades,
        "equity_curve": body["equity_curve"],
        "metrics": body["metrics"],
        "assumptions": {k: a[k] for k in (
            "position_mode", "buy_fills", "sell_fills", "skipped_buys_insufficient_cash",
            "indicator_warmup_bars",
        )},
    }
    assert canonical_json_sha256(pinned) == "b1af9dc607b4708d2df212d12190bc82191d29dc23d7cffdf95deb00e574631c"
