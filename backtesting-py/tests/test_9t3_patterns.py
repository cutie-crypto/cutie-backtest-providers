"""Independent hand-priced fixtures via registered builders and HTTP route."""
from pathlib import Path
import hashlib
import json
import sys

import pandas as pd
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as provider
from canonical_json import canonical_json
from strategy_bottom_patterns import bottom_signals
from test_9t1_engulf_pin import run, http, request
from test_time_layer import DEFAULTS

NAMES = ('double_bottom', 'inverse_head_shoulders')


def frame(name, *, signal=53, size=120):
    data = pd.DataFrame(dict(Open=[103.]*size, High=[104.]*size, Low=[102.]*size,
                             Close=[103.]*size, Volume=[1.]*size),
                        index=pd.date_range('2026-01-01', periods=size, freq='h'))
    if name == 'double_bottom':
        data.iloc[signal-25, 2] = 100
        data.iloc[signal-7, 2] = 100
        data.iloc[signal-18, 1] = 110
        opening = 111
    else:
        data.iloc[signal-37, 2] = 100
        data.iloc[signal-22, 2] = 95
        data.iloc[signal-7, 2] = 101
        data.iloc[signal-30, 1] = 110
        data.iloc[signal-15, 1] = 114
        opening = 119
    data.iloc[signal:, :4] = [opening, opening+1, opening-1, opening]
    return data


def compatibility_frame(name, *, signal=53, size=120):
    data = frame(name, signal=signal, size=size)
    data.iloc[signal+4, 1] = 150
    return data


def signals(name, data, **config):
    return bottom_signals(*(data[c].to_numpy() for c in ('High', 'Low', 'Close')), kind=name, **config)


@pytest.mark.parametrize('name', NAMES)
@pytest.mark.parametrize('warm', [0, 40])
def test_hand_entry_frozen_stop_target_and_points(name, warm):
    stats = run(name, frame(name), warm=warm)
    trades = stats['_strategy'].trades
    assert len(trades) == 1
    trade = trades[0]
    assert (trade.entry_bar, trade.entry_price) == (54-warm, 111 if name=='double_bottom' else 119)
    assert trade.tag.stop == pytest.approx(99.9 if name=='double_bottom' else 100.899)
    # H&S: (23,110)->(38,114), N(53)=118, N(31)=112+2/15, target=135+2/15.
    assert trade.tag.target == pytest.approx(120 if name=='double_bottom' else 135+2/15)
    report = stats['_strategy'].bottom_pattern_report
    setup = report['setups'][0]
    assert [p['index'] for p in setup['points']] == ([28,46] if name=='double_bottom' else [16,31,46])
    assert setup['confirmed_at'] == 51 and setup['breakout_bar'] == 53
    assert setup['neckline_points'] == ([(35,110)] if name=='double_bottom' else [(23,110),(38,114)])


@pytest.mark.parametrize('name', NAMES)
def test_right_n_must_complete_and_prefix_is_causal(name):
    data = frame(name)
    # Last low index46, n5 => confirmation51. A close at49 above neck is too early.
    data.iloc[49, :4] = [120, 121, 102, 120]
    full, _ = signals(name, data)
    assert full[49] is None and full[50] is None
    assert full[53] is not None
    for end in (49,50,51,52,54):
        prefix, _ = signals(name, data.iloc[:end])
        assert prefix == full[:end]
    assert not run(name, data.iloc[:51])['_strategy'].position


@pytest.mark.parametrize('name', NAMES)
def test_confirmation_close_can_break_and_fills_next_open(name):
    data = frame(name)
    data.iloc[51, :4] = [120,121,102,120]
    assert run(name, data)['_strategy'].trades[0].entry_bar == 52


@pytest.mark.parametrize('name', NAMES)
def test_close_below_stop_invalidates_before_later_breakout(name):
    data = frame(name)
    data.iloc[52, :4] = [102,104,98,99]
    stats = run(name, data)
    assert not stats['_strategy'].position
    assert stats['_strategy'].bottom_pattern_report['setups'][0]['status'] == 'invalidated_close'


@pytest.mark.parametrize('name', NAMES)
def test_new_confirmed_low_replaces_old_candidate(name):
    data = frame(name)
    data.iloc[53:, :4] = [103,104,102,103]
    data.iloc[52, 2] = 101.5
    data.iloc[60:, :4] = [120,121,119,120]
    stats = run(name, data)
    assert not stats['_strategy'].position
    assert stats['_trades'].empty
    assert signals(name,data)[0][60] is None
    assert stats['_strategy'].bottom_pattern_report['setups'][0]['status'] == 'invalidated_new_low'


@pytest.mark.parametrize('gap,valid', [(9,False),(10,True),(60,True),(61,False)])
def test_double_gap_inclusive(gap, valid):
    data = frame('double_bottom', signal=100, size=150)
    data.iloc[75,2]=102
    data.iloc[93-gap,2]=100
    data.iloc[:100,1]=104
    data.iloc[93-gap+1,1]=110
    result = run('double_bottom', data)
    assert bool(result['_strategy'].position) is valid


