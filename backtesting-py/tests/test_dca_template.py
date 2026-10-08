"""R3-T3 DCA: hand-counted golden, independent Fraction oracle and HTTP evidence."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from decimal import Context, Decimal, localcontext
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as provider
from scale_in_out_ledger import LedgerBar, run_scale_in_out

D = Decimal
TOOL = 'local.backtesting_py.dca'
HOUR = 3600
START = 1735509600  # Sunday 2024-12-29 22:00 UTC
CASES = json.loads((Path(__file__).parent/'fixtures/dca_golden.json').read_text())['cases']
BASE = {'amount':100}


def _decimal(value):
    if value is None:
        return None
    with localcontext(Context(prec=60)):
        number = D(value.numerator)/D(value.denominator)
        return format(number.normalize(), 'f')


def _cash_decimal(value):
    """Independent positive average-cost half-up rounding to eight places."""
    if value is None:
        return None
    units = (value*10**8+Fraction(1,2)).__floor__()
    return _decimal(Fraction(units,10**8))


def _times(case):
    return case.get('open_times', [case['start_at']+i*case['step'] for i in range(len(case['closes']))])


def _bars(case):
    return [LedgerBar(t, t+case['step'], D(str(o)), D(str(c)))
            for t,o,c in zip(_times(case),case['opens'],case['closes'])]


def _reference(case):
    """Pure rational loop: owns money, positions and scheduling; no production build.

    Cash/qty quantization uses integer arithmetic. Every entry/exit and close
    snapshot is independently evaluated before the next close signal is chosen.
    """
    p, times = case['params'], _times(case)
    amount = Fraction(str(p['amount']))
    dip_amount = amount*Fraction(str(p.get('dip_multiplier',1.5)))
    dip = Fraction(str(p.get('dip_pct',5)))/100
    profit = Fraction(str(p.get('profit_target_pct',10)))/100
    max_adds = p.get('max_dip_adds',3)
    cash = Fraction(case['initial_capital'])
    fee, slip = Fraction(case['fee_bps'])/10000, Fraction(case['slippage_bps'])/10000
    qty, notional, last_price, adds, last_avg = Fraction(0),Fraction(0),None,0,None
    invested, skipped, rounds, dip_fills = Fraction(0),0,0,0
    actions, fills, attempts, trades, snapshots, lots = [],[],[],[],[],[]
    pending = None
    def rounded(v):
        return Fraction((v*10**8+Fraction(1,2)).__floor__(),10**8)
    def period(ts):
        utc = datetime.fromtimestamp(ts, timezone.utc)
        return utc.date() if p.get('interval','daily')=='daily' else utc.isocalendar()[:2]
    def liquidate(i, price):
        nonlocal cash
        for opened, entry, q, costs in lots:
            exit_costs = rounded(q*price*fee)+rounded(q*price*slip)
            pnl = q*(price-entry)-costs-exit_costs
            trades.append((opened,i,q,entry,price,pnl))
            cash += q*price-exit_costs
        lots.clear()
    for i,(o,c) in enumerate(zip(case['opens'],case['closes'])):
        price, close = Fraction(str(o)), Fraction(str(c))
        if pending:
            kind, money, is_dip = pending
            filled = False
            if kind=='buy':
                q = Fraction((money/price*10**12).__floor__(),10**12)
                gross = q*price
                costs = rounded(gross*fee)+rounded(gross*slip)
                if q and gross+costs <= cash:
                    filled = True
                    cash -= gross+costs
                    lots.append((i,price,q,costs))
                    qty, notional, invested = qty+q,notional+gross,invested+gross
                    last_price = price
                    dip_fills += int(is_dip)
                else:
                    skipped += 1
            elif lots:
                filled = True
                last_avg = notional/qty
                liquidate(i,price)
                qty, notional, last_price, adds = Fraction(0),Fraction(0),None,0
                rounds += 1
            attempts.append((i,kind,filled,price))
            if filled:
                fills.append(dict(bar=i,action=kind,avg_cost=_cash_decimal(notional/qty if qty else last_avg)))
        unrealized = sum((q*(close-entry)-costs for _,entry,q,costs in lots), Fraction(0))
        snapshots.append((cash,qty,unrealized))
        pending = None
        if i+1==len(times):
            continue
        if qty and close >= notional/qty*(1+profit):
            pending = ('sell_all',None,False)
        elif qty and adds < max_adds and close <= last_price*(1-dip):
            adds += 1
            pending = ('buy',dip_amount,True)
        elif period(times[i+1]) != period(times[i]):
            pending = ('buy',amount,False)
        if pending:
            kind,money,_ = pending
            actions.append(dict(bar=i,action=kind,amount=_decimal(money)))
    avg = notional/qty if qty else last_avg
    liquidate(len(times),Fraction(str(case['closes'][-1])))
    return dict(actions=actions,fills=fills,attempts=attempts,trades=trades,snapshots=snapshots,
                skipped=skipped,rounds=rounds,dip_fills=dip_fills,avg_cost=_cash_decimal(avg),
                total_invested=invested,max_loss=min([Fraction(0),*(x[2] for x in snapshots)]),cash=cash)


def _run(case):
    bars = _bars(case)
    config = provider._build_dca(case['params'])['scale_in_out']
    signal,on_fill = config['signal_factory'](bars)
    actions,fills,attempts = [],[],[]
    display_ledger = SimpleNamespace(total_invested=D(0),max_unrealized_loss=D(0))
    def trace_signal(i):
        raw = signal(i)
        if raw != 'hold':
            kind = raw[0] if isinstance(raw,tuple) else raw
            actions.append(dict(bar=i,action=kind,amount=format(raw[1].normalize(),'f') if kind=='buy' else None))
        return raw
    def trace_fill(i,kind,filled,price):
        on_fill(i,kind,filled,price)
        attempts.append((i,kind,filled,price))
        if filled:
            fills.append(dict(bar=i,action=kind,avg_cost=config['extra_assumptions'](display_ledger)['avg_cost']))
    result = run_scale_in_out(bars,trace_signal,initial_capital=D(case['initial_capital']),
                             buy_notional=config['buy_notional'],sell_notional=config['sell_notional'],
                             fee_bps=D(case['fee_bps']),slippage_bps=D(case['slippage_bps']),
                             start_at=bars[0].open_time,end_at=bars[-1].close_time,
                             lot_order=config['lot_order'],on_fill=trace_fill)
    return result,config['extra_assumptions'](result),actions,fills,attempts


@pytest.mark.parametrize('case',CASES,ids=lambda c:c['name'])
def test_hand_counted_golden_and_next_open_fills(case):
    assert len(case['closes']) >= 40 and case['timeframe']=='1h'
    expected = _reference(case)
    assert expected['actions'] == case['expected_actions']
    assert expected['fills'] == case['expected_fills']
    assert len(expected['trades']) == case['expected_trades']
    assert expected['rounds'] == case['expected_rounds_completed']
    assert expected['dip_fills'] == case['expected_dip_adds_total']
    assert expected['skipped'] == case['expected_skipped_buys']
    result,a,actions,fills,attempts = _run(case)
    assert actions == expected['actions']
    assert fills == expected['fills']
    assert [(i,k,f,Fraction(price)) for i,k,f,price in attempts] == expected['attempts']
    assert [i for i,_,_,_ in attempts] == [x['bar']+1 for x in actions]
    assert all(price == D(str(case['opens'][i])) for i,_,_,price in attempts)
    times = _times(case)
    assert len(result.trades) == len(expected['trades'])
    for got,(opened,closed,qty,entry,exit_,pnl) in zip(result.trades,expected['trades']):
        assert got['opened_at']==times[opened]
        assert got['closed_at']==(times[closed] if closed<len(times) else times[-1]+case['step'])
        assert tuple(Fraction(got[k]) for k in ('qty','entry_price','exit_price','pnl'))==(qty,entry,exit_,pnl)
    for got,(cash,qty,unrealized) in zip(result.snapshots,expected['snapshots']):
        assert tuple(Fraction(x) for x in (got.cash,got.lot_qty_sum,got.unrealized_pnl))==(cash,qty,unrealized)
    assert result.skipped_buys_insufficient_cash == expected['skipped']
    assert Fraction(result.total_invested)==expected['total_invested']
    assert Fraction(result.max_unrealized_loss)==expected['max_loss']
    assert Fraction(result.final_cash)==expected['cash']
    assert a['position_mode']=='dca'
    assert a['avg_cost']==expected['avg_cost']
    if case['name']!='cash_skips':
        assert case['opens']==[case['closes'][0],*case['closes'][:-1]]
        assert a['avg_cost']==case['expected_final_avg_cost']
        assert Fraction(a['total_invested'])==Fraction(case['expected_total_invested'])
        first_close=result.trades[0]['closed_at']
        first=[t for t in result.trades if t['closed_at']==first_close]
        assert len(first)==4 and all(t['pnl']>0 for t in first)
        assert any(left['entry_price']!=right['entry_price'] for left,right in zip(first,first[1:]))
        assert [dict(opened_at=t['opened_at'],closed_at=t['closed_at'],qty=_decimal(Fraction(t['qty'])),
                     entry=_decimal(Fraction(t['entry_price'])),exit=_decimal(Fraction(t['exit_price'])),
                     pnl=_decimal(Fraction(t['pnl']))) for t in first]==case['expected_first_profit_trades']
        if case['name']=='daily':
            # An exact 10% single-lot exit, dip/calendar collision, and profit/calendar collision.
            assert case['closes'][26]==110
            assert next(x for x in actions if x['bar']==26)['action']=='sell_all'
            assert next(x for x in actions if x['bar']==73)==dict(bar=73,action='buy',amount='150')
            assert next(x for x in actions if x['bar']==97)['action']=='sell_all'
            assert all(datetime.fromtimestamp(times[i],timezone.utc).hour==23 for i in (73,97))
    assert a['rounds_completed']==expected['rounds']
    assert a['dip_adds_total']==expected['dip_fills']
    assert Fraction(a['total_invested'])==expected['total_invested']
    assert Fraction(a['max_unrealized_loss'])==expected['max_loss']
    assert all(t['closed_at']==times[-1]+case['step'] for t in result.trades[-case.get('expected_final_lots',4 if case['name']!='cash_skips' else 1):])


@pytest.fixture()
def client(monkeypatch,tmp_path):
    monkeypatch.setattr(provider,'REPORTS_DIR',tmp_path/'reports')
    return TestClient(provider.app)


def _fetch(case):
    times = [case['start_at']-2*case['step'],case['start_at']-case['step'],*_times(case)]
    opens,closes = [100,100,*case['opens']],[100,100,*case['closes']]
    frame = pd.DataFrame(dict(Open=opens,Close=closes,
                              High=[max(o,c)+1 for o,c in zip(opens,closes)],
                              Low=[min(o,c)-1 for o,c in zip(opens,closes)],Volume=[10]*len(times)),
                         index=pd.to_datetime(times,unit='s'))
    def fetch(exchange,market,symbol,timeframe,start,end):
        return frame.loc[(frame.index >= pd.to_datetime(start,unit='s')) &
                         (frame.index < pd.to_datetime(end,unit='s'))].copy()
    return fetch


def _post(client,case,*,params=None,market='spot',extra=None):
    payload = dict(run_id='dca_test',provider_tool_id=TOOL,
                   provider_params={'exchange':'binance',**(case['params'] if params is None else params)},
                   symbol='BTCUSDT',market=market,timeframe=case['timeframe'],start_at=case['start_at'],
                   end_at=_times(case)[-1]+case['step'],initial_capital=case['initial_capital'],
                   fee_bps=case['fee_bps'],slippage_bps=case['slippage_bps'],**(extra or {}))
    response = client.post('/cutie/backtest',json={'backtest':payload})
    assert response.status_code==200
    return response.json()


@pytest.mark.parametrize('case',CASES,ids=lambda c:c['name'])
def test_http_assumptions_match_ledger(client,monkeypatch,case):
    monkeypatch.setattr(provider,'_fetch_ohlcv',_fetch(case))
    body = _post(client,case)
    assert body['result_status']=='success',body
    result,a,_,_,_ = _run(case)
    assert {k:body['assumptions'][k] for k in a}==a
    assert body['assumptions']['buy_fills']==result.buy_fills
    assert body['assumptions']['sell_fills']==result.sell_fills
    assert body['assumptions']['skipped_buys_insufficient_cash']==case['expected_skipped_buys']
    assert body['metrics']['trade_count']==case['expected_trades']
    assert len(body['trades'])==len(result.trades)
    for got,expected in zip(body['trades'],sorted(result.trades,key=lambda t:(t['closed_at'],t['opened_at']))):
        assert (got['opened_at'],got['closed_at'])==(expected['opened_at'],expected['closed_at'])
        assert all(D(got[k])==expected[k] for k in ('qty','entry_price','exit_price','fee','slippage','pnl'))
    assert D(body['equity_curve'][-1]['equity'])==result.final_cash
    assert body['equity_curve'][-1]['ts']==_times(case)[-1]+case['step']
    assert body['assumptions']['indicator_warmup_bars']==2


def test_two_parameter_changes_change_trade_counts(client,monkeypatch):
    case = CASES[0]
    monkeypatch.setattr(provider,'_fetch_ohlcv',_fetch(case))
    counts=[]
    for params in ({'amount':100},{'amount':100,'max_dip_adds':1},{'amount':100,'max_dip_adds':0}):
        body=_post(client,case,params=params)
        assert body['result_status']=='success',body
        counts.append(body['metrics']['trade_count'])
    assert counts==[13,7,5]


def _case(closes,*,opens=None,params=None,capital='10000',times=None,start=START,step=HOUR):
    return dict(start_at=start,step=step,timeframe='1h',initial_capital=capital,fee_bps='10',slippage_bps='5',
                params={**BASE,**(params or {})},opens=opens or [100]*len(closes),closes=closes,
                **({'open_times':times} if times is not None else {}))


def test_profit_target_equality_exits():
    case=_case([100,100,110,100,100])
    result,a,actions,_,_=_run(case)
    assert actions==[dict(bar=1,action='buy',amount='100'),dict(bar=2,action='sell_all',amount=None)]
    assert result.sell_fills==a['rounds_completed']==1
    assert a['avg_cost']=='100'


def test_completed_round_resets_dip_attempt_budget():
    closes=[100]*30
    for i,c in {2:95,3:110,26:95}.items():closes[i]=c
    case=_case(closes,params={'max_dip_adds':1})
    _,a,actions,_,_=_run(case)
    assert actions==[dict(bar=i,action=k,amount=money) for i,k,money in
                     [(1,'buy','100'),(2,'buy','150'),(3,'sell_all',None),(25,'buy','100'),(26,'buy','150')]]
    assert a['dip_adds_total']==2 and a['rounds_completed']==1


def test_utc_midnight_boundary_and_no_initial_buy():
    case=_case([100]*5,params={'max_dip_adds':0})
    _,_,actions,fills,_=_run(case)
    assert actions==[dict(bar=1,action='buy',amount='100')]
    assert fills[0]['bar']==2
    assert datetime.fromtimestamp(_times(case)[2],timezone.utc).hour==0


def test_missing_whole_days_create_only_one_scheduled_buy():
    times=[START,START+HOUR,START+74*HOUR,START+75*HOUR]
    _,_,actions,fills,_=_run(_case([100]*4,times=times))
    assert actions==[dict(bar=1,action='buy',amount='100')]
    assert [f['bar'] for f in fills]==[2]


def test_weekly_uses_iso_year_and_week_not_calendar_year():
    start=int(datetime(2024,12,31,23,tzinfo=timezone.utc).timestamp())
    result,a,actions,_,_=_run(_case([100]*4,params={'interval':'weekly'},start=start))
    assert actions==[] and result.trades==[] and a['avg_cost'] is None


def test_zero_dip_budget_never_adds_and_multiplier_one_uses_base_amount():
    closes=[100,100,95,90,80,70,100]
    _,a,actions,_,_=_run(_case(closes,params={'max_dip_adds':0}))
    assert actions==[dict(bar=1,action='buy',amount='100')] and a['dip_adds_total']==0
    _,a,actions,_,_=_run(_case(closes,params={'dip_multiplier':1}))
    assert [x['amount'] for x in actions]==['100']*4
    assert a['dip_adds_total']==3


def test_failed_dip_keeps_last_fill_price_and_consumes_attempt():
    case=_case([100,100,95,99,95,95,80,80],opens=[100,100,100,200,100,100,100,100],
               capital='150',params={'max_dip_adds':2})
    result,a,actions,_,_=_run(case)
    assert [x['bar'] for x in actions]==[1,2,4]
    assert result.skipped_buys_insufficient_cash==2
    assert a['dip_adds_total']==0 and a['avg_cost']=='100'


def test_weighted_average_uses_quantized_filled_qty_without_fees():
    case=_case([100,100,95,80,80,80],opens=[100,100,100,75,100,100],params={'max_dip_adds':1})
    result,a,_,fills,_=_run(case)
    expected=_reference(case)
    assert a['avg_cost']=='83.33333333'==expected['avg_cost']
    assert fills[-1]['avg_cost']==a['avg_cost']
    # 91.666666665 exceeds the rounded display target 83.33333333 * 1.1,
    # but remains below the true 250/3 * 1.1: it must not trigger profit taking.
    rounding_case=_case([100,100,95,80,91.666666665,80],opens=[100,100,100,75,100,100],
                        params={'max_dip_adds':1})
    _,rounded_a,rounding_actions,_,_=_run(rounding_case)
    assert rounded_a['avg_cost']=='83.33333333'
    assert [x['action'] for x in rounding_actions]==['buy','buy']
    assert rounded_a['rounds_completed']==0
    assert a['total_invested']=='250'
    assert result.trades[0]['entry_price']==D(100) and result.trades[1]['entry_price']==D(75)
    case=_case([100,100,95,80,80,80],opens=[100,100,100,79,100,100],params={'max_dip_adds':1})
    result,a,_,_,_=_run(case)
    expected=_reference(case)
    assert a['avg_cost']==expected['avg_cost']
    assert Fraction(a['total_invested'])==expected['total_invested'] < 250


def test_never_filled_has_null_cost_and_no_phantom_dips():
    result,a,actions,_,_=_run(_case([100,100,95,95,95],capital='1'))
    assert actions==[dict(bar=1,action='buy',amount='100')]
    assert result.skipped_buys_insufficient_cash==1
    assert a['avg_cost'] is None and a['dip_adds_total']==a['rounds_completed']==0


def test_daily_on_weekly_candles_buys_each_next_bar():
    case=_case([100]*5,step=7*24*HOUR,params={'max_dip_adds':0})
    result,_,actions,_,_=_run(case)
    assert [x['bar'] for x in actions]==[0,1,2,3]
    assert result.buy_fills==4
    assert result.trades[0]['opened_at']==START+7*24*HOUR


def test_dip_notional_uses_ledger_cash_rounding_only_on_inexact_product():
    amount='100.'+'1234567890'*5+'123456789'
    case=_case([100,100,95,95],params={'amount':amount,'dip_multiplier':'1.1234567890123456789'})
    config=provider._build_dca(case['params'])['scale_in_out']
    bars=_bars(case)
    signal,on_fill=config['signal_factory'](bars)
    assert signal(1)==('buy',D(amount))
    on_fill(2,'buy',True,D(100))
    raw=signal(2)
    product=Fraction(amount)*Fraction(case['params']['dip_multiplier'])
    rounded=Fraction((product*10**8+Fraction(1,2)).__floor__(),10**8)
    assert raw==('buy',D(_decimal(rounded)))


@pytest.mark.parametrize('bad',[
    {'amount':None},{'amount':0},{'amount':-1},{'amount':True},{'amount':'NaN'},{'amount':'Infinity'},
    {'interval':'monthly'},{'dip_pct':0.5},{'dip_pct':21},{'profit_target_pct':0},{'profit_target_pct':101},
    {'dip_multiplier':0.9},{'dip_multiplier':3.1},{'max_dip_adds':-1},{'max_dip_adds':11},
    {'max_dip_adds':1.5},{'max_dip_adds':True},
])
def test_invalid_params(bad):
    params={k:v for k,v in {**BASE,**bad}.items() if v is not None}
    with pytest.raises(ValueError,match='INVALID_PARAMS'):
        provider._build_dca(params)


@pytest.mark.parametrize('bad',[
    {'amount':None},{'amount':0},{'amount':True},{'interval':'monthly'},
    {'dip_pct':0.5},{'dip_pct':21},{'profit_target_pct':0},{'profit_target_pct':101},
    {'max_dip_adds':-1},{'max_dip_adds':11},{'dip_multiplier':3.1},
])
def test_invalid_params_over_http(client,monkeypatch,bad):
    monkeypatch.setattr(provider,'_fetch_ohlcv',_fetch(CASES[0]))
    params={k:v for k,v in {**BASE,**bad}.items() if v is not None}
    body=_post(client,CASES[0],params=params)
    assert (body['result_status'],body['error_type'])==('failed','INVALID_PARAMS')


@pytest.mark.parametrize('market,params,extra,needle',[
    ('futures',BASE,{},'spot market only'),
    ('spot',{**BASE,'stop_loss_pct':5},{},'stop_loss_pct'),
    ('spot',{**BASE,'take_profit_pct':10},{},'take_profit_pct'),
    ('spot',{**BASE,'position_size_notional':500},{},'position_size_notional'),
    ('spot',BASE,{'risk_policy':{'schema':'x'}},'risk_policy'),
    ('spot',BASE,{'signal_execution':{'schema':'x'}},'signal_execution'),
])
def test_fixed_risk_and_futures_rejected(client,monkeypatch,market,params,extra,needle):
    monkeypatch.setattr(provider,'_fetch_ohlcv',_fetch(CASES[0]))
    body=_post(client,CASES[0],market=market,params=params,extra=extra)
    assert (body['result_status'],body['error_type'])==('failed','INVALID_PARAMS')
    assert needle in body['error_message']


def test_catalog_schema_and_runner():
    spec=provider.TOOL_SPECS[TOOL]
    entry=provider._catalog_tool(TOOL,spec,['BTCUSDT'])
    assert spec['runner']==provider.SCALE_IN_OUT_RUNNER
    assert spec['markets']==entry['markets']==['spot']
    assert entry['param_schema']['properties']=={
        'interval':{'type':'string','default':'daily','enum':['daily','weekly']},
        'amount':{'type':'number','minimum':0},
        'dip_pct':{'type':'number','default':5,'minimum':1,'maximum':20},
        'dip_multiplier':{'type':'number','default':1.5,'minimum':1,'maximum':3},
        'max_dip_adds':{'type':'integer','default':3,'minimum':0,'maximum':10},
        'profit_target_pct':{'type':'number','default':10,'minimum':1,'maximum':100},
        'exchange':{'type':'string','default':provider.DEFAULT_EXCHANGE},
    }
    assert not set(entry['param_schema']['properties']) & set(provider._FIXED_RISK_PARAM_SCHEMA_PROPERTIES)
    assert spec['description'].endswith("maps to KOL '定投 + 跌幅加码'")
    assert 'next bar open' in spec['description'] and '1w' in spec['description']
    assert 'first calendar boundary' in spec['description']
    assert entry['timeframes']==provider.CATALOG_TIMEFRAMES_EXCHANGE
    built=provider._build_dca(BASE)
    assert built['min_bars']==2 and built['scale_in_out']['lot_order']=='fifo'
    assert built['scale_in_out']['buy_notional']==built['scale_in_out']['sell_notional']==D(100)


def test_explicit_profit_target_changes_exit_and_is_reported(client,monkeypatch):
    case=_case([100,100,110,100,100])
    monkeypatch.setattr(provider,'_fetch_ohlcv',_fetch(case))
    default=_post(client,case)
    changed=_post(client,case,params={**BASE,'profit_target_pct':20})
    assert default['result_status']==changed['result_status']=='success'
    assert default['assumptions']['profit_target_pct']=='10'
    assert changed['assumptions']['profit_target_pct']=='20'
    assert default['assumptions']['rounds_completed']==1
    assert changed['assumptions']['rounds_completed']==0
    assert default['trades'][0]['closed_at']==START+3*HOUR
    assert changed['trades'][0]['closed_at']==START+5*HOUR


def test_profit_exit_wins_when_profit_and_dip_both_match():
    # At bar 3: avg=250/1.75, target ~157.14; last fill=200, dip=190.
    # Close 180 satisfies both, so the entire round exits rather than adding.
    case=_case([100,100,95,180,100],opens=[100,100,100,200,100])
    _,a,actions,_,_=_run(case)
    assert [x['action'] for x in actions]==['buy','buy','sell_all']
    assert a['rounds_completed']==1 and a['dip_adds_total']==1
