"""Group-only 3b subset: real fills, independent scalar targets, HTTP validation."""
import copy
import json
from decimal import Decimal
import pandas as pd
import pytest
from backtesting import Backtest
from fastapi.testclient import TestClient
import cutie_backtesting_provider as p
from test_turtle_template import (TOOL, TRADE_KEYS, fixture, directional_fixture,
                                  frame, request, run, hand, directional_hand)


def assert_disabled_golden(name, direction):
    assert p.TOOL_SPECS['local.backtesting_py.'+name]['runner'] == p.TURTLE_RUNNER
    fx = fixture() if direction == 'long' else directional_fixture(direction)
    before = run(fx)
    after = run(fx, {**fx['params'], 'risk_layer_enabled': False, 'max_holding_bars': 0})
    assert len(after['_trades']) > 0
    for key in ('_trades', '_equity_curve'):
        assert before[key].to_json() == after[key].to_json()
    expected = hand(fx) if direction == 'long' else directional_hand(fx)
    assert expected == fx['expected']
    for (_, t), e in zip(after['_trades'].sort_values(['ExitBar','EntryBar']).iterrows(), expected['trades']):
        assert [t.EntryBar,t.ExitBar,t.EntryPrice,t.ExitPrice,t.Size] == [
            e['entry_bar'],e['exit_bar'],e['entry_price'],e['exit_price'],e['size']]
    def v2(stats):
        return p._build_result_v2(stats_trades=stats['_trades'], equity_scale_dec=Decimal(1),
            fee_bps=Decimal(0), slippage_bps=Decimal(0), initial_capital=Decimal(fx['cash']),
            start_at=int(frame(fx).index[0].timestamp()), end_at=int(frame(fx).index[-1].timestamp())+3600,
            symbol='BTCUSDT', market='spot' if direction=='long' else 'futures', timeframe='1h',
            exchange_id='binance', df=frame(fx))
    assert json.dumps(v2(before), sort_keys=True) == json.dumps(v2(after), sort_keys=True)
    assert 'exit_reason' not in json.dumps(p._build_turtle_groups(after['_trades'],v2(after)['trades']))


def scenario(direction='long', add=False):
    rows = [[100,101,99,100] for _ in range(10)] + [[100,104,99,103]]
    rows += [[104,107,103,106],[108,109,107,108]] + [[108,109,107,108] for _ in range(9)]
    if direction == 'short':
        rows = [[200-o,200-lo,200-hi,200-c] for o,hi,lo,c in rows]
    data = pd.DataFrame(rows, columns=['Open','High','Low','Close'],
        index=pd.date_range('2026-01-01', periods=len(rows), freq='h'), dtype=float).assign(Volume=1)
    params = dict(entry_period=3,exit_period=2,atr_period=2,max_units=2 if add else 1,
                  direction=direction,risk_layer_enabled=True)
    return data,params


def execute(data, params, finalize=False):
    parent = p._build_turtle(params)['strategy']
    class Observed(parent):
        def init(self):
            super().init()
            self.snapshots = {}
        def next(self):
            super().next()
            self.snapshots[len(self.data)-1] = dict(units=len(self.trades),closing=self._closing_group,
                first=self._group_entry_bar,take=self._group_take,orders=len(self.orders))
    return Backtest(data,Observed,cash=100000,exclusive_orders=False,finalize_trades=finalize).run()


@pytest.mark.parametrize('extra',[{}, {'risk_layer_enabled':False},{'max_holding_bars':0},
    {'risk_layer_enabled':True},{'risk_layer_enabled':True,'max_holding_bars':1},
    {'risk_layer_enabled':True,'max_holding_bars':1000000},
    {'risk_layer_enabled':True,'take_profit_pct':.0001},
    {'risk_layer_enabled':True,'take_profit_pct':99.9999}])
def test_group_risk_defaults_and_boundaries(extra):
    cls = p._build_turtle(extra)['strategy']
    assert cls._turtle_risk.get('risk_layer_enabled',False) == extra.get('risk_layer_enabled',False)
    props = p.TOOL_SPECS[TOOL]['param_schema_properties']
    for key in p._TURTLE_RISK_KEYS:
        assert props[key] == p._FIXED_RISK_PARAM_SCHEMA_PROPERTIES[key]


