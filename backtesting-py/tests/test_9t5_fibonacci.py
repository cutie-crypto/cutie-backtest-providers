"""9T5 independent prices: L=100, H=120, P618=107.64, SL=104.17572."""
from pathlib import Path
import sys
import json
import numpy as np
import pandas as pd
import pytest
from backtesting import Backtest
from fastapi.testclient import TestClient
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p
from strategy_time_layer import TimeConfig, TimeContext

TOOL='local.backtesting_py.fibonacci_retracement'
BASE=dict(swing_n=2)


def frame():
    # Low2 confirmed4, High5 confirmed7. Bullish retracement6 is too early;
    # confirmation7 itself is excluded. First eligible decision8, fill9.
    return pd.DataFrame(dict(
        Open =[110,108,103,108,112,116,107,107,107,109,107,108,109,110],
        High =[113,112,110,112,116,120,119,115,114,115,115,115,115,115],
        Low  =[108,105,100,105,109,112,107,107,107,106,105,105,105,105],
        Close=[111,109,105,110,114,118,108,108,108,110,108,109,110,111],
        Volume=[100]*14), index=pd.date_range('2026-01-01', periods=14, freq='h')).astype(float)


def run(data=None, params=None, warm=None):
    cls=p.TOOL_SPECS[TOOL]['build']({**BASE, **(params or {})})['strategy']
    if warm is not None:
        cls._warmup_bars=len(warm)
        cls._warmup_cols={c:warm[c].to_numpy() for c in p._WARMUP_COLUMNS}
    data=frame() if data is None else data
    if cls._time_config:
        cls._time_context=TimeContext.build(cls._time_config,'1h',data.index)
    return Backtest(data,cls,cash=100000,exclusive_orders=True,finalize_trades=True,
                    **p._leverage_backtest_kwargs(cls._risk.get('leverage',1))).run()


def response(monkeypatch,tmp_path,params=None,data=None,market='spot',warm=None):
    data=frame() if data is None else data
    monkeypatch.setattr(p,'AUTH_TOKEN','')
    monkeypatch.setattr(p,'REPORTS_DIR',tmp_path)
    monkeypatch.setattr(p,'_fetch_ohlcv',lambda *a:data.copy())
    monkeypatch.setattr(p,'_fetch_template_warmup',lambda *a:data.iloc[:0].copy() if warm is None else warm)
    monkeypatch.setattr(Backtest,'plot',lambda *a,**k:None)
    return TestClient(p.app).post('/cutie/backtest',json={'backtest':dict(run_id='9t5',
        provider_tool_id=TOOL,provider_params={**BASE, **(params or {})},symbol='BTCUSDT',market=market,
        timeframe='1h',start_at=int(data.index[0].timestamp()),end_at=int(data.index[-1].timestamp())+3600,
        initial_capital='10000',fee_bps='0',slippage_bps='0')}).json()


def test_high_must_be_confirmed_before_entry():
    stats=run()
    assert stats['_trades'].EntryBar.tolist()==[9]
    wave=stats['_strategy']._fib_waves[0]
    assert (wave['low_index'],wave['high_index'],wave['low_confirmed_at'],wave['high_confirmed_at'])==(2,5,4,7)
    assert wave['signal_index']==8
    assert float(wave['level_price'])==107.64
    assert float(wave['stop_price'])==104.17572
    assert float(wave['target_price'])==120


@pytest.mark.parametrize('level,price,stop',[(.382,112.36,104.17572),(.5,110,104.17572),
                                           (.618,107.64,104.17572),(.786,104.28,99.9)])
def test_hand_calculated_levels_and_786_stop(level,price,stop):
    data=frame()
    data.loc[data.index[8],['Open','High','Low','Close']]=[price-.1,price+.5,price-.2,price+.1]
    # Keep the new candle from being a confirmed trough before inspecting entry.
    data=data.iloc[:10]
    stats=run(data,dict(fib_level=level))
    assert stats['_trades'].EntryBar.tolist()==[9]
    wave=stats['_strategy']._fib_waves[0]
    assert float(wave['level_price'])==price
    assert float(wave['stop_price'])==stop


@pytest.mark.parametrize('target,expected',[('swing_high',120),('1.272',125.44),('1.618',132.36)])
def test_hand_calculated_targets(target,expected):
    stats=run(params=dict(fib_target=target))
    assert float(stats['_strategy']._fib_waves[0]['frozen_target'])==expected


def test_bullish_close_required():
    data=frame().iloc[:10].copy()
    data.loc[data.index[8],'Open']=108
    assert len(run(data)['_trades'])==0


@pytest.mark.parametrize('low,close,enters',[(107.96,108,True),(107.97,108,False),
                                          (107,107.32,True),(107,107.31,False)])
