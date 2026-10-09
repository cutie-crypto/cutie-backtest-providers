"""Futures bearish patterns: independent scalar geometry and registered HTTP/broker proof."""
from pathlib import Path
from unittest.mock import patch
import hashlib
import ast
from decimal import Decimal as D
import json
import subprocess
import sys
import tempfile

import pandas as pd
import pytest
from backtesting import Backtest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p
from canonical_json import canonical_json
from strategy_top_patterns import top_signals
from test_9t1_engulf_pin import run
from test_time_layer import DEFAULTS

NAMES = ('double_top', 'head_shoulders')
V2_KEYS = ('schema_version', 'trades', 'equity_curve', 'metrics', 'data_manifest')
GOLDEN_PATH = Path(__file__).parent / 'fixtures/short_pat1_off.json'


def frame(name, *, signal=53, size=120):
    data = pd.DataFrame(dict(Open=[97.]*size, High=[98.]*size, Low=[96.]*size,
                            Close=[97.]*size, Volume=[1.]*size),
                        index=pd.date_range('2026-01-01', periods=size, freq='h'))
    if name == 'double_top':
        data.iloc[signal-25, 1] = 100
        data.iloc[signal-7, 1] = 100
        data.iloc[signal-18, 2] = 90
        opening = 89
    else:
        data.iloc[signal-37, 1] = 100
        data.iloc[signal-22, 1] = 105
        data.iloc[signal-7, 1] = 99
        data.iloc[signal-30, 2] = 90
        data.iloc[signal-15, 2] = 86
        opening = 81
    data.iloc[signal:, :4] = [opening, opening+1, opening-1, opening]
    return data


def compatibility_frame(name, *, signal=53, size=120):
    data = frame(name, signal=signal, size=size)
    data.iloc[signal+4, 2] = 50
    return data


def signals(name, data, **config):
    return top_signals(*(data[c].to_numpy() for c in ('High','Low','Close')), kind=name, **config)


def request(name, params, data):
    return {'backtest': dict(run_id='shortpat1', provider_tool_id='local.backtesting_py.'+name,
        provider_params=params, symbol='BTCUSDT', market='futures', timeframe='1h',
        start_at=int(data.index[0].timestamp()), end_at=int(data.index[-1].timestamp())+3600,
        initial_capital='10000', fee_bps='0', slippage_bps='0')}


def http(monkeypatch, tmp_path, name, data, params=None, *, fee=0, slip=0):
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a, **kw: data.copy())
    monkeypatch.setattr(p, '_fetch_template_warmup', lambda *a, **kw: data.iloc[:0].copy())
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **kw: None)
    req=request(name,params or {},data)
    req['backtest'].update(fee_bps=str(fee),slippage_bps=str(slip))
    body = TestClient(p.app).post('/cutie/backtest', json=req).json()
    assert body['result_status'] == 'success', body
    return body


@pytest.mark.parametrize('name', NAMES)
@pytest.mark.parametrize('warm', [0,40])
def test_hand_entry_frozen_levels_and_points(name,warm):
    stats=run(name,frame(name),warm=warm)
    assert len(stats['_strategy'].trades)==1
    trade=stats['_strategy'].trades[0]
    assert trade.is_short
    assert (trade.entry_bar,trade.entry_price)==(54-warm,89 if name=='double_top' else 81)
    assert trade.tag.stop==pytest.approx(100.1 if name=='double_top' else 99.099)
    # H&S: (23,90)->(38,86); N53=82, N31=87+13/15; 82-105+87+13/15=64+13/15.
    assert trade.tag.target==pytest.approx(80 if name=='double_top' else 64+13/15)
    setup=stats['_strategy'].top_pattern_report['setups'][0]
    assert [pt['index'] for pt in setup['points']]==([28,46] if name=='double_top' else [16,31,46])
    assert setup['confirmed_at']==51 and setup['breakout_bar']==53
    assert setup['neckline_points']==([(35,90)] if name=='double_top' else [(23,90),(38,86)])


@pytest.mark.parametrize('name', NAMES)
def test_right_n_complete_and_future_mutation(name):
    data=frame(name)
    # H2/S2 at46 is unconfirmed at49 and50; fifth right candle closes at51.
    data.iloc[49,:4]=[80,98,79,80]
    full,_=signals(name,data)
    assert full[49] is None and full[50] is None and full[53] is not None
    for end in (49,50,51,52,54):
        assert signals(name,data.iloc[:end])[0]==full[:end]
    assert not run(name,data.iloc[:51])['_strategy'].position
    data.iloc[60:,:4]=[500,510,1,2]
    assert signals(name,data)[0][:60]==full[:60]