@pytest.mark.parametrize('low,valid', [(101,True),(101.005,False),(101.01,False)])
def test_double_tolerance_uses_min_denominator(low,valid):
    data = frame('double_bottom')
    data.iloc[46,2]=low
    assert bool(run('double_bottom', data)['_strategy'].position) is valid


@pytest.mark.parametrize('peak,valid', [(103.0308,False),(103.0309,True),(103.031,True)])
def test_rebound_uses_higher_low_and_high(peak, valid):
    data=frame('double_bottom')
    data.iloc[46,2]=100.03
    data.iloc[:53,1]=103.01
    data.iloc[:53,0]=103
    data.iloc[:53,3]=103
    # higher low100.03 requires 103.0309 (separate stronger boundary below).
    data.iloc[35,1]=peak
    assert (signals('double_bottom',data)[0][53] is not None) is valid


def test_neckline_is_high_not_max_close():
    data=frame('double_bottom')
    data.iloc[53:, :4]=[109,109.5,108,109]
    assert signals('double_bottom',data)[0][53] is None  # max Close103, max High110
    stats=run('double_bottom',data)
    assert not stats['_strategy'].position and stats['_trades'].empty


@pytest.mark.parametrize('name,neck', [('double_bottom',110),('inverse_head_shoulders',118)])
def test_strict_close_above_neckline(name,neck):
    data=frame(name)
    data.iloc[53:, :4]=[neck,neck+1,neck-1,neck]
    # For rising H&S subsequent N(t)>118; equality at53 never breaks.
    assert not run(name,data)['_strategy'].position


@pytest.mark.parametrize('head,shoulder,valid', [(98,100,True),(98.001,100,False),(95,103,True),(95,103.01,False)])
def test_head_depth_and_shoulders_min_denominator(head,shoulder,valid):
    data=frame('inverse_head_shoulders')
    data.iloc[31,2]=head
    data.iloc[46,2]=shoulder
    # Keep right shoulder below its five neighbours when raising it to103.
    data.iloc[41:52, :4]=[105,106,104,105]
    data.iloc[46,2]=shoulder
    assert bool(run('inverse_head_shoulders',data)['_strategy'].position) is valid


@pytest.mark.parametrize('name', NAMES)
def test_gap_wrong_side_skipped_and_consumed(name):
    data=frame(name)
    data.iloc[54, :4]=[99,120,98,110]
    stats=run(name,data)
    assert not stats['_strategy'].position
    report=stats['_strategy'].bottom_pattern_report
    assert report['skipped_entry_count']==1
    assert report['skipped_entries'][0]['reason']=='entry_open_at_or_below_frozen_stop'


@pytest.mark.parametrize('name', NAMES)
@pytest.mark.parametrize('reason', ['stop_loss','time_expiry','take_profit'])
def test_exit_priority_and_next_open_fill(name,reason):
    data=frame(name)
    data.iloc[56,1]=150  # target trigger, independent of gap/close
    params={}
    if reason=='stop_loss':
        data.iloc[56,2]=90
        params={'risk_layer_enabled':True,'max_holding_bars':3}
    elif reason=='time_expiry':
        params={'risk_layer_enabled':True,'max_holding_bars':3}
    data.iloc[57, :4]=[112,113,111,112]
    stats=run(name,data,params)
    trade=stats['_trades'].iloc[0]
    assert (trade.EntryBar,trade.ExitBar,trade.ExitPrice)==(54,57,112)
    assert stats['_strategy']._risk_exit_reason==reason


@pytest.mark.parametrize('name', NAMES)
def test_http_raw_details_and_frozen_v2_contract(monkeypatch,tmp_path,name):
    body=http(monkeypatch,tmp_path,name,compatibility_frame(name))
    assert len(body['trades'])==1
    assert 'next bar open' in body['assumptions']['pattern_execution']
    assert body['raw_report']['bottom_pattern']['setups'][0]['status']=='breakout'
    assert set(body['trades'][0])=={'seq','side','opened_at','closed_at','entry_price','exit_price','qty','fee','slippage','pnl'}
    from test_time_layer_compatibility import capture
    old=capture.response('bullish_engulfing',{})
    for key in ('metrics','data_manifest'):
        assert set(body[key])==set(old[key])


@pytest.mark.parametrize('name', NAMES)
@pytest.mark.parametrize('market', ['spot','futures'])
@pytest.mark.parametrize('side', ['short','both'])
def test_direction_rejected_before_fetch(monkeypatch,name,market,side):
    monkeypatch.setattr(provider,'AUTH_TOKEN','')
    monkeypatch.setattr(provider,'_fetch_ohlcv',lambda *a: pytest.fail('must reject before fetch'))
    monkeypatch.setattr(provider,'_fetch_template_warmup',lambda *a: pytest.fail('must reject before warmup'))
    req=request(name,{'direction':side},frame(name))
    req['backtest']['market']=market
    body=TestClient(provider.app).post('/cutie/backtest',json=req).json()
    assert body['error_type']=='INVALID_PARAMS'