BAD = [{'risk_layer_enabled':v} for v in (0,1,'true',None,[])] + [
    {'risk_layer_enabled':True,'max_holding_bars':v}
    for v in (-1,1000001,True,False,1.,0.,'1',None,float('nan'),float('inf'))] + [
    {'risk_layer_enabled':True,'take_profit_pct':v}
    for v in (-1,0,100,101,True,False,'5',None,float('nan'),float('inf'))] + [
    {'max_holding_bars':1},{'take_profit_pct':5},
    {'risk_layer_enabled':False,'max_holding_bars':1}, {'risk_layer_enabled':False,'take_profit_pct':5}]


@pytest.mark.parametrize('params',BAD)
def test_invalid_group_risk_before_fetch(monkeypatch,params):
    monkeypatch.setattr(p,'AUTH_TOKEN','')
    monkeypatch.setattr(p,'_fetch_ohlcv',lambda *a:pytest.fail('invalid group risk fetched data'))
    monkeypatch.setattr(p,'_fetch_template_warmup',lambda *a:pytest.fail('invalid group risk fetched warmup'))
    result = TestClient(p.app).post('/cutie/backtest',content=json.dumps(request(params)),headers={'Content-Type':'application/json'}).json()
    assert result['error_type'] == 'INVALID_PARAMS'
    with pytest.raises(ValueError,match='INVALID_PARAMS:'):
        p._build_turtle(params)


CONFLICTS = sorted(set(p._FIXED_RISK_PARAM_SCHEMA_PROPERTIES)-set(p._TURTLE_RISK_KEYS))


@pytest.mark.parametrize('key',CONFLICTS)
def test_conflicting_key_rejected_before_fetch_even_default(monkeypatch,key):
    value = p._FIXED_RISK_PARAM_SCHEMA_PROPERTIES[key].get('default',0)
    params = {'risk_layer_enabled':True,key:value}
    monkeypatch.setattr(p,'AUTH_TOKEN','')
    monkeypatch.setattr(p,'_fetch_ohlcv',lambda *a:pytest.fail('conflict validation occurred after fetch'))
    monkeypatch.setattr(p,'_fetch_template_warmup',lambda *a:pytest.fail('conflict validation occurred after warmup'))
    result = TestClient(p.app).post('/cutie/backtest',content=json.dumps(request(params)),headers={'Content-Type':'application/json'}).json()
    assert result['error_type']=='INVALID_PARAMS' and key in result['error_message']
    with pytest.raises(ValueError,match='INVALID_PARAMS:'):
        p._build_turtle(params)


@pytest.mark.parametrize('direction',['long','short','both'])
@pytest.mark.parametrize('n',[1,3,5])
def test_expiry_n_minus_one_n_and_next_open(direction,n):
    data,params = scenario(direction)
    stats = execute(data,{**params,'max_holding_bars':n})
    first = stats['_trades'].query("Tag == 'turtle-1'").sort_values('EntryBar')
    assert first.EntryBar.tolist()==[11] and first.ExitBar.tolist()==[11+n]
    assert first.ExitPrice.tolist()==[data.Open.iloc[11+n]]
    snaps = stats['_strategy'].snapshots
    if n>1:
        assert snaps[11+n-2]['closing'] is False
    assert snaps[11+n-1]['closing'] is True and snaps[11+n]['units']==0
    assert stats['_strategy']._group_exit_reasons['turtle-1']=='time_expiry'


@pytest.mark.parametrize('direction',['long','short','both'])
def test_add_does_not_reset_group_expiry_and_due_prevents_add(direction):
    data,params = scenario(direction,add=True)
    stats = execute(data,{**params,'max_holding_bars':3})
    first = stats['_trades'].query("Tag == 'turtle-1'").sort_values('EntryBar')
    assert first.EntryBar.tolist()==[11,12] and first.ExitBar.tolist()==[14,14]
    assert stats['_strategy'].snapshots[12]['first']==11
    due = execute(data,{**params,'max_holding_bars':1})
    assert due['_trades'].query("Tag == 'turtle-1'").EntryBar.tolist()==[11]
    assert due['_strategy'].snapshots[11]['orders']==1