@pytest.mark.parametrize('name', NAMES)
def test_confirmation_close_can_break(name):
    data=frame(name)
    data.iloc[51,:4]=[80,98,79,80]
    assert run(name,data)['_strategy'].trades[0].entry_bar==52


@pytest.mark.parametrize('name', NAMES)
def test_close_above_stop_invalidates(name):
    data=frame(name)
    data.iloc[52,:4]=[98,102,96,101]
    assert signals(name,data)[0][53] is None
    assert signals(name,data)[1][0]['status']=='invalidated_close'
    assert not run(name,data)['_strategy'].position


@pytest.mark.parametrize('name', NAMES)
def test_new_confirmed_high_invalidates(name):
    data=frame(name)
    data.iloc[53:,:4]=[97,98,96,97]
    data.iloc[52,1]=98.5
    data.iloc[60:,:4]=[80,81,79,80]
    stats=run(name,data)
    assert not stats['_strategy'].position and stats['_trades'].empty
    assert signals(name,data)[1][0]['status']=='invalidated_new_high'


@pytest.mark.parametrize('gap,valid',[(9,False),(10,True),(60,True),(61,False)])
def test_gap_inclusive(gap,valid):
    data=frame('double_top',signal=100,size=150)
    data.iloc[75,1]=98
    data.iloc[93-gap,1]=100
    data.iloc[:100,2]=96
    data.iloc[93-gap+1,2]=90
    assert bool(run('double_top',data)['_strategy'].position) is valid


@pytest.mark.parametrize('h2,valid',[(101,True),(101.005,False),(101.01,False)])
def test_tolerance_min_denominator(h2,valid):
    data=frame('double_top'); data.iloc[46,1]=h2
    assert bool(run('double_top',data)['_strategy'].position) is valid


@pytest.mark.parametrize('low,valid',[(96.9902,True),(96.9903,True),(96.9904,False)])
def test_decline_lower_high_strict_boundary(low,valid):
    # H1=100, H2=99.99; threshold 99.99*.97=96.9903. Neck must be <= threshold.
    data=frame('double_top'); data.iloc[46,1]=99.99; data.iloc[:53,2]=97
    data.iloc[35,2]=low
    assert (signals('double_top',data)[0][53] is not None) is valid


def test_neckline_uses_low_not_close():
    data=frame('double_top'); data.iloc[53:,:4]=[91,92,90,91]
    assert signals('double_top',data)[0][53] is None
    assert not run('double_top',data)['_strategy'].position


@pytest.mark.parametrize('name,neck',[('double_top',90),('head_shoulders',82)])
def test_strict_below_neckline(name,neck):
    data=frame(name); data.iloc[53:,:4]=[neck,neck+1,neck-1,neck]
    assert not run(name,data)['_strategy'].position


@pytest.mark.parametrize('head,shoulder,valid',[(102,100,True),(101.999,100,False),(105.06,103,True),(105.06,103.01,False)])
def test_head_and_shoulders_thresholds(head,shoulder,valid):
    data=frame('head_shoulders'); data.iloc[31,1]=head; data.iloc[46,1]=shoulder
    assert bool(run('head_shoulders',data)['_strategy'].position) is valid


@pytest.mark.parametrize('name',NAMES)
@pytest.mark.parametrize('opening,skip',[(100.101,True),(100.1,True),(100.099,False)])
def test_gap_wrong_side(name,opening,skip):
    data=frame(name)
    stop=100.1 if name=='double_top' else 99.099
    opening+=stop-100.1
    data.iloc[54,:4]=[opening,102,79,89]
    stats=run(name,data)
    report=stats['_strategy'].top_pattern_report
    assert report['skipped_entry_count']==int(skip)
    if skip:
        assert stats['_trades'].empty and not stats['_strategy'].position
        assert report['skipped_entries'][0]['reason']=='entry_open_at_or_above_frozen_stop'
    else:
        assert stats['_trades'].iloc[0].EntryBar==54


