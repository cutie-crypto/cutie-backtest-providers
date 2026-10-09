"""Independent OHLC fixtures through the registered tools, engine and HTTP route."""
from pathlib import Path
import hashlib
import sys

import pandas as pd
import pytest
from backtesting import Backtest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as provider
from canonical_json import canonical_json
from strategy_candle_patterns import candle_geometry, candle_patterns
from test_9t1_engulf_pin import run, http, request
from test_time_layer import DEFAULTS

NAMES = ('morning_star', 'three_white_soldiers', 'bullish_doji_reversal', 'inside_bar_breakout')


def frame(name, *, signal=25, size=70):
    rows = [[120., 122., 119., 121., 1.] for _ in range(size)]
    if name == 'morning_star':
        rows = [[100., 102., 99., 101., 1.] for _ in range(size)]
        # Prior mean body at middle=1.45. Middle body=1 > .725, <= 10*.3=3.
        rows[signal-2][:4] = [110, 111, 99, 100]
        rows[signal-1][:4] = [99, 100, 97, 98]
        rows[signal][:4] = [98, 108, 97, 106]  # > first midpoint 105
    elif name == 'three_white_soldiers':
        rows[signal-3][:4] = [100, 101, 97.5, 98]
        rows[signal-2][:4] = [98, 101.5, 97, 101]
        rows[signal-1][:4] = [100, 104.5, 98, 104]
        rows[signal][:4] = [103, 107.5, 102, 107]
    elif name == 'bullish_doji_reversal':
        # Pure declines into doji: its small positive body does not change RSI=0.
        rows = [[141+signal-i, 142+signal-i, 139+signal-i, 140+signal-i, 1.] for i in range(size)]
        rows[signal-1][:4] = [100, 104, 97, 100.5]  # .5 <= 7*.1
        rows[signal][:4] = [101, 109, 99, 108]  # strict next-close confirmation
    elif name == 'inside_bar_breakout':
        rows[signal-2][:4] = [100, 108, 97, 104]
        rows[signal-1][:4] = [102, 107, 98, 103]
        rows[signal][:4] = [103, 110, 97, 109]
    else:
        raise AssertionError(name)
    for i in range(signal+1, size):
        rows[i][:4] = [110, 111, 109, 110]
    return pd.DataFrame(rows, columns=['Open', 'High', 'Low', 'Close', 'Volume'],
                        index=pd.date_range('2026-01-01', periods=size, freq='h'), dtype=float)


def compatibility_frame(name, *, signal=29, size=120):
    data = frame(name, signal=signal, size=size)
    data.iloc[signal+4, :4] = [110, 140, 105, 110]
    return data


@pytest.mark.parametrize('name', NAMES)
def test_hand_shape_next_open_and_specific_anchor(name):
    result = run(name, frame(name))
    trade = result['_strategy'].trades[0]
    assert len(result['_strategy'].trades) == 1
    assert (trade.entry_bar, trade.entry_price) == (26, 110)
    # All independent fixtures have designated stop anchor=97; other bars differ.
    assert trade.tag.stop == pytest.approx(96.903)


def test_morning_middle_between_generic_small_and_first_relative_threshold():
    data = frame('morning_star')
    g = candle_geometry(*(data[c].to_numpy() for c in ('Open', 'High', 'Low', 'Close')))
    assert g.mean_body[24] == pytest.approx(1.45)
    assert .5 * 1.45 < g.body[24] == 1 <= .3 * 10
    patterns = candle_patterns('star', g)
    assert not patterns.small_body[24] and patterns.bullish[25]
    assert run('morning_star', data)['_strategy'].trades[0].entry_bar == 26


@pytest.mark.parametrize('bad', ['large', 'middle', 'middle_close', 'third', 'midpoint'])
def test_morning_requires_each_clause(bad):
    data = frame('morning_star')
    if bad == 'large':
        data.iloc[23, :4] = [101, 102, 99, 100]
    elif bad == 'middle':
        data.iloc[24, :4] = [99, 100, 95, 95.9]  # 3.1 > 3
    elif bad == 'middle_close':
        data.iloc[24, :4] = [99, 101, 97, 100]
    elif bad == 'third':
        data.iloc[25, :4] = [107, 108, 97, 106]
    else:
        data.iloc[25, :4] = [98, 108, 97, 105]
    assert not run('morning_star', data)['_strategy'].position