@pytest.mark.parametrize('direction',['long','short'])
@pytest.mark.parametrize('add',[False,True])
def test_take_profit_scalar_vwap_and_next_open(direction,add):
    data,params = scenario(direction,add)
    if direction=='long':
        data.loc[data.index[12],'High']=110.5
        data.loc[data.index[13],'High']=112
    else:
        data.loc[data.index[12],'Low']=89.5
        data.loc[data.index[13],'Low']=88
    stats = execute(data,{**params,'take_profit_pct':5})
    first = stats['_trades'].query("Tag == 'turtle-1'").sort_values('EntryBar')
    entries = [104,108] if direction=='long' else [96,92]
    if not add:
        entries = entries[:1]
    # Scalar N=(2+5)/2=3.5; q=floor(1000/(2*3.5))=142.
    assert first.EntryPrice.tolist()==entries and first.Size.abs().tolist()==[142]*len(entries)
    vwap = sum(price*142 for price in entries)/(142*len(entries))
    target = Decimal(str(vwap))*(Decimal('1.05') if direction=='long' else Decimal('.95'))
    assert stats['_strategy'].snapshots[12]['take']==target
    exit_bar = 14 if add else 13
    assert first.ExitBar.tolist()==[exit_bar]*len(entries)
    assert first.ExitPrice.tolist()==[data.Open.iloc[exit_bar]]*len(entries)
    assert stats['_strategy']._group_exit_reasons['turtle-1']=='take_profit'
    if add:
        assert stats['_strategy'].snapshots[12]['closing'] is False
        assert stats['_strategy'].snapshots[13]['closing'] is True


@pytest.mark.parametrize('case,reason',[('stop_expiry','stop'),('expiry_take','time_expiry'),
                                       ('take_channel','take_profit')])
@pytest.mark.parametrize('direction',['long','short'])
def test_exit_arbitration(case,reason,direction):
    data,params = scenario(direction)
    params['take_profit_pct']=5
    if case in ('stop_expiry','expiry_take'):
        params['max_holding_bars']=2
    if direction=='long':
        data.loc[data.index[12],'High']=115
        if case=='stop_expiry':
            data.loc[data.index[12],'Low']=96
        elif case=='take_channel':
            data.loc[data.index[12],['Close','Low']]=[100,99]
    else:
        data.loc[data.index[12],'Low']=85
        if case=='stop_expiry':
            data.loc[data.index[12],'High']=104
        elif case=='take_channel':
            data.loc[data.index[12],['Close','High']]=[100,101]
    stats = execute(data,params)
    assert stats['_strategy']._group_exit_reasons['turtle-1']==reason
    assert stats['_trades'].query("Tag == 'turtle-1'").sort_values('EntryBar').ExitBar.tolist()==[13]


@pytest.mark.parametrize('direction',['long','short','both'])
def test_expiry_trigger_and_fill_bars_block_reentry(direction):
    data,params = scenario(direction)
    if direction=='short':
        data.loc[data.index[12],['Open','High','Low','Close']]=[90,91,88,89]
    else:
        data.loc[data.index[12],['Open','High','Low','Close']]=[110,112,109,111]
    stats = execute(data,{**params,'max_holding_bars':1})
    assert stats['_trades'].query("Tag == 'turtle-1'").sort_values('EntryBar').ExitBar.tolist()==[12]
    assert 13 not in set(stats['_trades'].EntryBar)
    assert stats['_strategy'].snapshots[12]==dict(units=0,closing=False,first=11,take=None,orders=0)


def http_result(monkeypatch,tmp_path,data,params):
    monkeypatch.setattr(p,'AUTH_TOKEN','')
    monkeypatch.setattr(p,'_fetch_ohlcv',lambda *a:data.copy())
    monkeypatch.setattr(p,'_fetch_template_warmup',lambda *a:data.iloc[:0].copy())
    monkeypatch.setattr(p,'REPORTS_DIR',tmp_path)
    monkeypatch.setattr(Backtest,'plot',lambda *a,**k:None)
    body=request(params)
    body['backtest'].update(start_at=int(data.index[0].timestamp()),
        end_at=int(data.index[-1].timestamp())+3600,market='futures')
    result=TestClient(p.app).post('/cutie/backtest',json=body).json()
    assert result['result_status']=='success',result
    return result