@pytest.mark.parametrize('name',NAMES)
@pytest.mark.parametrize('reason',['stop_loss','time_expiry','take_profit'])
def test_exit_priority_and_next_open(name,reason):
    data=frame(name); data.iloc[56,2]=50
    params={}
    if reason=='stop_loss':
        data.iloc[56,1]=110; params=dict(risk_layer_enabled=True,max_holding_bars=3)
    elif reason=='time_expiry':
        params=dict(risk_layer_enabled=True,max_holding_bars=3)
    data.iloc[57,:4]=[88,89,87,88]
    stats=run(name,data,params)
    trade=stats['_trades'].iloc[0]
    assert (trade.EntryBar,trade.ExitBar,trade.ExitPrice)==(54,57,88)
    assert stats['_strategy']._risk_exit_reason==reason


@pytest.mark.parametrize('name',NAMES)
def test_time_gate_and_flatten_priority(name):
    data=compatibility_frame(name)
    denied=run(name,data,dict(time_layer_enabled=True,time_session_start='08:00',time_session_end='10:00'))
    assert denied['_trades'].empty and not denied['_strategy'].position
    # Break at05:00 -> fill06:00, flatten at10:00 -> fill10:00 (outside session).
    allowed=run(name,data,dict(time_layer_enabled=True,time_session_start='06:00',time_session_end='10:00',time_flatten_at='10:00'))
    assert (allowed['_trades'].iloc[0].EntryBar,allowed['_trades'].iloc[0].ExitBar)==(54,58)
    assert allowed['_strategy']._risk_exit_reason=='time_expiry'


@pytest.mark.parametrize('name',NAMES)
def test_http_v2_keys_and_raw_details(monkeypatch,tmp_path,name):
    body=http(monkeypatch,tmp_path,name,compatibility_frame(name))
    assert len(body['trades'])==1 and body['trades'][0]['side']=='short'
    assert body['raw_report']['top_pattern']['setups'][0]['status']=='breakout'
    assert 'next bar open' in body['assumptions']['pattern_execution']
    assert set(body['trades'][0])=={'seq','side','opened_at','closed_at','entry_price','exit_price','qty','fee','slippage','pnl'}
    assert set(body['metrics'])=={'total_return','max_drawdown','trade_count'}
    assert set(body['data_manifest'])=={'source','symbol','market','timeframe','start_at','end_at','kline_count','checksum','checksum_algo'}
    assert 'isolated_risk' not in body and 'top_pattern' not in body


@pytest.mark.parametrize('name',NAMES)
def test_http_skip_report(monkeypatch,tmp_path,name):
    data=frame(name); data.iloc[54,:4]=[110,111,79,89]
    body=http(monkeypatch,tmp_path,name,data)
    assert not body['trades'] and body['raw_report']['top_pattern']['skipped_entry_count']==1


@pytest.mark.parametrize('name',NAMES)
@pytest.mark.parametrize('market',[None,'spot'])
@pytest.mark.parametrize('side',[None,'short'])
def test_spot_and_default_market_reject_before_fetch(monkeypatch,name,market,side):
    monkeypatch.setattr(p,'AUTH_TOKEN','')
    monkeypatch.setattr(p,'_fetch_ohlcv',lambda *a,**kw:pytest.fail('must reject before fetch'))
    monkeypatch.setattr(p,'_fetch_template_warmup',lambda *a,**kw:pytest.fail('must reject before warmup'))
    req=request(name,{} if side is None else {'direction':side},frame(name))
    if market is None: req['backtest'].pop('market')
    else: req['backtest']['market']=market
    body=TestClient(p.app).post('/cutie/backtest',json=req).json()
    assert body['error_type']=='INVALID_PARAMS'


@pytest.mark.parametrize('name',NAMES)
@pytest.mark.parametrize('params',[
    {'direction':'long'},{'direction':'both'},{'swing_n':True},{'swing_n':5.0},{'swing_n':0},{'swing_n':501},
    {'stop_loss_pct':3},{'take_profit_pct':5},{'trailing_stop_pct':1},{'take_profit_r':2},
    {'breakeven_stop':True},{'atr_stop_multiplier':1},{'tp1_r':1,'tp1_close_pct':50},
    {'filter_layer_enabled':True},{'time_session_start':'06:00'},{'leverage':21},
])
def test_bad_params_before_fetch(monkeypatch,name,params):
    monkeypatch.setattr(p,'AUTH_TOKEN','')
    monkeypatch.setattr(p,'_fetch_ohlcv',lambda *a,**kw:pytest.fail('invalid params fetched'))
    body=TestClient(p.app).post('/cutie/backtest',json=request(name,params,frame(name))).json()
    assert body['error_type']=='INVALID_PARAMS'


