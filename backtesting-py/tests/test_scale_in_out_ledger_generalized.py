"""R3-T1 分批账本泛化：指定金额买入、lifo、整批卖出、元组信号 / sell_all、on_fill、浮动盈亏统计。

直接测账本与 runner，不走 HTTP；费率统一 手续费 10bps、滑点 5bps，金额全部手算。
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scale_in_out_ledger import (  # noqa: E402
    LedgerBar,
    LedgerInvariantError,
    ScaleInOutLedger,
    run_scale_in_out,
)

D = Decimal
T0 = 1735689600
DAY = 86400


def _ledger(capital="1000", *, lot_order="fifo", buy="100", sell="100") -> ScaleInOutLedger:
    ledger = ScaleInOutLedger(initial_capital=D(capital), buy_notional=D(buy), sell_notional=D(sell),
                              fee_bps=D("10"), slippage_bps=D("5"), lot_order=lot_order)
    ledger.begin_bar()
    return ledger


def _bars(opens, closes) -> list[LedgerBar]:
    return [
        LedgerBar(open_time=T0 + i * DAY, close_time=T0 + (i + 1) * DAY, open=D(str(o)), close=D(str(c)))
        for i, (o, c) in enumerate(zip(opens, closes))
    ]


def _run(bars, signals, *, capital="10000", lot_order="fifo", on_fill=None):
    return run_scale_in_out(
        bars, lambda i: signals[i],
        initial_capital=D(capital), buy_notional=D("100"), sell_notional=D("100"),
        fee_bps=D("10"), slippage_bps=D("5"), start_at=bars[0].open_time, end_at=bars[-1].close_time,
        lot_order=lot_order, on_fill=on_fill,
    )


def _lots(ledger):
    return [(lot.opened_at, lot.entry_price, lot.qty, lot.fee_left, lot.slippage_left) for lot in ledger.lots]


# ---------------------------------------------------------------------------
# 指定金额买入
# ---------------------------------------------------------------------------

def test_buy_with_explicit_notional_after_default():
    ledger = _ledger()
    assert ledger.buy(T0, D("100"))  # 默认 100：1 个，费 0.1、滑点 0.05
    assert ledger.buy(T0 + DAY, D("80"), notional=D("150"))  # 1.5 倍：150/80 = 1.875 个，费 0.15、滑点 0.075
    assert _lots(ledger) == [
        (T0, D("100"), D("1"), D("0.1"), D("0.05")),
        (T0 + DAY, D("80"), D("1.875"), D("0.15"), D("0.075")),
    ]
    assert ledger.cash == D("1000") - D("100.15") - D("150.225") == D("749.625")
    assert ledger.total_invested == D("250")
    assert ledger.cum_bought == D("2.875")
    ledger.check_invariants(D("90"), "after")


def test_explicit_notional_exact_cash_fills_and_one_unit_short_skips():
    ledger = _ledger("100.15", buy="50")
    assert ledger.buy(T0, D("100"), notional=D("100"))
    assert (ledger.cash, ledger.skipped_buys, ledger.total_invested) == (D("0"), 0, D("100"))

    ledger = _ledger("100.14999999", buy="50")
    assert not ledger.buy(T0, D("100"), notional=D("100"))
    assert (ledger.cash, ledger.skipped_buys, ledger.lots) == (D("100.14999999"), 1, [])
    assert ledger.total_invested == 0


@pytest.mark.parametrize("bad", [D("0"), D("-1"), D("NaN"), D("Infinity"), 150.0, 150])
def test_explicit_notional_must_be_positive_finite_decimal(bad):
    with pytest.raises(ValueError, match="notional"):
        _ledger().buy(T0, D("100"), notional=bad)


def test_unknown_lot_order_is_rejected():
    with pytest.raises(ValueError, match="lot_order"):
        _ledger(lot_order="hifo")


# ---------------------------------------------------------------------------
# lifo / sell_lot / 不变量 ③
# ---------------------------------------------------------------------------

def _three_lots(lot_order):
    ledger = _ledger(lot_order=lot_order, sell="150")
    for i, price in enumerate(("100", "50", "40")):  # 数量 1、2、2.5
        ledger.begin_bar()
        assert ledger.buy(T0 + i * DAY, D(price))
    ledger.begin_bar()
    return ledger


def test_lifo_amount_sell_splits_newest_two_lots():
    ledger = _three_lots("lifo")
    assert ledger.sell(T0 + 3 * DAY, D("50"))  # 150/50 = 3 个：最新批 2.5 全卖 + 次新批 0.5
    assert [(t["opened_at"], t["qty"]) for t in ledger.trades] == [(T0 + 2 * DAY, D("2.5")), (T0 + DAY, D("0.5"))]
    assert [(at, qty) for at, _, qty, _, _ in _lots(ledger)] == [(T0, D("1")), (T0 + DAY, D("1.5"))]
    ledger.check_invariants(D("50"), "after")


def test_fifo_amount_sell_splits_oldest_lots_for_contrast():
    ledger = _three_lots("fifo")
    assert ledger.sell(T0 + 3 * DAY, D("50"))
    assert [(t["opened_at"], t["qty"]) for t in ledger.trades] == [(T0, D("1")), (T0 + DAY, D("2"))]


@pytest.mark.parametrize("lot_order,sold_at,qty", [("lifo", T0 + DAY, D("2")), ("fifo", T0, D("1"))])
def test_sell_lot_sells_whole_head_lot_at_qty_times_price(lot_order, sold_at, qty):
    ledger = _ledger(lot_order=lot_order)
    ledger.buy(T0, D("100"))
    ledger.begin_bar()
    ledger.buy(T0 + DAY, D("50"))
    ledger.begin_bar()
    cash_before = ledger.cash
    assert ledger.sell_lot(T0 + 2 * DAY, D("60"))
    (trade,) = ledger.trades
    assert (trade["opened_at"], trade["qty"], trade["exit_price"]) == (sold_at, qty, D("60"))
    gross = qty * D("60")  # 卖出成交额 = 批次数量 × 成交价，不是买入成交额 100
    assert ledger.cash == cash_before + gross - gross * D("0.001") - gross * D("0.0005")
    assert (ledger.sell_fills, ledger.sells_this_bar, len(ledger.lots)) == (1, 1, 1)
    ledger.check_invariants(D("60"), "after")


def test_sell_lot_without_holdings_returns_false():
    ledger = _ledger(lot_order="lifo")
    assert not ledger.sell_lot(T0, D("60"))
    assert (ledger.sell_fills, ledger.trades) == (0, [])


@pytest.mark.parametrize("lot_order,needle", [("fifo", "FIFO broken"), ("lifo", "LIFO broken")])
def test_lot_order_invariant_violation_is_raised(lot_order, needle):
    ledger = _three_lots(lot_order)
    ledger.lots.reverse()  # 打乱批次顺序：两次整批卖出的 opened_at 方向与模式相反
    with pytest.raises(LedgerInvariantError, match=needle):
        ledger.sell_lot(T0 + 3 * DAY, D("60"))
        ledger.sell_lot(T0 + 3 * DAY, D("60"))


def test_lifo_buy_between_sells_restarts_order_check():
    ledger = _ledger(lot_order="lifo")
    ledger.buy(T0, D("100"))
    ledger.begin_bar()
    ledger.buy(T0 + DAY, D("100"))
    ledger.begin_bar()
    assert ledger.sell_lot(T0 + 2 * DAY, D("100"))
    ledger.begin_bar()
    ledger.buy(T0 + 3 * DAY, D("100"))  # 新批次比刚卖掉的更新，属合法 lifo
    ledger.begin_bar()
    assert ledger.sell_lot(T0 + 4 * DAY, D("100"))
    assert [t["opened_at"] for t in ledger.trades] == [T0 + DAY, T0 + 3 * DAY]


# ---------------------------------------------------------------------------
# runner：元组信号、sell_all、on_fill
# ---------------------------------------------------------------------------

def test_tuple_signals_and_on_fill_reports():
    calls = []
    bars = _bars([100, 100, 120, 130], [100, 110, 125, 140])
    result = _run(bars, [("buy", D("150")), ("sell_lot",), "hold", "hold"],
                  on_fill=lambda *args: calls.append(args))
    assert calls == [(1, "buy", True, D("100")), (2, "sell_lot", True, D("120"))]
    (trade,) = result.trades
    assert (trade["qty"], trade["entry_price"], trade["exit_price"]) == (D("1.5"), D("100"), D("120"))
    assert (result.buy_fills, result.sell_fills, result.total_invested) == (1, 1, D("150"))


def test_on_fill_reports_unfilled_buy_when_cash_short():
    calls = []
    bars = _bars([100, 100, 100], [100, 100, 100])
    result = _run(bars, [("buy", D("150")), "hold", "hold"], capital="100",
                  on_fill=lambda *args: calls.append(args))
    assert calls == [(1, "buy", False, D("100"))]
    assert (result.buy_fills, result.skipped_buys_insufficient_cash, result.total_invested) == (0, 1, D("0"))


@pytest.mark.parametrize("raw", [("buy", 150.0), ("buy", D("0")), ("buy",), ("sell",), ["buy"], None, "sell_lot"])
def test_unknown_signal_shapes_are_rejected(raw):
    bars = _bars([100, 100, 100], [100, 100, 100])
    with pytest.raises(LedgerInvariantError, match="unknown signal"):
        _run(bars, [raw, "hold", "hold"])


@pytest.mark.parametrize("lot_order,opened", [("fifo", [1, 2]), ("lifo", [2, 1])])
@pytest.mark.parametrize("sell_all", ["sell_all", ("sell_all",)])
def test_sell_all_signal_clears_every_lot_at_next_open(lot_order, opened, sell_all):
    calls = []
    bars = _bars([100, 100, 50, 60, 70], [100, 100, 50, 60, 70])
    result = _run(bars, ["buy", "buy", sell_all, "hold", "hold"], lot_order=lot_order,
                  on_fill=lambda *args: calls.append(args))
    assert calls[-1] == (3, "sell_all", True, D("60"))
    # 每批一条 trade，全部在第 3 根开盘以 60 成交；没有留到期末强平的批次
    assert [(t["opened_at"], t["qty"], t["closed_at"], t["exit_price"]) for t in result.trades] == [
        (bars[i].open_time, D("1") if i == 1 else D("2"), bars[3].open_time, D("60")) for i in opened
    ]
    assert result.sell_fills == 1 and result.snapshots[3].sells_this_bar == 1
    assert result.snapshots[3].lot_qty_sum == 0


def test_sell_all_without_holdings_is_unfilled():
    calls = []
    bars = _bars([100, 100, 100], [100, 100, 100])
    result = _run(bars, ["sell_all", "hold", "hold"], on_fill=lambda *args: calls.append(args))
    assert calls == [(1, "sell_all", False, D("100"))]
    assert (result.trades, result.sell_fills) == ([], 0)


# ---------------------------------------------------------------------------
# 浮动盈亏 / 最大浮亏 / 累计买入成交额
# ---------------------------------------------------------------------------

def test_unrealized_pnl_hand_computed_and_max_unrealized_loss():
    bars = _bars([100, 100, 50, 70], [100, 110, 60, 70])
    result = _run(bars, ["buy", "buy", "hold", "hold"])
    # 第 1 根：批 1（100 买 1 个，未分摊费 0.15）收盘 110 → 10 − 0.15
    # 第 2 根：批 2（50 买 2 个，未分摊费 0.15）；收盘 60 → (60−100)·1 − 0.15 + (60−50)·2 − 0.15
    # 第 3 根：收盘 70 → (70−100)·1 − 0.15 + (70−50)·2 − 0.15
    assert [s.unrealized_pnl for s in result.snapshots] == [D("0"), D("9.85"), D("-20.3"), D("9.7")]
    assert result.max_unrealized_loss == D("-20.3")
    assert result.total_invested == D("200")


def test_max_unrealized_loss_is_zero_when_always_in_profit():
    bars = _bars([100, 100, 100, 100], [100, 110, 120, 130])
    result = _run(bars, ["buy", "hold", "hold", "hold"])
    assert min(s.unrealized_pnl for s in result.snapshots) == 0
    assert result.max_unrealized_loss == 0


def test_total_invested_excludes_skipped_buys():
    bars = _bars([100] * 5, [100] * 5)
    result = _run(bars, ["buy", "buy", "buy", "buy", "hold"], capital="250")
    assert (result.buy_fills, result.skipped_buys_insufficient_cash) == (2, 2)
    assert result.total_invested == D("200")