def test_tolerance_both_bounds(low,close,enters):
    # Upper bound=107.96292; lower bound=107.31708, independently computed.
    data=frame().iloc[:10].copy()
    data.loc[data.index[8],['Open','High','Low','Close']]=[min(close-.1,low),114,low,close]
    assert bool(len(run(data)['_trades']))==enters


def test_close_below_stop_invalidates_even_if_later_recovers():
    data=frame().iloc[:10].copy()
    data.loc[data.index[7],['Open','Low','Close']]=[105,103,104]
    stats=run(data)
    assert len(stats['_trades'])==0
    assert stats['_strategy']._fib_waves[0]['invalidated_at']==7


def test_intrabar_stop_does_not_invalidate_before_signal():
    data=frame().iloc[:10].copy()
    data.loc[data.index[8],'Low']=103
    assert len(run(data)['_trades'])==1


@pytest.mark.parametrize('gain,enters',[(20,True),(20.01,False)])
def test_minimum_gain(gain,enters):
    assert bool(len(run(params=dict(swing_min_gain_pct=gain))['_trades']))==enters


@pytest.mark.parametrize('opening,reason',[(104.17572,'frozen_stop_wrong_side_of_entry_open'),
                                          (100,'frozen_stop_wrong_side_of_entry_open'),
                                          (120,'entry_open_at_or_above_frozen_target'),
                                          (130,'entry_open_at_or_above_frozen_target')])
def test_gap_cancels_before_real_broker_fill(opening,reason):
    data=frame().iloc[:10].copy()
    data.loc[data.index[9],['Open','High','Low','Close']]=[opening,max(132,opening),min(100,opening),opening]
    stats=run(data)
    assert len(stats['_trades'])==0
    assert stats['_strategy']._fib_skips[0]['reason']==reason
    assert stats['_strategy']._fib_waves[0]['used'] is True


@pytest.mark.parametrize('low,high,holding,reason',[(103,121,1,'stop_loss'),(106,121,1,'time_expiry'),
                                                 (106,121,0,'take_profit')])
def test_touch_exit_priority_and_next_open_fill(low,high,holding,reason):
    data=frame()
    data.loc[data.index[9],['Low','High']]=[low,high]
    stats=run(data,dict(risk_layer_enabled=True,max_holding_bars=holding))
    assert stats['_trades'].ExitBar.tolist()==[10]
    assert stats['_trades'].ExitPrice.tolist()==[107]
    assert stats['_strategy']._fib_exits==[dict(decision_index=9,reason=reason)]


def test_intrinsic_defaults_touch_even_with_risk_layer_off():
    data=frame()
    data.loc[data.index[9],'Low']=103
    stats=run(data)
    assert stats['_trades'].ExitBar.tolist()==[10]
    assert stats['_strategy']._fib_exits[0]['reason']=='stop_loss'


def test_same_pair_cannot_enter_twice():
    data=frame()
    data.loc[data.index[9],'High']=121
    stats=run(data)
    assert stats['_trades'].EntryBar.tolist()==[9]
    assert stats['_trades'].ExitBar.tolist()==[10]
    assert stats['_strategy']._fib_waves[0]['signal_index']==8


def test_new_confirmed_point_discards_old_pair():
    data=frame()
    # trough6 confirms8; its new low disposes L2/H5 before the possible signal8.
    data.loc[data.index[6],'Low']=106
    assert len(run(data)['_trades'])==0


def test_consecutive_highs_do_not_pair_with_stale_low():
    data=frame()
    data.loc[data.index[8],['Open','Low','Close']]=[110,108,110]
    data.loc[data.index[9],['Open','High','Close']]=[110,121,110]
    data.loc[data.index[10],['Open','Low','Close']]=[110,106,110]
    data.loc[data.index[11],['Open','Low','Close']]=[107,105,108]
    data.loc[data.index[12],'Low']=104.5
    stats=run(data)
    assert len(stats['_strategy']._fib_waves)==1
    assert len(stats['_trades'])==0


def test_future_changes_do_not_move_earlier_entry():
    original=run(frame().iloc[:10])
    changed=frame()
    changed.loc[changed.index[10]:,['Open','High','Low','Close']]=[150,180,140,160]
    future=run(changed)
    assert original['_trades'].EntryBar.tolist()==future['_trades'].EntryBar.tolist()==[9]
    assert original['_strategy']._fib_waves[0]['signal_index']==future['_strategy']._fib_waves[0]['signal_index']==8


def test_warmup_has_confirmed_points_without_warmup_trades():
    data=frame()
    stats=run(data.iloc[7:].copy(),warm=data.iloc[:7].copy())
    assert stats['_trades'].EntryBar.tolist()==[2]
    assert stats['_strategy']._fib_waves[0]['signal_index']==8