@pytest.mark.parametrize('reason',['stop','time_expiry','take_profit','channel','end_of_data'])
def test_registered_report_reason_complete_dict_and_signed_keysets(monkeypatch,tmp_path,reason):
    data,params=scenario()
    if reason=='time_expiry':
        params['max_holding_bars']=2
    elif reason=='take_profit':
        params['take_profit_pct']=5
        data.loc[data.index[12],'High']=115
    elif reason=='stop':
        data.loc[data.index[12],['Close','Low']]=[96,95]
    elif reason=='channel':
        data.loc[data.index[13],['Close','Low']]=[102,101]
    # End immediately after the expected fill: full report must contain exactly one group.
    end = {'stop':14,'time_expiry':14,'take_profit':14,'channel':15,'end_of_data':len(data)}[reason]
    data = data.iloc[:end]
    result=http_result(monkeypatch,tmp_path,data,params)
    assert len(result['raw_report']['turtle_groups']) == 1
    assert result['raw_report']['turtle_groups'][0]==dict(
        group_id='turtle-1',trade_seqs=[1],units=1,exit_reason=reason)
    assert all(set(t)==TRADE_KEYS for t in result['trades'])
    assert set(result['metrics'])=={'total_return','max_drawdown','trade_count'}
    risk=result['assumptions']['turtle_risk']
    assert risk['holding_bars_count_from']=='group_first_fill_bar_is_1'
    assert risk['take_profit_basis']=='group_vwap_entry'
    assert 'time_layer' not in result['assumptions']


@pytest.mark.parametrize('direction',['long','short','both'])
def test_enabled_no_rules_preserves_off_response_except_optin_metadata(monkeypatch,tmp_path,direction):
    fx=fixture() if direction=='long' else directional_fixture(direction)
    off=http_result(monkeypatch,tmp_path,frame(fx),fx['params'])
    on=http_result(monkeypatch,tmp_path,frame(fx),{**fx['params'],'risk_layer_enabled':True})
    for key in ('trades','metrics','equity_curve'):
        assert off[key]==on[key]
    assert 'turtle_risk' not in off['assumptions']
    assert [g['exit_reason'] for g in on['raw_report']['turtle_groups']]==['stop','channel','channel']
    groups=copy.deepcopy(on['raw_report']['turtle_groups'])
    for group in groups:
        group.pop('exit_reason')
    assert groups==off['raw_report']['turtle_groups']


@pytest.mark.parametrize('reason',['stop','time_expiry','take_profit'])
def test_last_bar_submission_is_end_of_data_settlement(monkeypatch,tmp_path,reason):
    data,params = scenario()
    data = data.iloc[:13].copy()
    if reason == 'time_expiry':
        params['max_holding_bars'] = 2
    elif reason == 'take_profit':
        params['take_profit_pct'] = 5
        data.loc[data.index[-1],'High'] = 115
    else:
        data.loc[data.index[-1],['Close','Low']] = [96,95]
    result = http_result(monkeypatch,tmp_path,data,params)
    assert result['raw_report']['turtle_groups'] == [dict(
        group_id='turtle-1',trade_seqs=[1],units=1,exit_reason='end_of_data')]


@pytest.mark.parametrize('value',[1,2,20,True,1.0])
def test_turtle_leverage_explicitly_rejected_even_one(monkeypatch,value):
    monkeypatch.setattr(p,'AUTH_TOKEN','')
    monkeypatch.setattr(p,'_fetch_ohlcv',lambda *a:pytest.fail('leverage rejection fetched data'))
    result = TestClient(p.app).post('/cutie/backtest',json=request({'leverage':value})).json()
    assert result['error_type']=='INVALID_PARAMS' and 'leverage' in result['error_message']