@pytest.mark.parametrize('bar', [22, 23, 24, 25])
def test_soldier_open_must_be_inside_previous_body(bar):
    data = frame('three_white_soldiers')
    if bar == 22:
        data.iloc[bar, :4] = [95, 97, 94, 96]  # first open=98 outside
    else:
        data.iloc[bar, 0] = [96, 102, 105][bar-23]
        data.iloc[bar, 2] = min(data.iloc[bar, 2], data.iloc[bar, 0]-1)
    assert not run('three_white_soldiers', data)['_strategy'].position


@pytest.mark.parametrize('bad', ['color', 'close', 'wick'])
def test_soldier_color_close_and_wick(bad):
    data = frame('three_white_soldiers')
    data.iloc[25, :4] = {'color': [103, 104, 101, 102], 'close': [100, 105, 99, 104],
                        'wick': [103, 108.21, 102, 107]}[bad]
    assert not run('three_white_soldiers', data)['_strategy'].position


@pytest.mark.parametrize('enabled', [False, True])
def test_soldier_decline_filter_switch(enabled):
    data = frame('three_white_soldiers')
    data.iloc[3, :4] = [96, 98, 95, 97]  # first open98 >= close20barsago97
    assert bool(run('three_white_soldiers', data, {'position_filter': enabled})['_strategy'].position) is (not enabled)


@pytest.mark.parametrize('close,valid', [(104, False), (104.001, True), (103.999, False)])
def test_doji_only_next_strict_close_confirms(close, valid):
    data = frame('bullish_doji_reversal')
    data.iloc[25, :4] = [101, 109, 99, close]
    result = run('bullish_doji_reversal', data)
    assert bool(result['_strategy'].position) is valid
    assert not run('bullish_doji_reversal', data.iloc[:25], finalize=True)['_strategy'].position
    if valid:
        assert result['_strategy'].trades[0].entry_bar == 26


def test_doji_failed_next_confirmation_never_confirms_later():
    data = frame('bullish_doji_reversal')
    data.iloc[25, :4] = [101, 104, 99, 103]
    assert not run('bullish_doji_reversal', data)['_strategy'].position


@pytest.mark.parametrize('enabled', [False, True])
def test_doji_rsi_at_candidate_not_confirmation(enabled):
    data = frame('bullish_doji_reversal')
    for i in range(24):
        data.iloc[i, :4] = [70+i, 72+i, 69+i, 71+i]  # candidate RSI=100
    assert bool(run('bullish_doji_reversal', data, {'position_filter': enabled})['_strategy'].position) is (not enabled)


@pytest.mark.parametrize('row', [[100, 104, 97, 100.701], [100, 100.5, 99.5, 100]])
def test_doji_body_and_minimum_span(row):
    data = frame('bullish_doji_reversal')
    data.iloc[24, :4] = row
    assert not run('bullish_doji_reversal', data)['_strategy'].position


def inside_frame(delay):
    data = frame('inside_bar_breakout')
    for i in range(25, 25+delay):
        # Equal mother extremes, deliberately not another strict inside bar.
        data.iloc[i, :4] = [103, 108, 97, 104]
    data.iloc[24+delay, :4] = [103, 110, 97, 109]
    data.iloc[25+delay:, :4] = [110, 111, 109, 110]
    return data


@pytest.mark.parametrize('window,delay,valid', [(3,1,True),(3,3,True),(3,4,False),
    (1,1,True),(1,2,False),(4,4,True),(20,20,True),(20,21,False)])
def test_inside_breakout_deadline(window, delay, valid):
    result = run('inside_bar_breakout', inside_frame(delay), {'breakout_window': window})
    assert bool(result['_strategy'].position) is valid
    if valid:
        trade = result['_strategy'].trades[0]
        assert trade.entry_bar == 25+delay and trade.entry_price == 110
        assert trade.tag.stop == pytest.approx(96.903)


