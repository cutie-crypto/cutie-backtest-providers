"""F5: independent UTC two-day goldens and real registered HTTP/engine paths."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from backtesting import Backtest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p
from strategy_time_layer import TimeContext

TOOL = 'local.backtesting_py.vwap_reversion'


def frame():
    # Six hours per bar, >= two UTC days. HLC3 values:
    # day1: 100(v=0),100(v=1),94(v=3),94(v=0) => None,100,95.5,95.5.
    # day2: 200(v=1),190(v=1),198(v=1),198(v=0) => 200,195,196,196.
    # Signal at 2: 94 <= 95.5*.985 = 94.0675; entry at 3=94.
    # High=100 at 3 does NOT exit; UTC flatten fills at 4=200.
    # Signal at 5: 190 <= 195*.985=192.075; entry 6=190;
    # close 198 >= VWAP196 => exit at 7=199.
    close = [100, 100, 94, 94, 200, 190, 198, 198, 300]
    opens = [100, 100, 94, 94, 200, 190, 190, 199, 300]
    highs = [100, 100, 94, 100, 200, 190, 206, 206, 300]
    lows = [100, 100, 94, 88, 200, 190, 190, 190, 300]
    return pd.DataFrame(dict(Open=opens, High=highs, Low=lows, Close=close,
        Volume=[0,1,3,0,1,1,1,0,1]), index=pd.date_range('2026-01-01', periods=9, freq='6h')).astype(float)


def run(params=None, data=None, warm=None, finalize=False):
    data = frame() if data is None else data
    cls = p.TOOL_SPECS[TOOL]['build'](params or {})['strategy']
    if warm is not None:
        cls._warmup_bars = len(warm)
        cls._warmup_index = warm.index
        cls._warmup_cols = {c:warm[c].to_numpy() for c in p._WARMUP_COLUMNS}
    if cls._time_config is not None:
        hours = int((data.index[1]-data.index[0]).total_seconds()/3600)
        cls._time_context = TimeContext.build(cls._time_config, f'{hours}h', data.index)
    return Backtest(data, cls, cash=100000, exclusive_orders=True, finalize_trades=finalize).run()


def response(monkeypatch, tmp_path, params=None, data=None, warm=None, timeframe='6h', market='spot'):
    data = frame() if data is None else data
    monkeypatch.setattr(p,'AUTH_TOKEN','')
    monkeypatch.setattr(p,'REPORTS_DIR',tmp_path)
    monkeypatch.setattr(p,'_fetch_ohlcv',lambda *a:data.copy())
    monkeypatch.setattr(p,'_fetch_template_warmup',lambda *a: data.iloc[:0] if warm is None else warm.copy())
    monkeypatch.setattr(Backtest,'plot',lambda *a,**kw:None)
    request=dict(run_id='f5',provider_tool_id=TOOL,provider_params=params or {},symbol='BTCUSDT',market=market,
        timeframe=timeframe,start_at=int(data.index[0].timestamp()),end_at=int(data.index[-1].timestamp())+21600,
        initial_capital='10000',fee_bps='0',slippage_bps='0')
    return TestClient(p.app).post('/cutie/backtest',json={'backtest':request}).json()


def test_two_day_hand_vwap_and_next_open_fills():
    # Pins the legacy close-only layer (the default 2% stop now runs the unified layer since Q42-C).
    stats=run({'risk_layer_enabled':False})
    np.testing.assert_allclose(stats['_strategy']._f5_vwap, [np.nan,100,95.5,95.5,200,195,196,196,300],equal_nan=True)
    trades=stats['_trades']
    assert trades[['EntryBar','ExitBar']].values.tolist()==[[3,4],[6,7]]
    assert trades[['EntryPrice','ExitPrice']].values.tolist()==[[94,200],[190,199]]
    assert (trades.Size>0).all()
    assert stats['_strategy']._f5_expiries==[dict(decision_at=1767312000,reason='utc_day_end')]


def test_close_price_source_independent_hand_values():
    stats=run({'time_vwap_price':'close'})
    # Day2 close prefix: 200,195,(200+190+198)/3=196,196.
    np.testing.assert_allclose(stats['_strategy']._f5_vwap,[np.nan,100,95.5,95.5,200,195,196,196,300],equal_nan=True)
    data=frame()
    data.loc[data.index[2],['High','Low']]=[106,94]  # HLC3=98 vs close94.
    hlc=run(data=data)
    close=run({'time_vwap_price':'close'},data)
    assert hlc['_strategy']._f5_vwap[2]==98.5  # (100+98*3)/4
    assert close['_strategy']._f5_vwap[2]==95.5


def test_vwap_includes_current_bar_changes_entry():
    data=frame()
    data.loc[data.index[2],['Open','High','Low','Close']]=[97,97,97,97]
    data.loc[data.index[3],'Open']=97  # Keep a wrongly queued entry above its frozen stop.
    # current VWAP=(100+3*97)/4=97.75; threshold96.28375 rejects97;
    # previous VWAP100 would wrongly accept97 <= 98.5.
    assert not (run(data=data)['_trades'].EntryBar==3).any()


def test_high_touch_does_not_close_before_reversion():
    data=frame()
    data.loc[data.index[6],['High','Low','Close']]=[208,190,190]
    # HLC3=196 => current VWAP=(200+190+196)/3=195.333...
    stats=run(data=data)
    assert stats['_trades'].iloc[1].ExitBar==8  # day2 flatten, not high-touch exit7.


def test_no_new_entry_on_daily_last_bar_or_tail():
    data=frame()
    data.loc[data.index[2],['Open','High','Low','Close']]=[100,100,100,100]
    assert not (run(data=data)['_trades'].EntryBar==4).any()
    assert run(data=frame().iloc[:3],finalize=True)['_trades'].empty


@pytest.mark.parametrize('source',['hlc3','close'])
def test_partial_opening_day_fail_closed_and_next_day_works(source):
    stats=run({'time_vwap_price':source},frame().iloc[1:])
    assert stats['_trades'][['EntryBar','ExitBar']].values.tolist()==[[5,6]]
    assert np.isnan(stats['_strategy']._f5_vwap[:3]).all()
    assert stats['_strategy']._f5_history[0]['reason']=='opening_utc_day_history_incomplete'


def test_complete_warmup_reaches_midnight_instead_of_skipping_day():
    data=frame()
    stats=run(data=data.iloc[1:],warm=data.iloc[:1])
    assert stats['_trades'][['EntryBar','ExitBar']].values.tolist()==[[2,3],[5,6]]
    assert stats['_strategy']._f5_history==[]
    assert stats['_strategy']._f5_vwap[1]==95.5


def test_warmup_gap_does_not_create_partial_vwap():
    data=frame()
    stats=run(data=data.iloc[2:],warm=data.iloc[:1])
    assert np.isnan(stats['_strategy']._f5_vwap[:2]).all()
    assert stats['_strategy']._f5_history


def test_future_prices_cannot_change_prefix_or_first_trade():
    original=run()
    data=frame()
    data.iloc[5:]=data.iloc[5:]*10
    changed=run(data=data)
    np.testing.assert_allclose(original['_strategy']._f5_vwap[:5],changed['_strategy']._f5_vwap[:5],equal_nan=True)
    pd.testing.assert_series_equal(original['_trades'].iloc[0],changed['_trades'].iloc[0])


@pytest.mark.parametrize('enabled',[False,True])
def test_frozen_signal_stop_not_actual_fill_basis(enabled):
    data=frame()
    # signal94 => stop92.12. Fill100 would instead stop98 and wrongly exit.
    data.loc[data.index[3],['Open','High','Low','Close']]=[100,100,93,94]
    stats=run({'risk_layer_enabled':enabled},data)
    assert stats['_trades'].iloc[0].ExitBar==4
    assert stats['_strategy']._f5_expiries[0]['reason']=='utc_day_end'


@pytest.mark.parametrize('kind,reason',[('stop','stop_loss'),('daily','time_expiry'),('target','take_profit')])
def test_same_bar_stop_daily_target_template_priority(kind,reason):
    data=frame()
    # At entry3, close at or above VWAP and high touches target; daily wins.
    data.loc[data.index[3],['High','Low','Close']]=[110,92 if kind=='stop' else 94,100]
    if kind=='target':
        data.index=pd.date_range('2026-01-01',periods=9,freq='3h') # entry3 no daily due.
    stats=run({'risk_layer_enabled':True,'take_profit_pct':1,'stop_loss_pct':2},data.iloc[:5])
    assert stats['_trades'].iloc[0].ExitBar==4
    assert stats['_strategy']._risk_exit_reason==reason
    if kind=='daily':
        assert stats['_strategy']._f5_expiries[0]['reason']=='utc_day_end'
    elif kind=='stop':
        assert stats['_strategy']._f5_expiries==[]


@pytest.mark.parametrize('enabled',[False,True])
@pytest.mark.parametrize('entry_open',[92,92.12])
def test_gap_cancelled_before_entry_and_disclosed(monkeypatch,tmp_path,enabled,entry_open):
    data=frame()
    data.loc[data.index[3],['Open','High','Low','Close']]=[entry_open,100,88,94]
    body=response(monkeypatch,tmp_path,{'risk_layer_enabled':enabled},data)
    assert body['result_status']=='success',body
    assert len(body['trades'])==1
    skip=body['raw_report']['vwap_reversion']['skipped_entries']
    assert len(skip)==1 and skip[0]['reason']=='frozen_stop_wrong_side_of_entry_open'
    assert float(skip[0]['frozen_stop'])==pytest.approx(92.12)


@pytest.mark.parametrize('values',[
    {'stop_loss_pct':1},{'take_profit_pct':8},
    {'risk_layer_enabled':True,'risk_atr_period':2,'atr_stop_multiplier':2,'take_profit_r':3},
    {'risk_layer_enabled':True,'trailing_stop_pct':1},
    {'risk_layer_enabled':True,'breakeven_stop':False},
])
def test_user_pricing_group_suppresses_default(values):
    risk=p.TOOL_SPECS[TOOL]['build'](values)['strategy']._risk
    if 'stop_loss_pct' not in values:
        assert 'stop_loss_pct' not in risk
    else:
        assert risk['stop_loss_pct']==.01


@pytest.mark.parametrize('params',[
    {'vwap_deviation_pct':-1.5},{'vwap_deviation_pct':0},{'vwap_deviation_pct':.49},{'vwap_deviation_pct':5.01},
    {'vwap_deviation_pct':True},{'vwap_deviation_pct':'1.5'},
    {'time_vwap_price':'bad'},{'time_vwap_price':True},{'direction':'short'},{'direction':'both'},
    {'time_session_start':'06:00'},{'atr_stop_multiplier':2},{'max_holding_bars':1},
])
def test_invalid_parameters_before_fetch(monkeypatch,params):
    monkeypatch.setattr(p,'AUTH_TOKEN','')
    monkeypatch.setattr(p,'_fetch_ohlcv',lambda *a:pytest.fail('invalid request fetched data'))
    monkeypatch.setattr(p,'_fetch_template_warmup',lambda *a:pytest.fail('invalid request fetched history'))
    request=dict(run_id='invalid',provider_tool_id=TOOL,provider_params=params,symbol='BTCUSDT',market='spot',
        timeframe='15m',start_at=1704067200,end_at=1704672000,initial_capital='10000',fee_bps='0',slippage_bps='0')
    body=TestClient(p.app).post('/cutie/backtest',json={'backtest':request}).json()
    assert body['error_type']=='INVALID_PARAMS',body


@pytest.mark.parametrize('timeframe',['1d','3d','1w','1M'])
def test_non_intraday_before_fetch(monkeypatch,timeframe):
    monkeypatch.setattr(p,'AUTH_TOKEN','')
    monkeypatch.setattr(p,'_fetch_ohlcv',lambda *a:pytest.fail('non-intraday fetched data'))
    request=dict(run_id='invalid',provider_tool_id=TOOL,provider_params={},symbol='BTCUSDT',market='spot',
        timeframe=timeframe,start_at=1704067200,end_at=1704672000,initial_capital='10000',fee_bps='0',slippage_bps='0')
    assert TestClient(p.app).post('/cutie/backtest',json={'backtest':request}).json()['error_type']=='INVALID_PARAMS'


def test_http_result_contract_assumptions_and_partial_day_note(monkeypatch,tmp_path):
    body=response(monkeypatch,tmp_path,{'time_vwap_price':'close','risk_layer_enabled':True},frame().iloc[1:])
    assert body['result_status']=='success',body
    layer=body['assumptions']['vwap_reversion']
    assert layer['price_source']=='close' and layer['stop_loss_pct']==2
    assert layer['entry_fill']==layer['exit_fill']=='next_bar_open_market'
    assert body['assumptions']['risk_layer']['initial_levels_based_on']=='entry_signal_close'
    assert body['raw_report']['vwap_reversion']['history_notes']
    import test_time_layer_compatibility as compat
    old=compat.capture.response('rsi_reversal',{})
    assert set(body['trades'][0])==set(old['trades'][0])
    for key in ('metrics','data_manifest'):
        assert set(body[key])==set(old[key])


def test_http_warmup_requested_back_to_utc_midnight(monkeypatch,tmp_path):
    data=frame()
    calls=[]
    body=response(monkeypatch,tmp_path,data=data.iloc[1:],warm=data.iloc[:1])
    assert len(body['trades'])==2
    assert body['raw_report']['vwap_reversion']['history_notes']==[]
    # Use an hourly main beginning at noon to distinguish min_bars2 vs UTC history12.
    hourly=pd.concat([frame()]*3,ignore_index=True)
    hourly.index=pd.date_range('2026-01-01',periods=len(hourly),freq='h')
    main=hourly.iloc[12:]
    def warmup(*args):
        calls.append(args[5])
        return hourly.iloc[:12].copy()
    monkeypatch.setattr(p,'_fetch_ohlcv',lambda *a:main.copy())
    monkeypatch.setattr(p,'_fetch_template_warmup',warmup)
    import test_time_layer_compatibility as compat
    request=compat.capture.request_body('vwap_reversion',{})
    request['backtest'].update(start_at=int(main.index[0].timestamp()),end_at=int(main.index[-1].timestamp())+3600)
    result=TestClient(p.app).post('/cutie/backtest',json=request).json()
    assert result['result_status']=='success',result
    assert calls==[12]


def test_http_gap_rejected_before_warmup(monkeypatch,tmp_path):
    # P-LOW5-VWAP: 1 missing of 9 (8 < 9 * 0.9) is beyond the tolerance; within it see test_plow5_vwap_gap_day_mask.
    data=frame().drop(frame().index[3])
    body=response(monkeypatch,tmp_path,data=data)
    assert body['error_type']=='TIME_DATA_GAP',body
    assert body['limitations']=={'reason':'time_data_gap','gap_count':1,'missing_bars':1,'segments':[
        {'after':'2026-01-01T12:00:00+00:00','before':'2026-01-02T00:00:00+00:00','missing_bars':1}]}


def test_optional_time_and_filter_entry_gates_preserve_exits():
    assert run({'time_layer_enabled':True,'time_session_start':'01:00','time_session_end':'02:00'})['_trades'].empty
    assert not (run({'filter_layer_enabled':True,'filter_ema_enabled':True,'filter_ema_period':2})['_trades'].EntryBar==3).any()
    stats=run({'time_layer_enabled':True,'time_session_start':'12:00','time_session_end':'19:00'})
    assert stats['_trades'].iloc[0].ExitBar==4


def test_fifteen_minute_http_target_period(monkeypatch,tmp_path):
    close=[100]*96+[200]*96+[300]*4
    close[10]=94
    close[11]=94
    data=pd.DataFrame(dict(Open=close,High=close,Low=close,Close=close,Volume=1),
        index=pd.date_range('2026-01-01',periods=len(close),freq='15min'))
    body=response(monkeypatch,tmp_path,data=data,timeframe='15m')
    assert body['result_status']=='success',body
    assert body['trades'][0]['opened_at']==int(data.index[11].timestamp())
    assert body['trades'][0]['closed_at']==int(data.index[13].timestamp())


def test_entry_threshold_is_inclusive():
    data=frame().iloc[:4].copy()
    data.loc[:,['Open','High','Low','Close']]=100.
    data.loc[data.index[1],['Open','High','Low','Close']]=[100,104,97.5,98.5]
    data.loc[:,'Volume']=[0,1,0,0]
    # HLC3=(104+97.5+98.5)/3=100; close==100*(1-.015).
    assert run(data=data)['_trades'].iloc[0].EntryBar==2


def test_default_stop_exits_and_target_only_removes_it():
    data=frame().copy()
    data.index=pd.date_range('2026-01-01',periods=9,freq='h')
    data.loc[data.index[3],['High','Low','Close']]=[94,91,91]
    default=run(data=data)
    assert default['_trades'].iloc[0].ExitBar==4
    custom=run({'take_profit_pct':90},data)
    assert custom['_trades'].iloc[0].ExitBar==5


def test_all_zero_volume_never_enters():
    data=frame()
    data.loc[:,'Volume']=0
    stats=run(data=data)
    assert stats['_trades'].empty
    assert np.isnan(stats['_strategy']._f5_vwap).all()


def test_optional_flatten_reuses_priority_before_template():
    data=frame().iloc[:5].copy()
    data.index=pd.date_range('2026-01-01',periods=5,freq='3h')
    data.loc[data.index[3],['High','Low','Close']]=[110,94,100]
    stats=run({'time_layer_enabled':True,'time_flatten_at':'12:00','take_profit_pct':1},data)
    assert stats['_trades'].iloc[0].ExitBar==4
    assert stats['_strategy']._risk_exit_reason=='time_expiry'
    assert stats['_strategy']._f5_expiries[0]['reason']=='configured_time_expiry'


@pytest.mark.parametrize('enabled',[False,True])
def test_futures_leverage_keeps_existing_settlement(monkeypatch,tmp_path,enabled):
    body=response(monkeypatch,tmp_path,{'leverage':2,'risk_layer_enabled':enabled},market='futures')
    assert body['result_status']=='success',body
    assert len(body['trades'])==2
    assert body['raw_report']['isolated_risk']['liquidation_count']==0


def test_catalog_price_key_and_intraday_only(monkeypatch):
    monkeypatch.setattr(p,'AUTH_TOKEN','')
    tools=TestClient(p.app).get('/catalog').json()['tools']
    tool=next(t for t in tools if t['tool_id']==TOOL)
    assert tool['timeframes']==['15m','30m','1h','4h']
    assert tool['param_schema']['properties']['time_vwap_price']==dict(type='string',default='hlc3',enum=['hlc3','close'])
    assert '1d' not in tool['data_source']['coverage_hint']
    with pytest.raises(ValueError,match='INVALID_PARAMS'):
        p._build_ema_cross({'time_vwap_price':'close'})


def test_unaligned_grid_rejected(monkeypatch,tmp_path):
    data=frame()
    data.index=data.index+pd.Timedelta(minutes=1)
    assert response(monkeypatch,tmp_path,data=data)['error_type']=='TIME_DATA_GAP'


def test_frozen_stop_gap_reports_actual_liquidation_distance(monkeypatch,tmp_path):
    data=frame()
    data.loc[data.index[3],['Open','High','Low','Close']]=[200,210,94,94]
    # Fill E200, leverage10 => liquidation180; frozen stop94*.98=92.12 is outside.
    body=response(monkeypatch,tmp_path,{'leverage':10},data,market='futures')
    assert body['result_status']=='success',body
    assert body['raw_report']['isolated_risk']['liquidation_count']==1
    assert body['assumptions']['isolated_margin']['stop_beyond_liquidation'] is True