@pytest.mark.parametrize('name,params', [
    ('double_bottom',{'swing_n':True}),('double_bottom',{'swing_n':5.0}),
    ('double_bottom',{'swing_n':0}),('double_bottom',{'swing_n':501}),
    ('double_bottom',{'min_gap_bars':61,'max_gap_bars':60}),
    ('double_bottom',{'bottom_tolerance_pct':-1}),('double_bottom',{'min_rebound_pct':0}),
    ('inverse_head_shoulders',{'head_depth_pct':0}),('inverse_head_shoulders',{'shoulder_tolerance_pct':-1}),
    *[(name,{key:value}) for name in NAMES for key,value in [('stop_loss_pct',3),('take_profit_pct',5),('trailing_stop_pct',1),('take_profit_r',2)]],
])
def test_bad_params_rejected_before_fetch(monkeypatch,name,params):
    monkeypatch.setattr(provider,'AUTH_TOKEN','')
    monkeypatch.setattr(provider,'_fetch_ohlcv',lambda *a: pytest.fail('must reject before fetch'))
    body=TestClient(provider.app).post('/cutie/backtest',json=request(name,params,frame(name))).json()
    assert body['error_type']=='INVALID_PARAMS'


@pytest.mark.parametrize('name',NAMES)
@pytest.mark.parametrize('warm',[False,True])
def test_off_state_v2_fingerprint(name,warm):
    import test_time_layer_compatibility as time_compat
    body=time_compat.capture.response(name,dict(DEFAULTS,risk_layer_enabled=False),warm)
    expected=time_compat.PATTERN3_BASELINE['single'][name][str(int(warm))]
    v2={key:body[key] for key in time_compat.capture.V2_KEYS}
    assert hashlib.sha256(canonical_json(v2).encode()).hexdigest()==expected['result_v2_sha256']
    assert len(body['trades'])==expected['trade_count']==1


def test_last_bar_breakout_has_no_fill():
    assert not run('double_bottom',frame('double_bottom').iloc[:54],finalize=True)['_strategy'].position


def test_n_is_adjustable_and_equal_high_peak_chooses_earliest():
    data=frame('double_bottom')
    data.iloc[36,1]=110
    points=signals('double_bottom',data,n=2)[1]
    assert points[0]['confirmed_at']==48
    assert points[0]['neckline_points']==[(35,110)]


@pytest.mark.parametrize('name', NAMES)
def test_frozen_target_does_not_follow_next_open_gap(name):
    data=frame(name)
    data.iloc[54, :4]=[115,116,114,115]
    trade=run(name,data)['_strategy'].trades[0]
    assert trade.entry_price==115
    assert trade.tag.target==pytest.approx(120 if name=='double_bottom' else 135+2/15)


@pytest.mark.parametrize('name', NAMES)
def test_invalidation_is_strict_close_not_wick(name):
    data=frame(name)
    stop=99.9 if name=='double_bottom' else 100.899
    data.iloc[52, :4]=[102,104,98,stop]
    assert signals(name,data)[0][53] is not None  # low below, close exactly stop


@pytest.mark.parametrize('name', NAMES)
def test_neckline_excludes_endpoint_highs(name):
    data=frame(name)
    for index in ([28,46] if name=='double_bottom' else [16,31,46]):
        data.iloc[index,1]=200
    trade=run(name,data)['_strategy'].trades[0]
    assert trade.tag.target==pytest.approx(120 if name=='double_bottom' else 135+2/15)


def test_descending_neckline_and_head_value_hand_calculation():
    data=frame('inverse_head_shoulders')
    data.iloc[23,1]=114
    data.iloc[38,1]=110
    data.iloc[53:, :4]=[107,108,106,107]
    trade=run('inverse_head_shoulders',data)['_strategy'].trades[0]
    # N53=106, N31=111+13/15 => 106+111+13/15-95=122+13/15.
    assert trade.tag.target==pytest.approx(122+13/15)


def test_excessive_falling_neckline_invalidated_geometry():
    data=frame('inverse_head_shoulders')
    data.iloc[23,1]=150
    data.iloc[38,1]=110
    _,details=signals('inverse_head_shoulders',data)
    assert details[0]['status']=='invalidated_geometry'
    assert not run('inverse_head_shoulders',data)['_strategy'].position


@pytest.mark.parametrize('name', NAMES)
def test_http_skip_reason_and_no_trade(monkeypatch,tmp_path,name):
    data=frame(name)
    data.iloc[54, :4]=[99,120,98,110]
    body=http(monkeypatch,tmp_path,name,data)
    assert not body['trades']
    assert body['raw_report']['bottom_pattern']['skipped_entry_count']==1


@pytest.mark.parametrize('name', NAMES)
def test_time_session_filters_consume_breakout(name):
    data=compatibility_frame(name)
    stats=run(name,data,dict(time_layer_enabled=True,time_session_start='08:00',time_session_end='10:00'))
    assert not stats['_strategy'].position and stats['_trades'].empty