@pytest.mark.parametrize('change', ['high_equal','low_equal','close_equal','wick_only'])
def test_inside_strict_containment_and_close_breakout(change):
    data = frame('inside_bar_breakout')
    if change == 'high_equal':
        data.iloc[24, 1] = 108
    elif change == 'low_equal':
        data.iloc[24, 2] = 97
    else:
        data.iloc[25:29, :4] = [103, 110, 97, 108 if change == 'close_equal' else 107]
    assert not run('inside_bar_breakout', data)['_strategy'].position


@pytest.mark.parametrize('enabled', [False, True])
def test_inside_optional_ema20_trend_filter(enabled):
    # Prior closes~121 keep EMA20 above breakout109; false is default.
    assert bool(run('inside_bar_breakout', frame('inside_bar_breakout'), {'trend_filter': enabled})['_strategy'].position) is (not enabled)


def test_latest_inside_setup_replaces_previous_mother():
    data = frame('inside_bar_breakout')
    data.iloc[25, :4] = [102, 106, 99, 105]  # inside prior inside, new mother=(107,98)
    data.iloc[26, :4] = [105, 108, 98, 107.5]  # breaks107, not old108
    result = run('inside_bar_breakout', data)
    trade = result['_strategy'].trades[0]
    assert trade.entry_bar == 27 and trade.tag.stop == pytest.approx(97.902)


@pytest.mark.parametrize('name', NAMES)
@pytest.mark.parametrize('r,hit', [(2,True),(2,False),(3,True),(3,False)])
def test_r_target_hand_computed_from_fill(name, r, hit):
    # Entry110-stop96.903=13.097; 2R=136.194, 3R=149.291.
    target = 136.194 if r == 2 else 149.291
    data = frame(name)
    data.iloc[27, :4] = [110, target+(.001 if hit else -.001), 105, 110]
    data.iloc[28, :4] = [112, 113, 111, 112]
    result = run(name, data, {'reward_r': r})
    assert len(result['_trades']) == int(hit)
    if hit:
        trade = result['_trades'].iloc[0]
        assert (trade.ExitBar, trade.ExitPrice) == (28, 112)
    else:
        assert result['_strategy'].position


@pytest.mark.parametrize('name', NAMES)
@pytest.mark.parametrize('low,hit', [(96.902,True),(96.904,False)])
def test_buffered_frozen_stop(name, low, hit):
    data = frame(name)
    data.iloc[27, :4] = [110, 111, low, 110]
    data.iloc[28, :4] = [101, 102, 100, 101]
    result = run(name, data)
    assert len(result['_trades']) == int(hit)
    if hit:
        assert (result['_trades'].iloc[0].ExitBar, result['_trades'].iloc[0].ExitPrice) == (28,101)


@pytest.mark.parametrize('name', NAMES)
@pytest.mark.parametrize('opening,skip', [(96.8,True),(96.903,True),(96.904,False)])
def test_entry_gap_guard_and_skip_reason(name, opening, skip):
    data = frame(name)
    data.iloc[26, :4] = [opening, 110, opening, 106]
    result = run(name, data)
    report = result['_strategy'].pattern_report
    assert report['skipped_entry_count'] == int(skip)
    assert len(result['_trades']) == int(not skip)
    if skip:
        assert report['skipped_entries'] == [dict(reason='entry_open_at_or_below_frozen_stop',
            signal_bar=25, entry_bar=26, entry_open=opening, frozen_stop=pytest.approx(96.903))]


@pytest.mark.parametrize('name', NAMES)
def test_prefix_and_warmup_are_causal(name):
    data = frame(name)
    prefix = run(name, data.iloc[:27])['_strategy'].trades[0]
    data.iloc[35:, :4] = [1, 2, .5, 1]
    full = run(name, data)['_trades'].iloc[0]
    assert (prefix.entry_bar,prefix.entry_price,prefix.tag.stop) == (full.EntryBar,full.EntryPrice,full.Tag.stop)
    warm = run(name, frame(name), warm=24)['_strategy'].trades[0]
    assert (warm.entry_bar,warm.entry_price,warm.tag.stop) == (2,110,pytest.approx(96.903))


