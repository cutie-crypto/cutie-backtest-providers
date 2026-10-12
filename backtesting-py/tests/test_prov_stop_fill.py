"""PSF-P1: protective fills through the real provider mixin and broker, no network."""
import sys
from pathlib import Path
import pandas as pd
import pytest
from backtesting import Backtest, Strategy
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p


def run(rows, *, side='long', params=None, reenter=False):
    frame = pd.DataFrame(rows, columns=['Open', 'High', 'Low', 'Close'],
                         index=pd.date_range('2026-01-01', periods=len(rows), freq='h')).assign(Volume=1)
    class Manual(p._FixedRiskMixin, Strategy):
        _risk = p._parse_fixed_risk_params(dict(risk_layer_enabled=True, stop_loss_pct=5, **(params or {})))
        def init(self):
            self._risk_init()
        def next(self):
            if self.position and self._risk_check_exit():
                return
            if not self.position and (len(self.data) == 2 or reenter):
                self._risk_buy() if side == 'long' else self._risk_sell()
    stats = Backtest(frame, p._final_close_strategy(p._exit_kind_strategy(Manual)), cash=10000,
                     exclusive_orders=True, finalize_trades=True).run()
    return stats, frame


@pytest.mark.parametrize('side,trigger,price', [('long', [100, 101, 94, 99], 95), ('short', [100, 106, 99, 101], 105)])
def test_stop_fills_at_stop_price_on_trigger_bar(side, trigger, price):
    stats, _ = run([[100, 101, 99, 100]] * 3 + [trigger] + [[103, 104, 102, 103]] * 2, side=side)
    [trade] = stats['_trades'].itertuples()
    assert (trade.EntryBar, trade.ExitBar, trade.ExitPrice) == (2, 3, price)
    assert p._engine_trade_exit_kinds(stats['_trades'], stats['_strategy']._broker.closed_trades) == ['stop_loss']


@pytest.mark.parametrize('side,gap,price', [('long', [90, 93, 89, 92], 90), ('short', [110, 111, 107, 108], 110)])
def test_stop_gap_through_fills_at_open(side, gap, price):
    stats, _ = run([[100, 101, 99, 100]] * 3 + [gap] + [[100, 101, 99, 100]] * 2, side=side)
    [trade] = stats['_trades'].itertuples()
    assert (trade.ExitBar, trade.ExitPrice) == (3, price)


@pytest.mark.parametrize('side,price', [('long', 95), ('short', 105)])
def test_same_bar_stop_and_take_stop_wins(side, price):
    stats, _ = run([[100, 101, 99, 100]] * 3 + [[100, 111, 89, 100]] + [[102, 103, 101, 102]] * 2,
                   side=side, params=dict(take_profit_pct=10))
    [trade] = stats['_trades'].itertuples()
    assert (trade.ExitBar, trade.ExitPrice) == (3, price)
    assert p._engine_trade_exit_kinds(stats['_trades'], stats['_strategy']._broker.closed_trades) == ['stop_loss']


def test_trailing_stop_effective_next_bar():
    rows = [[100, 101, 99, 100]] * 3 + [[100, 110, 99, 109], [109, 109, 104, 108], [103, 104, 102, 103]]
    stats, _ = run(rows, params=dict(trailing_stop_pct=5))
    [trade] = stats['_trades'].itertuples()
    assert (trade.ExitBar, trade.ExitPrice) == (4, 104.5)
    assert p._engine_trade_exit_kinds(stats['_trades'], stats['_strategy']._broker.closed_trades) == ['trailing_stop']


def test_stop_exit_closed_at_in_trigger_bucket():
    stats, frame = run([[100, 101, 99, 100]] * 3 + [[100, 101, 94, 99]] + [[103, 104, 102, 103]] * 2)
    [trade] = stats['_trades'].itertuples()
    assert trade.ExitTime == frame.index[3]
    [result] = p._build_result_v2_trades(stats['_trades'], 1, 0, 0)
    assert result['closed_at'] == int(frame.index[3].timestamp())


def test_no_reentry_same_bar():
    stats, _ = run([[100, 101, 99, 100]] * 3 + [[100, 101, 94, 99]] + [[100, 101, 99, 100]] * 4, reenter=True)
    assert list(stats['_trades'].EntryBar) == [2, 5]


@pytest.mark.parametrize('side,gap,price', [('long', [115, 116, 114, 115], 115), ('short', [85, 86, 84, 85], 85)])
def test_take_gap_through_fills_at_open(side, gap, price):
    stats, _ = run([[100, 101, 99, 100]] * 3 + [gap] + [[100, 101, 99, 100]] * 2, side=side, params=dict(take_profit_pct=10))
    [trade] = stats['_trades'].itertuples()
    assert (trade.ExitBar, trade.ExitPrice) == (3, price)
    assert p._engine_trade_exit_kinds(stats['_trades'], stats['_strategy']._broker.closed_trades) == ['take_profit']


def test_partial_take_keeps_next_open_fill():
    stats, _ = run([[100, 101, 99, 100]] * 3 + [[100, 106, 99, 100], [103, 104, 102, 103]]
                   + [[100, 101, 99, 100]] * 2,
                   params=dict(tp1_r=1, tp1_close_pct=50, tp2_r=2, tp2_close_pct=50))
    trade = stats['_trades'].iloc[0]
    assert (trade.ExitBar, trade.ExitPrice) == (4, 103)
