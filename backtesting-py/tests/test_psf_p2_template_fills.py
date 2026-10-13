"""PSF-P2: synthetic OHLC through registered templates and the real broker."""
import pytest
from backtesting import Backtest
import cutie_backtesting_provider as p
import test_9t1_engulf_pin as pattern
import test_9t5_fibonacci as fib


def execute(family, trigger, *, entry_touch=False):
    if family == 'pattern':
        data = pattern.frame('bullish_engulfing', size=34)
        name, params, entry_bar, stop, target = 'bullish_engulfing', {'position_filter': False}, 26, 96.903, 124.194
        data.iloc[27:, :4] = [106, 107, 105, 106]
    elif family == 'pattern_short':
        data = pattern.frame('bullish_engulfing', size=34)
        for column in ('Open', 'Close'):
            data[column] = 200 - data[column]
        high, low = data.High.copy(), data.Low.copy()
        data.High, data.Low = 200-low, 200-high
        name, params, entry_bar, stop, target = 'bearish_engulfing', {'position_filter': False}, 26, 103.103, 75.794
        data.iloc[27:, :4] = [94, 95, 93, 94]
    else:
        data = fib.frame()
        name, params, entry_bar, stop, target = 'fibonacci_retracement', {'swing_n': 2}, 9, 104.17572, 120
    bar = entry_bar if entry_touch else entry_bar + 1
    data.iloc[bar, :4] = trigger
    cls = p.TOOL_SPECS['local.backtesting_py.'+name]['build'](params)['strategy']
    stats = Backtest(data, p._final_close_strategy(p._exit_kind_strategy(cls)), cash=10000,
                     exclusive_orders=True, finalize_trades=True).run()
    return stats, data, entry_bar, bar, stop, target


@pytest.mark.parametrize('family', ['pattern', 'pattern_short', 'fibonacci'])
@pytest.mark.parametrize('scenario', ['stop', 'gap', 'both', 'take', 'take_gap', 'entry_stop'])
def test_template_protective_fill(family, scenario):
    short = family == 'pattern_short'
    stop = 103.103 if short else 96.903 if family == 'pattern' else 104.17572
    target = 75.794 if short else 124.194 if family == 'pattern' else 120
    opening = 94 if short else 106 if family == 'pattern' else 109
    if scenario == 'gap':
        opening = stop+5 if short else stop-5
    elif scenario == 'take_gap':
        opening = target-5 if short else target+5
    high, low = opening+1, opening-1
    if scenario in ('stop', 'both', 'entry_stop'):
        high = max(high, stop+1) if short else high
        low = low if short else min(low, stop-1)
    if scenario in ('take', 'both'):
        low = min(low, target-1) if short else low
        high = high if short else max(high, target+1)
    stats, data, entry, bar, _, _ = execute(family, [opening, high, low, opening], entry_touch=scenario=='entry_stop')
    [trade] = stats['_trades'].itertuples()
    expected = opening if scenario in ('gap', 'take_gap') else target if scenario == 'take' else stop
    assert trade.EntryBar == entry
    assert (trade.ExitBar, trade.ExitPrice) == (bar, pytest.approx(expected))
    assert trade.ExitTime == data.index[bar]
    expected_kind = 'take_profit' if scenario in ('take', 'take_gap') else 'stop_loss'
    assert p._engine_trade_exit_kinds(stats['_trades'], stats['_strategy']._broker.closed_trades) == [expected_kind]
    assert not any(order.parent_trade is None for order in stats['_strategy'].orders)
    if family == 'fibonacci':
        assert stats['_strategy']._fib_exits == [dict(decision_index=bar, reason=expected_kind)]