@pytest.mark.parametrize('name', NAMES)
def test_tail_no_entry_without_next_open(name):
    result = run(name, frame(name).iloc[:26], finalize=True)
    assert result['_trades'].empty and not result['_strategy'].position


@pytest.mark.parametrize('name', NAMES)
@pytest.mark.parametrize('params', [dict(direction='short'),dict(direction='both'),dict(stop_loss_pct=5),
    dict(take_profit_pct=5),dict(risk_layer_enabled=True,take_profit_r=2),dict(reward_r=0),
    dict(reward_r=21),dict(reward_r=True),dict(reward_r='2'),dict(time_session_start='06:00'),
    dict(atr_stop_multiplier=1)])
@pytest.mark.parametrize('market', ['spot','futures'])
def test_invalid_params_before_fetch(monkeypatch, name, params, market):
    monkeypatch.setattr(provider, 'AUTH_TOKEN', '')
    def forbidden(*a):
        pytest.fail('invalid params fetched market data')
    monkeypatch.setattr(provider, '_fetch_ohlcv', forbidden)
    monkeypatch.setattr(provider, '_fetch_template_warmup', forbidden)
    body = request(name,params,frame(name))
    body['backtest']['market'] = market
    result = TestClient(provider.app).post('/cutie/backtest',json=body).json()
    assert result['error_type'] == 'INVALID_PARAMS', result


@pytest.mark.parametrize('params', [dict(breakout_window=0),dict(breakout_window=21),
    dict(breakout_window=True),dict(breakout_window=3.0),dict(breakout_window='3'),dict(trend_filter=1)])
def test_inside_invalid_params(params):
    with pytest.raises(ValueError,match='INVALID_PARAMS:'):
        provider.TOOL_SPECS['local.backtesting_py.inside_bar_breakout']['build'](params)


@pytest.mark.parametrize('name', NAMES)
def test_stop_then_expiry_then_target(name):
    data = frame(name)
    data.iloc[27, :4] = [110,150,96,110]
    stop = run(name,data,dict(risk_layer_enabled=True,max_holding_bars=2))
    assert stop['_trades'].iloc[0].ExitBar == 28
    assert stop['_strategy']._risk_exit_reason == 'stop_loss'
    data.iloc[27, :4] = [110,150,105,110]
    expiry = run(name,data,dict(risk_layer_enabled=True,max_holding_bars=2))
    assert expiry['_trades'].iloc[0].ExitBar == 28
    assert expiry['_strategy']._risk_exit_reason == 'time_expiry'


@pytest.mark.parametrize('name', NAMES)
def test_time_gate_entry_and_unrestricted_exit(name):
    data = compatibility_frame(name)
    allowed = run(name,data,dict(time_layer_enabled=True,time_session_start='06:00',time_session_end='10:00'))
    denied = run(name,data,dict(time_layer_enabled=True,time_session_start='07:00',time_session_end='10:00'))
    assert (allowed['_trades'].iloc[0].EntryBar,allowed['_trades'].iloc[0].ExitBar) == (30,34)
    assert denied['_trades'].empty and not denied['_strategy'].position


@pytest.mark.parametrize('name', NAMES)
def test_http_skip_assumptions_and_frozen_v2_keys(monkeypatch,tmp_path,name):
    data = frame(name)
    data.iloc[26, :4] = [96.8,110,96.8,106]
    skipped = http(monkeypatch,tmp_path,name,data)
    assert not skipped['trades'] and skipped['raw_report']['candle_pattern']['skipped_entry_count'] == 1
    assert 'next bar open' in skipped['assumptions']['pattern_execution']
    body = http(monkeypatch,tmp_path,name,compatibility_frame(name))
    assert len(body['trades']) == 1
    from test_time_layer_compatibility import capture
    old = capture.response('bullish_engulfing',{})
    for key in ('metrics','data_manifest'):
        assert set(body[key]) == set(old[key])
    assert set(body['trades'][0]) == set(old['trades'][0])