@pytest.mark.parametrize('values', [dict(stop_loss_pct=3),dict(take_profit_pct=5),
    dict(atr_stop_multiplier=1,risk_atr_period=2,risk_layer_enabled=True),
    dict(trailing_stop_pct=3,risk_layer_enabled=True),dict(stop_loss_pct=3,take_profit_r=2,risk_layer_enabled=True),
    dict(stop_loss_pct=3,breakeven_stop=True,risk_layer_enabled=True),
    dict(stop_loss_pct=3,tp1_r=2,tp1_close_pct=100,risk_layer_enabled=True)])
def test_user_pricing_disables_whole_default_group(values):
    stats=run(params=values)
    state=stats['_strategy']._fib_frozen
    if 'take_profit_pct' in values:
        assert state.initial_stop is None
        assert float(state.take_price)==113.4
    elif 'take_profit_r' in values:
        assert float(state.take_price)==114.48
    else:
        assert state.take_price is None
    if 'stop_loss_pct' in values:
        assert float(state.initial_stop)==104.76


@pytest.mark.parametrize('params',[dict(position_size_pct=20),dict(position_size_notional=2000),
                                  dict(risk_layer_enabled=False),dict(risk_layer_enabled=True),dict(leverage=1)])
def test_non_pricing_keys_preserve_default_prices(params):
    wave=run(params=params)['_strategy']._fib_waves[0]
    assert float(wave['frozen_stop'])==104.17572
    assert float(wave['frozen_target'])==120


@pytest.mark.parametrize('params',[dict(direction='short'),dict(direction='both'),dict(swing_n=2.0),
    dict(swing_n=True),dict(swing_n=0),dict(swing_n=501),dict(swing_min_gain_pct=.99),
    dict(swing_min_gain_pct=51),dict(fib_level=.7),dict(fib_level=True),dict(fib_tolerance_pct=.049),
    dict(fib_tolerance_pct=2.01),dict(fib_tolerance_pct=float('nan')),dict(fib_target=1.272),
    dict(fib_target='2'),dict(pattern_confirmation=True)])
def test_invalid_params_before_data(monkeypatch,params):
    def forbidden(*a): raise AssertionError('invalid params must fail before data')
    monkeypatch.setattr(p,'_fetch_ohlcv',forbidden)
    request=dict(run_id='9t5bad',provider_tool_id=TOOL,provider_params=params,symbol='BTCUSDT',market='spot',
        timeframe='1h',start_at=1704067200,end_at=1704672000,initial_capital='10000')
    monkeypatch.setattr(p,'AUTH_TOKEN','')
    result=TestClient(p.app).post('/cutie/backtest',content=json.dumps({'backtest':request}),headers={'Content-Type':'application/json'})
    assert result.json()['error_type']=='INVALID_PARAMS'


def test_registered_http_report_and_frozen_result_v2(monkeypatch,tmp_path):
    data=frame()
    data.loc[data.index[9],'High']=121
    body=response(monkeypatch,tmp_path,data=data)
    assert body['result_status']=='success',body
    assert len(body['trades'])==1
    assert body['trades'][0]['opened_at']==int(data.index[9].timestamp())
    assert body['trades'][0]['closed_at']==int(data.index[10].timestamp())
    assert set(body['metrics'])=={'total_return','max_drawdown','trade_count'}
    assert set(body['trades'][0])=={'seq','opened_at','closed_at','side','qty','entry_price','exit_price','fee','slippage','pnl'}
    assert set(body['equity_curve'][0])=={'ts','equity'}
    wave=body['raw_report']['fibonacci_retracement']['waves'][0]
    assert float(wave['stop_price'])==104.17572
    assert 'fibonacci_retracement' not in body['data_manifest']
    assert 'pattern_confirmation' in body['assumptions']['fibonacci_retracement']


def test_gap_reason_survives_http(monkeypatch,tmp_path):
    data=frame().iloc[:10].copy()
    data.loc[data.index[9],['Open','Low','Close']]=[100,99,100]
    body=response(monkeypatch,tmp_path,data=data)
    assert body['result_status']=='success',body
    assert body['trades']==[]
    assert body['raw_report']['fibonacci_retracement']['skipped_entries'][0]['reason']=='frozen_stop_wrong_side_of_entry_open'


def test_filter_blocks_real_entry(monkeypatch,tmp_path):
    body=response(monkeypatch,tmp_path,params=dict(filter_layer_enabled=True,filter_ema_enabled=True,filter_ema_period=3),data=frame().iloc[:10].copy())
    assert body['result_status']=='success',body
    assert body['trades']==[]
    assert body['raw_report']['entry_filters']


def test_time_blocks_real_entry(monkeypatch,tmp_path):
    body=response(monkeypatch,tmp_path,params=dict(time_layer_enabled=True,time_session_start='12:00',time_session_end='13:00'),data=frame().iloc[:10].copy())
    assert body['result_status']=='success',body
    assert body['trades']==[]


