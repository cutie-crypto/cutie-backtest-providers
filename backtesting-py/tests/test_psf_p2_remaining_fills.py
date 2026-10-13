"""Verification of the remaining PSF-P2 candidates; offline registered builders."""
import pytest
import pandas as pd
from backtesting import Backtest
import cutie_backtesting_provider as p
import test_9t3_patterns as bottom
import test_9t4_divergence as div
import test_9t6_chan_3buy as chan
import test_f5_vwap_reversion as f5
import test_f6_streak_rsi as f6
import test_turtle_risk_3b as turtle

FAMILIES = ('bottom', 'divergence', 'chan', 'f5', 'f6', 'turtle')


def setup(family):
    if family == 'bottom':
        return bottom.frame('double_bottom'), 'double_bottom', {}, 54, 111, 99.9, 120
    if family == 'divergence':
        return div.hand_frame(), 'macd_bullish_divergence', div.hand_params('macd_bullish_divergence'), 24, 100, 77.922, 144.156
    if family == 'chan':
        return chan.frame(), 'chan_3buy', {}, 13, 125, 113.886, 147.228
    if family == 'f5':
        data = f5.frame()
        data.index = pd.date_range('2026-01-01', periods=len(data), freq='h')
        data.iloc[3, data.columns.get_loc('Low')] = 93
        return data, 'vwap_reversion', {'risk_layer_enabled': False}, 3, 94, 92.12, None
    if family == 'f6':
        return f6.frame(), 'red_streak_rsi', {'risk_layer_enabled': False}, 44, 93, 89.24, 94.76
    data, params = turtle.scenario()
    # Signal10 Wilder N=3.5, actual fill11=104 => group stop97.
    return data, 'turtle', {**params, 'take_profit_pct': 10}, 11, 104, 97, 114.4


@pytest.mark.parametrize('family', FAMILIES)
@pytest.mark.parametrize('scenario', ['stop', 'gap', 'both'])
def test_remaining_template_protective_fill(family, scenario):
    data, name, params, entry, opening, stop, target = setup(family)
    bar = entry + 1
    # Keep one designated entry and enough subsequent bars for a next-open mutant.
    data = data.iloc[:bar+3].copy()
    price = stop-5 if scenario == 'gap' else opening
    high = max(price+1, (target+1 if target is not None else opening*2)) if scenario == 'both' else price+1
    data.loc[data.index[bar], ['Open', 'High', 'Low', 'Close']] = [price, high, min(price-1, stop-1), price]
    cls = p.TOOL_SPECS['local.backtesting_py.'+name]['build'](params)['strategy']
    stats = Backtest(data, p._final_close_strategy(p._exit_kind_strategy(cls)), cash=10000,
                     exclusive_orders=name!='turtle', finalize_trades=True).run()
    trade = stats['_trades'].iloc[0]
    assert (trade.EntryBar, trade.ExitBar) == (entry, bar)
    assert trade.ExitPrice == pytest.approx(price if scenario=='gap' else stop)
    assert p._engine_trade_exit_kinds(stats['_trades'], stats['_strategy']._broker.closed_trades)[0] == 'stop_loss'
    if family in ('divergence', 'chan'):
        report = getattr(stats['_strategy'], family+'_report')
        assert report['exits'][0] == dict(reason='stop_loss', decision_bar=bar)