@pytest.mark.parametrize('name,params',[
    ('double_top',{'min_gap_bars':61,'max_gap_bars':60}),('double_top',{'min_gap_bars':10.0}),
    ('double_top',{'top_tolerance_pct':-1}),('double_top',{'min_decline_pct':0}),
    ('head_shoulders',{'head_height_pct':0}),('head_shoulders',{'shoulder_tolerance_pct':-1}),
])
def test_geometry_params_rejected(name,params):
    with pytest.raises(ValueError,match='INVALID_PARAMS'):
        p.TOOL_SPECS['local.backtesting_py.'+name]['build'](params)


@pytest.mark.parametrize('name',NAMES)
def test_freeze_targets_and_close_only_invalidation(name):
    data=frame(name); stop=100.1 if name=='double_top' else 99.099
    data.iloc[52,:4]=[98,110,96,stop]
    assert signals(name,data)[0][53] is not None
    data.iloc[54,:4]=[85,86,84,85]
    trade=run(name,data)['_strategy'].trades[0]
    assert trade.entry_price==85
    assert trade.tag.target==pytest.approx(80 if name=='double_top' else 64+13/15)


@pytest.mark.parametrize('name',NAMES)
def test_neckline_excludes_endpoints_and_last_bar_never_fills(name):
    data=frame(name)
    for i in ([28,46] if name=='double_top' else [16,31,46]): data.iloc[i,2]=1
    trade=run(name,data)['_strategy'].trades[0]
    assert trade.tag.target==pytest.approx(80 if name=='double_top' else 64+13/15)
    assert not run(name,frame(name).iloc[:54],finalize=True)['_strategy'].position


def test_rising_neckline_hand_target():
    data=frame('head_shoulders'); data.iloc[23,2]=86; data.iloc[38,2]=90
    data.iloc[53:,:4]=[93,94,92,93]
    trade=run('head_shoulders',data)['_strategy'].trades[0]
    # N53=94, N31=88+2/15; target94-105+88+2/15=77+2/15.
    assert trade.tag.target==pytest.approx(77+2/15)


def test_adjustable_n_and_equal_trough_earliest():
    data=frame('double_top'); data.iloc[36,2]=90
    _,details=signals('double_top',data,n=2)
    assert details[0]['confirmed_at']==48 and details[0]['neckline_points']==[(35,90)]


@pytest.mark.parametrize('name',NAMES)
@pytest.mark.parametrize('risk_enabled',[False,True])
@pytest.mark.parametrize('scenario',['near_stop','equal_stop','liquidation','gap','insolvency'])
def test_registered_isolated_arbitration(monkeypatch,tmp_path,name,risk_enabled,scenario):
    data=frame(name)
    # Double: E89, L5 => Q106.8, frozen stop100.1. HS: E81 => Q97.2 < stop99.099.
    params=dict(leverage=5,risk_layer_enabled=risk_enabled,position_size_pct=20)
    if scenario=='equal_stop':
        params['leverage']=10
        opening=91 if name=='double_top' else 90.09 # E*11/10 exactly equals the frozen stop.
        data.iloc[54,:4]=[opening,opening+1,opening-1,opening]
    if scenario=='near_stop' and name=='head_shoulders':
        data.iloc[54,:4]=[85,86,84,85] # Q102 > stop99.099
    if scenario=='insolvency' and name=='double_top':
        data.iloc[54,:4]=[82,83,81,82] # Q98.4 < frozen stop100.1; must liquidate before insolvency.
    opening=data.iloc[54,0]
    q=float(D(str(opening))*D(params['leverage']+1)/params['leverage'])
    if scenario=='gap': data.iloc[56,:4]=[120,121,50,120]
    elif scenario=='insolvency':
        params['position_size_pct']=50
        data.iloc[56,:4]=[opening,200,50,200]
    elif scenario=='liquidation': data.iloc[56,:4]=[opening,110,50,opening]
    else: data.iloc[56,:4]=[opening,q+1,50,opening]
    body=http(monkeypatch,tmp_path,name,data,params,fee=10,slip=5)
    isolated=body['raw_report']['isolated_risk']
    liq=scenario in ('equal_stop','gap','insolvency') or (name=='head_shoulders' and scenario=='liquidation')
    assert isolated['liquidation_count']==int(liq)
    assert len(body['trades'])==1
    if liq:
        t=body['trades'][0]
        assert float(t['exit_price'])==pytest.approx(120 if scenario=='gap' else q)
        assert t['closed_at']==int(data.index[56].timestamp())
        assert isolated['liquidations'][0]['liquidation_gap'] is (scenario=='gap')
        assert body['assumptions']['isolated_margin']['funding_rate_included'] is False
        recompute_money(body,data,fee=10,slip=5)
    else:
        assert body['trades'][0]['closed_at']==int(data.index[57].timestamp())