@pytest.mark.parametrize('name', NAMES)
@pytest.mark.parametrize('warm', [False,True])
@pytest.mark.parametrize('disabled', [False,True])
def test_frozen_off_http_bytes(name,warm,disabled):
    import test_time_layer_compatibility as compat
    expected = compat.PATTERN2_BASELINE['single'][name][str(int(warm))]
    params = {**DEFAULTS,'risk_layer_enabled':False,'atr_stop_multiplier':0,
              'risk_atr_period':0,'take_profit_r':0,'leverage':1} if disabled else {}
    body = compat.capture.response(name,params,warm)
    assert len(body['trades']) == expected['trade_count'] == 1
    assert compat.capture.digest(body['trades']) == expected['trades_sha256']
    assert compat.capture.digest(body['equity_curve']) == expected['equity_sha256']
    assert hashlib.sha256(canonical_json({k:body[k] for k in compat.capture.V2_KEYS}).encode()).hexdigest() == expected['result_v2_sha256']


def test_doji_candidate_rsi_remains_authority_when_confirmation_rsi_rises():
    data = frame('bullish_doji_reversal')
    data.iloc[25, :4] = [101,191,99,190]
    data.iloc[26:, :4] = [200,201,199,200]
    # All preceding close changes -1; candidate delta=-41.5 => RSI=0.
    # Confirmation delta=89.5 => gain=89.5/14, loss=(54.5/14)*13/14;
    # confirmation RSI > 60, yet candidate RSI=0 is the specified authority.
    result = run('bullish_doji_reversal',data)
    trade = result['_strategy'].trades[0]
    assert (trade.entry_bar,trade.entry_price,trade.tag.stop) == (26,200,pytest.approx(96.903))


@pytest.mark.parametrize('name', ['three_white_soldiers','bullish_doji_reversal'])
def test_position_filter_boolean_required(name):
    with pytest.raises(ValueError,match='INVALID_PARAMS:'):
        provider.TOOL_SPECS['local.backtesting_py.'+name]['build']({'position_filter':1})


def test_inside_no_unrequested_history_filter():
    data = frame('inside_bar_breakout').iloc[23:].copy()
    result = run('inside_bar_breakout',data)
    assert result['_strategy'].trades[0].entry_bar == 3
    assert provider.TOOL_SPECS['local.backtesting_py.inside_bar_breakout']['build']({})['min_bars'] == 3


def test_soldier_filter_off_allows_short_history():
    data = frame('three_white_soldiers').iloc[22:].copy()
    result = run('three_white_soldiers',data,{'position_filter':False})
    assert result['_strategy'].trades[0].entry_bar == 4
    assert provider.TOOL_SPECS['local.backtesting_py.three_white_soldiers']['build']({'position_filter':False})['min_bars'] == 4


@pytest.mark.parametrize('trend_filter', [False, True])
def test_inside_breakout_and_same_bar_inside_rearms_independently_of_filter(trend_filter):
    data = frame('inside_bar_breakout')
    data.iloc[25, :4] = [104,120,96,105]  # no old breakout; large future mother
    data.iloc[26, :4] = [106,115,98,109]  # > old108, strictly inside25 [96,120]
    data.iloc[27, :4] = [110,116,90,110]  # first entry at110, stop triggers for exit28
    data.iloc[28, :4] = [111,121,95,111]  # no containment/replacement, close still<=120
    data.iloc[29, :4] = [112,123,95,122]  # third/last allowed close > new mother120
    data.iloc[30:, :4] = [124,125,123,124]
    result = run('inside_bar_breakout',data,{'trend_filter':trend_filter})
    # Prior~121 makes EMA20(26)>109 but EMA20(29)<122 (independent price bounds).
    assert bool(result['_strategy']._signals[26]) is (not trend_filter)
    assert bool(result['_strategy']._signals[29]) is True
    assert list(result['_trades'].EntryBar) == ([] if trend_filter else [27])
    assert list(result['_trades'].ExitBar) == ([] if trend_filter else [28])
    trade = result['_strategy'].trades[0]
    assert (trade.entry_bar,trade.entry_price)==(30,124)
    assert trade.tag.stop==pytest.approx(95.904)  # new mother25 low96*.999
