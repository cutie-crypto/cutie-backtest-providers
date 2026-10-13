"""PSF-P2c: group protective fills and the first post-exit reverse signal."""
import pandas as pd
from decimal import Decimal

import pytest
from backtesting import Backtest

import cutie_backtesting_provider as provider
from test_turtle_template import directional_fixture, fixture, frame, hand, directional_hand
from test_turtle_risk_3b import scenario, execute


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

    # A queued add must be canceled if OPEN has already crossed old protection.
    for direction in ('long', 'short'):
        fx = fixture() if direction == 'long' else directional_fixture(direction)
        sign = 1 if direction == 'long' else -1
        n = Decimal(str(fx['expected']['groups'][0]['n']))
        old_stop = Decimal(str(fx['opens'][16])) - sign * Decimal(str(fx['params']['stop_atr_multiplier'])) * n
        opening = float(old_stop - sign * 2)
        fx['opens'][18] = opening
        fx['lows'][18] = min(fx['lows'][18], opening)
        fx['highs'][18] = max(fx['highs'][18], opening)
        scalar = hand(fx) if direction == 'long' else directional_hand(fx)
        scalar_first = [t for t in scalar['trades'] if t['group_id'] == 'turtle-1']
        stats = Backtest(frame(fx), provider._build_turtle(fx['params'])['strategy'],
                         cash=fx['cash'], exclusive_orders=False, finalize_trades=False).run()
        first = stats['_trades'].query("Tag == 'turtle-1'")
        assert first.EntryBar.tolist() == [16] == [t['entry_bar'] for t in scalar_first]
        assert first.ExitBar.tolist() == [18] == [t['exit_bar'] for t in scalar_first]
        assert first.ExitPrice.tolist() == [opening] == [t['exit_price'] for t in scalar_first]
        # The same opening precedence applies to an existing single TP.
        data, params = scenario(direction, add=True)
        entry = Decimal(104 if sign == 1 else 96)
        target = entry * (Decimal(1) + sign * Decimal('.05'))
        opening = float(target + sign * 2)
        data.loc[data.index[12], 'Open'] = opening
        data.loc[data.index[12], 'Low'] = min(data.Low.iloc[12], opening)
        data.loc[data.index[12], 'High'] = max(data.High.iloc[12], opening)
        stats = execute(data, {**params, 'take_profit_pct': 5})
        first = stats['_trades'].query("Tag == 'turtle-1'")
        assert first.EntryBar.tolist() == [11]
        assert first.ExitBar.tolist() == [12]
        assert first.ExitPrice.tolist() == [opening]
        assert stats['_strategy']._group_exit_reasons['turtle-1'] == 'take_profit'

    # Large Wilder N makes the long stop nonpositive: no executable SL exists.
    rows = [[100, 101, 99, 100]] * 9 + [[100, 101, 1, 100], [100, 104, 99, 103]] + [[104, 105, 103, 104]] * 4
    data = pd.DataFrame(rows, columns=['Open', 'High', 'Low', 'Close'],
                        index=pd.date_range('2026-01-01', periods=len(rows), freq='h')).assign(Volume=1)
    params = dict(entry_period=3, exit_period=2, atr_period=2, max_units=1,
                  unit_risk_pct=1, stop_atr_multiplier=5, direction='long')
    stats = Backtest(data, provider._build_turtle(params)['strategy'],
                     cash=100000, exclusive_orders=False, finalize_trades=False).run()
    assert stats['_strategy']._group_stop == 104 - 5 * 28 < 0
    assert len(stats['_strategy'].trades) == 1
    assert stats['_strategy'].trades[0].sl is None

    # Independently calculate the same-bar stop in the changed risk HTTP golden.
    fx = fixture()
    n = None
    previous = None
    for hi, lo, close in zip(fx['highs'][:22], fx['lows'][:22], fx['closes'][:22]):
        tr = hi - lo if previous is None else max(hi - lo, abs(hi - previous), abs(lo - previous))
        n = tr if n is None else (n + tr) / 2  # fixture atr_period=2
        previous = close
    stop = fx['opens'][22] - 2 * n
    assert stop == 147.421875
    stats = Backtest(frame(fx), provider._build_turtle({**fx['params'], 'risk_layer_enabled': True,
                     'max_holding_bars': 3, 'take_profit_pct': 5})['strategy'],
                     cash=fx['cash'], exclusive_orders=False, finalize_trades=False).run()
    third = stats['_trades'].query("Tag == 'turtle-3'")
    assert third.EntryBar.tolist() == third.ExitBar.tolist() == [22]
    assert third.ExitPrice.tolist() == [stop]


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