def recompute_money(body,data,*,fee,slip):
    equity=D(10000)
    bars={int(t.timestamp()):bar for t,bar in data.iterrows()}
    curve={pt['ts']:D(pt['equity']) for pt in body['equity_curve']}
    for t in body['trades']:
        e,x,q=(D(t[k]) for k in ('entry_price','exit_price','qty'))
        for price,ts in ((e,t['opened_at']),(x,t['closed_at'])):
            bar=bars[ts]
            assert D(str(bar.Low))<=price<=D(str(bar.High))
        f=(e+x)*q*D(fee)/10000
        cost=(e+x)*q*D(slip)/10000
        pnl=(e-x)*q-f-cost
        assert D(t['fee'])==f and D(t['slippage'])==cost and D(t['pnl'])==pnl
        equity+=pnl
        assert curve[t['closed_at']]==equity
    assert D(body['metrics']['total_return'])==(equity-10000)/10000


def off_response(name,params,warm):
    # Same inputs for absent keys and explicit OFF; read frozen fixture after capture.
    if GOLDEN_PATH.exists():
        frozen=json.loads(GOLDEN_PATH.read_text())['inputs'][name]
        full=pd.DataFrame(frozen['columns'],index=pd.to_datetime(frozen['index']))
    else:
        full=compatibility_frame(name,signal=113,size=200)
    data=full.iloc[60:].copy()
    with tempfile.TemporaryDirectory(prefix='shortpat1-off-') as reports, \
         patch.object(p,'AUTH_TOKEN',''), patch.object(p,'REPORTS_DIR',Path(reports)), \
         patch.object(p,'_fetch_ohlcv',lambda *a,**kw:data.copy()), \
         patch.object(p,'_fetch_template_warmup',lambda *a,**kw:full.iloc[:60].copy() if warm else full.iloc[:0].copy()), \
         patch.object(Backtest,'plot',return_value=None):
        req=request(name,params,data)
        req['backtest'].update(run_id='time_compat',fee_bps='10',slippage_bps='5')
        body=TestClient(p.app).post('/cutie/backtest',json=req).json()
        assert body['result_status']=='success',body
        return body


def fingerprint(body):
    digest=lambda value:hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
    return dict(trade_count=len(body['trades']),warmup_bars=body['assumptions']['indicator_warmup_bars'],
        assumptions_sha256=digest(body['assumptions']),raw_report_sha256=digest(body['raw_report']),
        trades_sha256=digest(body['trades']),equity_sha256=digest(body['equity_curve']),
        result_v2_sha256=hashlib.sha256(canonical_json({key:body[key] for key in V2_KEYS}).encode()).hexdigest())


@pytest.mark.parametrize('name',NAMES)
@pytest.mark.parametrize('warm',[False,True])
@pytest.mark.parametrize('explicit',[False,True])
def test_frozen_off_bytes(name,warm,explicit,monkeypatch):
    frozen=json.loads(GOLDEN_PATH.read_text())
    monkeypatch.setattr(p,'_isolated_liquidation_candidate',lambda *a,**kw:pytest.fail('OFF arbitration'))
    monkeypatch.setattr(p._FixedRiskMixin,'_isolated_install_settlement',lambda *a,**kw:pytest.fail('OFF settlement'))
    monkeypatch.setattr(p.TimeContext,'build',lambda *a,**kw:pytest.fail('OFF time context'))
    params={**DEFAULTS,'risk_layer_enabled':False,'leverage':1} if explicit else {}
    body=off_response(name,params,warm)
    assert fingerprint(body)==frozen['single'][name][str(int(warm))]
    assert len(body['trades'])==1
    assert 'isolated_risk' not in body['raw_report'] and 'time_layer' not in body['raw_report']


def test_long_source_and_golden_files_byte_unchanged():
    root=Path(__file__).resolve().parents[2]
    for relative in ('backtesting-py/strategy_bottom_patterns.py','backtesting-py/tests/test_9t3_patterns.py',
                     'backtesting-py/tests/fixtures/9t3_bottom_off.json'):
        assert (root/relative).read_bytes()==subprocess.check_output(['git','show','32ae030:'+relative],cwd=root)