def test_catalog_registration(monkeypatch):
    monkeypatch.setattr(p,'AUTH_TOKEN','')
    body=TestClient(p.app).get('/catalog').json()
    assert TOOL in {t['tool_id'] for t in body['tools']}
    props=p.TOOL_SPECS[TOOL]['param_schema_properties']
    assert props['fib_level']['default']==.618
    assert props['direction']['enum']==['long']
    assert 'filter_layer_enabled' in props and 'time_layer_enabled' in props


def test_simultaneous_high_low_has_no_invented_order():
    data=frame()
    data.loc[data.index[5],'Low']=95
    stats=run(data)
    assert stats['_strategy']._fib_waves==[]
    assert len(stats['_trades'])==0


def test_cancelled_gap_consumes_pair_before_another_signal():
    data=frame()
    data.loc[data.index[9],['Open','Low','Close']]=[100,99,100]
    stats=run(data)
    assert len(stats['_trades'])==0
    assert len(stats['_strategy']._fib_skips)==1
    assert stats['_strategy']._fib_waves[0]['signal_index']==8


def test_position_keeps_old_target_when_a_new_pair_confirms():
    data=frame()
    extra=pd.DataFrame(dict(Open=[110]*3,High=[115]*3,Low=[105]*3,Close=[111]*3,Volume=[100]*3),
        index=pd.date_range(data.index[-1]+pd.Timedelta(hours=1),periods=3,freq='h'))
    data=pd.concat([data,extra])
    data.loc[data.index[8],'Low']=106
    data.loc[data.index[9]:,'Low']=107
    data.loc[data.index[11],'High']=126
    data.loc[data.index[14],'High']=133
    stats=run(data,dict(fib_target='1.618'))
    assert stats['_trades'].EntryBar.tolist()==[9]
    assert stats['_trades'].ExitBar.tolist()==[15]
    waves=stats['_strategy']._fib_waves
    assert [(w['low_index'],w['high_index']) for w in waves][:2]==[(2,5),(8,11)]
    assert float(waves[0]['frozen_target'])==132.36


@pytest.mark.parametrize('params',[dict(atr_stop_multiplier=0),dict(breakeven_stop=False),dict(tp1_r=0)])
def test_explicit_inactive_pricing_keys_remove_defaults(params):
    state=run(params=params)['_strategy']._fib_frozen
    assert state.initial_stop is None and state.take_price is None


@pytest.mark.parametrize('low,close',[(107.96292,108),(107,107.31708)])
def test_exact_tolerance_boundary_is_inclusive(low,close):
    data=frame().iloc[:10].copy()
    data.loc[data.index[8],['Open','Low','Close']]=[min(low,close-.1),low,close]
    assert len(run(data)['_trades'])==1


def test_no_entry_at_final_bar():
    stats=run(frame().iloc[:9])
    assert len(stats['_trades'])==0
    assert stats['_strategy']._fib_waves[0]['used'] is False


def test_stop_boundary_close_is_valid_then_touch_exits():
    data=frame()
    data.loc[data.index[7],['Open','Low','Close']]=[105,104,104.17572]
    stats=run(data)
    assert stats['_trades'].EntryBar.tolist()==[9]
    assert stats['_strategy']._fib_waves[0]['invalid'] is False


def test_spot_leverage_rejected_before_fetch(monkeypatch):
    def forbidden(*a): raise AssertionError('spot leverage must fail before fetch')
    monkeypatch.setattr(p,'_fetch_ohlcv',forbidden)
    request=dict(run_id='9t5lev',provider_tool_id=TOOL,provider_params=dict(leverage=2),symbol='BTCUSDT',market='spot',
        timeframe='1h',start_at=1704067200,end_at=1704672000,initial_capital='10000')
    monkeypatch.setattr(p,'AUTH_TOKEN','')
    result=TestClient(p.app).post('/cutie/backtest',content=json.dumps({'backtest':request}),headers={'Content-Type':'application/json'})
    assert result.json()['error_type']=='INVALID_PARAMS'


def risk_feature_frame(feature):
    """Feature-specific suffix; the independently known entry stays at decision8."""
    data=frame()
    if feature=='breakeven':
        # Fill110, frozen stop107.73: first full post-entry close113 exceeds 1R.
        data.loc[data.index[9],['Open','High','Low','Close']]=[110,111.01,109.99,111]
        data.loc[data.index[10],['Open','High','Low','Close']]=[111,113.01,110.99,113]
    elif feature=='levels':
        # Signal108, SL86.4, R=21.6: .01R=108.216, .02R=108.432.
        # First filled bar touches level1 only; next bar touches level2.
        data.loc[data.index[9],['Open','High','Low','Close']]=[108,108.3,108,108.25]
        data.loc[data.index[10],['Open','High','Low','Close']]=[108.25,109,108,108.5]
    return data
