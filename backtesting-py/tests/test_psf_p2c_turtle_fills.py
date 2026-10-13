"""PSF-P2c: group protective fills and the first post-exit reverse signal."""
from decimal import Decimal

import pytest
from backtesting import Backtest

import cutie_backtesting_provider as provider
from test_turtle_template import directional_fixture, fixture, frame


def test_group_stop_fills_all_units_on_touch_bar_and_updates_after_add():
    # Independent frozen-N arithmetic, with both long and short gap directions.
    for direction in ('long', 'short'):
        fx = fixture() if direction == 'long' else directional_fixture(direction)
        sign = 1 if direction == 'long' else -1
        n = Decimal(str(fx['expected']['groups'][0]['n']))
        final_add = Decimal(str(fx['opens'][21]))
        stop = final_add - sign * Decimal(str(fx['params']['stop_atr_multiplier'])) * n
        for gap in (False, True):
            data = frame(fx)
            if gap:
                op = float(stop - sign * 2)
                data.loc[data.index[22], 'Open'] = op
                data.loc[data.index[22], 'Low'] = min(data.Low.iloc[22], op)
                data.loc[data.index[22], 'High'] = max(data.High.iloc[22], op)
            built = provider._build_turtle(fx['params'])
            stats = Backtest(data, built['strategy'], cash=fx['cash'],
                             exclusive_orders=False, finalize_trades=False).run()
            first = stats['_trades'].query("Tag == 'turtle-1'").sort_values('EntryBar')
            assert first.EntryBar.tolist() == [16, 18, 19, 21]
            assert first.ExitBar.tolist() == [22] * 4
            wanted = float(stop - sign * 2) if gap else float(stop)
            assert first.ExitPrice.tolist() == [wanted] * 4
            assert stats['_strategy']._group_exit_reasons['turtle-1'] == 'stop'


def test_next_bar_after_group_protective_exit_accepts_reverse_signal():
    fx = directional_fixture('both')
    stats = Backtest(frame(fx), provider._build_turtle(fx['params'])['strategy'],
                     cash=fx['cash'], exclusive_orders=False, finalize_trades=False).run()
    trades = stats['_trades'].sort_values(['EntryBar', 'ExitBar'])
    long = trades.query("Tag == 'turtle-1'")
    assert long.ExitBar.tolist() == [22] * 4
    reverse = trades.query("Tag == 'turtle-2'")
    # The stopped bar cannot submit another group. Bar 23 can decide again,
    # so its reverse order fills at bar 24 open=100, rather than bar 51 open=91.
    assert reverse.EntryBar.tolist() == [24]
    assert reverse.EntryPrice.tolist() == [100]
    assert (reverse.Size < 0).all()
    assert 23 not in set(trades.EntryBar)