@pytest.mark.parametrize('name',NAMES)
def test_warmup_requested_from_futures_and_adjustable_min_bars(monkeypatch,tmp_path,name):
    data=compatibility_frame(name,signal=113,size=200)
    calls=[]
    def fetch(exchange,market,*args):
        calls.append(('main',market)); return data.iloc[60:].copy()
    def warmup(exchange,market,symbol,timeframe,start,count,main):
        calls.append(('warm',market,count)); return data.iloc[:60].copy()
    monkeypatch.setattr(p,'AUTH_TOKEN','')
    monkeypatch.setattr(p,'REPORTS_DIR',tmp_path)
    monkeypatch.setattr(p,'_fetch_ohlcv',fetch)
    monkeypatch.setattr(p,'_fetch_template_warmup',warmup)
    monkeypatch.setattr(Backtest,'plot',lambda *a,**kw:None)
    body=TestClient(p.app).post('/cutie/backtest',json=request(name,{'swing_n':2},data.iloc[60:])).json()
    assert body['result_status']=='success' and body['assumptions']['indicator_warmup_bars']==60
    assert calls==[('main','futures'),('warm','futures',15 if name=='double_top' else 11)]
    assert body['trades'][0]['opened_at']==int(data.index[114].timestamp())


@pytest.mark.parametrize('name',NAMES)
def test_catalog_short_only_and_tool_count(name):
    catalog=p._catalog_tool('local.backtesting_py.'+name,p.TOOL_SPECS['local.backtesting_py.'+name],['BTCUSDT'])
    assert catalog['markets']==['futures']
    assert catalog['param_schema']['properties']['direction']['enum']==['short']
    # 集成 D：做空一基于 main 32ae030（48 个）+2 = 50；同批合入 10-E 轮动 +1、做空二 +2 → 53
    assert len(p.TOOL_SPECS)==53


@pytest.mark.parametrize('name',NAMES)
def test_nonpositive_target_is_invalid_geometry(name):
    data=frame(name)
    if name=='double_top':
        data.iloc[35,2]=40
    else:
        data.iloc[23,2]=40; data.iloc[38,2]=40
    data.iloc[53:,:4]=[30,31,29,30]
    assert signals(name,data)[1][0]['status']=='invalidated_geometry'
    assert signals(name,data)[0][53] is None


def test_long_builders_and_tool_spec_source_byte_unchanged():
    root=Path(__file__).resolve().parents[2]
    old=subprocess.check_output(['git','show','32ae030:backtesting-py/cutie_backtesting_provider.py'],cwd=root,text=True)
    current=(root/'backtesting-py/cutie_backtesting_provider.py').read_text()
    def fragments(source):
        tree=ast.parse(source)
        result={node.name:ast.get_source_segment(source,node) for node in tree.body
                if isinstance(node,ast.FunctionDef) and node.name in (
                    '_build_bottom_pattern','_build_double_bottom','_build_inverse_head_shoulders')}
        spec=next(node.value for node in tree.body if isinstance(node,ast.AnnAssign)
                  and isinstance(node.target,ast.Name) and node.target.id=='TOOL_SPECS')
        for key,value in zip(spec.keys,spec.values):
            if isinstance(key,ast.Constant) and key.value in (
                'local.backtesting_py.double_bottom','local.backtesting_py.inverse_head_shoulders'):
                result[key.value]=ast.get_source_segment(source,value)
        return result
    assert len(fragments(current))==5
    assert fragments(current)==fragments(old)


@pytest.mark.parametrize('name',NAMES)
@pytest.mark.parametrize('key,value',[
    ('position_size_risk_pct',1),('compound',False),('position_size_qty_step',.1),
])
def test_pending_sizing_futures_rejected_before_fetch(monkeypatch,name,key,value):
    monkeypatch.setattr(p,'AUTH_TOKEN','')
    monkeypatch.setattr(p,'_fetch_ohlcv',lambda *a,**kw:pytest.fail('pending sizing fetched main data'))
    monkeypatch.setattr(p,'_fetch_template_warmup',lambda *a,**kw:pytest.fail('pending sizing fetched warmup'))
    assert key not in p.TOOL_SPECS['local.backtesting_py.'+name]['param_schema_properties']
    body=TestClient(p.app).post('/cutie/backtest',json=request(name,{key:value},frame(name))).json()
    assert body['error_type']=='INVALID_PARAMS'
    assert body['raw_report']['position_sizing']['rejections']==[
        {'reason':'position sizing is not wired to this template yet'}]
