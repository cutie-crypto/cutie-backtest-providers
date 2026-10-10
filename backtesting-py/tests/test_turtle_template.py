"""Turtle group contract: scalar golden, actual engine fills and registered route."""
from __future__ import annotations
import json
import math
import sys
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from backtesting import Backtest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as provider

TOOL = 'local.backtesting_py.turtle'
FIXTURE = Path(__file__).parent / 'fixtures/turtle_long_golden.json'
CONTRACT = {
    'entry_period': (20, 2, 200), 'exit_period': (10, 1, 200),
    'atr_period': (20, 2, 100), 'unit_risk_pct': (1, 0.0001, 2),
    'add_step_atr': (0.5, 0.1, 2), 'max_units': (4, 1, 4),
    'stop_atr_multiplier': (2, 0.5, 5),
}
TRADE_KEYS = {'seq','opened_at','closed_at','side','qty','entry_price','exit_price','fee','slippage','pnl'}
INT_KEYS = {'entry_period', 'exit_period', 'atr_period', 'max_units'}


def fixture():
    return json.loads(FIXTURE.read_text())


def frame(fx):
    return pd.DataFrame({k: fx[k.lower()+'s'] for k in ('Open', 'High', 'Low', 'Close')},
                        index=pd.date_range('2026-01-01', periods=len(fx['closes']), freq='h')).assign(Volume=1)


def hand(fx):
    """Independent scalar account, next-open all-or-nothing fills, Wilder recurrence.

    No provider functions, pandas indicators or engine state are used. The fixture
    stores this calculation for review; the test recalculates rather than trusting it.
    """
    p = fx['params']
    cash = fx['cash']
    atr, previous, held, trades, groups, skips = None, None, [], [], [], 0
    pending, exit_reason, blocked_bar = None, None, -1
    n, q, last = 0., 0, None
    for i, close in enumerate(fx['closes']):
        op, hi, lo = fx['opens'][i], fx['highs'][i], fx['lows'][i]
        tr = hi-lo if previous is None else max(hi-lo, abs(hi-previous), abs(lo-previous))
        atr = tr if atr is None else (atr*(p['atr_period']-1)+tr)/p['atr_period']
        previous = close
        if exit_reason:
            group = groups[-1]
            group.update(exit_signal=i-1, exit_bar=i, reason=exit_reason)
            for entry in held:
                cash += entry['size']*op
                trades.append({**entry, 'exit_bar': i, 'exit_price': op})
            held, exit_reason, blocked_bar = [], None, i
        if pending:
            size, gid = pending
            if size*op <= cash:
                cash -= size*op
                held.append(dict(entry_bar=i, entry_price=op, size=size, group_id=gid))
                last = op
            else:
                skips += 1
            pending = None
        if i < max(p['entry_period'], p['exit_period'], p['atr_period']) or i == blocked_bar:
            continue
        if held:
            if lo <= last-p['stop_atr_multiplier']*n:
                exit_reason = 'stop'
            elif close < min(fx['lows'][i-p['exit_period']:i]):
                exit_reason = 'channel'
            elif len(held) < p['max_units'] and close >= last+p['add_step_atr']*n:
                pending = q, held[0]['group_id']
        elif close > max(fx['highs'][i-p['entry_period']:i]):
            n = atr
            q = math.floor(cash*p['unit_risk_pct']/100/(p['stop_atr_multiplier']*n)) if n else 0
            if q:
                gid = 'turtle-'+str(len(groups)+1)
                groups.append(dict(group_id=gid, signal=i, n=n, q=q))
                pending = q, gid
    assert not held and not pending and not exit_reason
    return dict(trades=trades, groups=groups, units_skipped=skips)


def run(fx, params=None):
    built = provider.TOOL_SPECS[TOOL]['build'](params or fx['params'])
    return Backtest(frame(fx), built['strategy'], cash=fx['cash'],
                    exclusive_orders=False, finalize_trades=False).run()


def test_scalar_golden_is_independent_and_covers_full_groups():
    fx = fixture()
    assert len(fx['closes']) >= 120
    expected = hand(fx)
    assert expected == fx['expected']
    assert [g['reason'] for g in expected['groups']] == ['stop', 'channel', 'channel']
    assert [sum(t['group_id'] == g['group_id'] for t in expected['trades']) for g in expected['groups']] == [4, 1, 1]
    assert expected['units_skipped'] == 1


def test_golden_engine_times_prices_quantities_and_group_ids():
    fx = fixture()
    stats = run(fx)
    actual = stats['_trades'].sort_values(['ExitBar', 'EntryBar'])
    assert len(actual) == len(fx['expected']['trades'])
    for (_, trade), expected in zip(actual.iterrows(), fx['expected']['trades']):
        for side in ('entry', 'exit'):
            assert trade[side.title()+'Bar'] == expected[side+'_bar']
            assert trade[side.title()+'Time'] == frame(fx).index[expected[side+'_bar']]
            assert trade[side.title()+'Price'] == expected[side+'_price']
        assert trade['Size'] == expected['size']
        assert trade['Tag'] == expected['group_id']
    assert stats['_strategy'].units_skipped == 1
    assert not stats['_strategy'].position
    result = provider._build_result_v2_trades(actual, Decimal('.1'), Decimal(0), Decimal(0))
    assert all(set(t)==TRADE_KEYS for t in result)
    assert provider._build_turtle_groups(actual, result)==[
        dict(group_id='turtle-1',trade_seqs=[1,2,3,4],units=4),
        dict(group_id='turtle-2',trade_seqs=[5],units=1), dict(group_id='turtle-3',trade_seqs=[6],units=1)]
    assert [t['qty'] for t in result] == [provider.canonical_decimal_str(Decimal(t['size'])/10) for t in fx['expected']['trades']]


def test_golden_fill_baseline_one_add_per_bar_frozen_n_and_raised_stop():
    fx = fixture()
    stats = run(fx)
    ts = stats['_trades'].sort_values(['ExitBar', 'EntryBar'])
    first = ts[ts.Tag == 'turtle-1']
    assert list(first.EntryBar) == [16, 18, 19, 21]
    assert list(first.EntryPrice) == [114, 130, 148, 163]
    # Signal 16 close=115 exceeds signal 15 close=106 + .5N, but not fill 114 + .5N.
    assert 17 not in set(first.EntryBar)
    assert len(set(first.EntryBar)) == 4  # huge favorable gaps still add at most one
    assert len(set(first.Size)) == 1  # ATR changes sharply after the first signal
    assert first.ExitBar.tolist() == [23]*4  # low145 hits raised stop146, not initial97
    assert first.Size.tolist() == [117]*4


@pytest.mark.parametrize('key', CONTRACT)
@pytest.mark.parametrize('boundary', [0, 1, 2])
def test_numeric_defaults_min_max(key, boundary):
    val = CONTRACT[key][boundary]
    params = {} if boundary == 0 else {key: val}
    built = provider._build_turtle(params)
    cls = built['strategy']
    assert not issubclass(cls, provider._FixedRiskMixin)
    assert built['min_bars'] == max(params.get('entry_period',20), params.get('exit_period',10), params.get('atr_period',20))+1
    if key == 'unit_risk_pct':
        assert cls._unit_risk == val
    if key == 'max_units':
        assert cls._max_units == val
    if key == 'add_step_atr':
        assert cls._add_step == val
    if key == 'stop_atr_multiplier':
        assert cls._stop_multiple == val


@pytest.mark.parametrize('key', CONTRACT)
@pytest.mark.parametrize('bad_kind', ['below', 'above', 'bool', 'string', 'nan', 'inf', 'none', 'list'])
def test_numeric_invalid(key, bad_kind):
    _, lo, hi = CONTRACT[key]
    bad = dict(below=0 if key=='unit_risk_pct' else lo-1, above=hi+1,
               bool=True, string='2', nan=float('nan'), inf=float('inf'), none=None, list=[])[bad_kind]
    with pytest.raises(ValueError, match='^INVALID_PARAMS:'):
        provider._build_turtle({key: bad})


@pytest.mark.parametrize('key', sorted(INT_KEYS))
@pytest.mark.parametrize('bad', [2.0, 2.5])
def test_integer_keys_reject_all_floats(key, bad):
    with pytest.raises(ValueError, match='^INVALID_PARAMS:'):
        provider._build_turtle({key: bad})


def request(params, tool=TOOL):
    fx=fixture(); data=frame(fx)
    return {'backtest': dict(run_id='turtle_test', provider_tool_id=tool, provider_params=params,
                            symbol='BTCUSDT', market='spot', timeframe='1h',
                            start_at=int(data.index[0].timestamp()), end_at=int(data.index[-1].timestamp())+3600,
                            initial_capital='100000', fee_bps='0', slippage_bps='0')}


@pytest.mark.parametrize('direction', ['short', 'both'])
def test_spot_direction_rejected_before_fetch(monkeypatch, direction):
    def fail(*a, **kw):
        pytest.fail('fetch must not be called')
    monkeypatch.setattr(provider, '_fetch_ohlcv', fail)
    body=request({'direction': direction})
    result=TestClient(provider.app).post('/cutie/backtest', json=body).json()
    assert result['error_type']=='INVALID_PARAMS'
    assert result['error_message']=='Turtle short/both direction requires futures market'


@pytest.mark.parametrize('bad', ['LONG', '', True, None, 1, []])
def test_direction_type_enum(bad):
    with pytest.raises(ValueError, match='^INVALID_PARAMS:'):
        provider._build_turtle({'direction': bad})


def test_schema_runner_and_risk_definition():
    spec=provider.TOOL_SPECS[TOOL]
    props=spec['param_schema_properties']
    assert spec['runner']==provider.TURTLE_RUNNER and spec['exclusive_orders'] is False
    assert set(props) & set(provider._FIXED_RISK_PARAM_SCHEMA_PROPERTIES) == set(provider._TURTLE_RISK_KEYS)
    assert not set(props) & set(provider._LEVERAGE_PARAM_SCHEMA_PROPERTIES)
    # TURTLE-TIME: 只多时间层 9 键（杠杆、FILTER、定量键仍不声明）。
    assert {k for k in props if k.startswith('time_')} == set(provider._TIME_PARAM_SCHEMA_PROPERTIES)
    assert props['direction']==dict(type='string', default='long', enum=['long','short','both'])
    assert props['unit_risk_pct']['description']==provider._TURTLE_RISK_DESCRIPTION
    assert provider._build_turtle({})['executed_name']=='Turtle (20/10/20)'


@pytest.mark.parametrize('tool', [TOOL, 'local.backtesting_py.breakout'])
def test_registered_route_exclusivity_tags_and_assumptions(monkeypatch, tmp_path, tool):
    import backtesting
    captured=[]
    class RecordingBacktest(Backtest):
        def __init__(self, *a, **kw):
            captured.append(kw.copy())
            super().__init__(*a, **kw)
        def plot(self, **kw):
            return None
    monkeypatch.setattr(backtesting, 'Backtest', RecordingBacktest)
    monkeypatch.setattr(provider, 'REPORTS_DIR', tmp_path)
    data=frame(fixture())
    def fetch(exchange, market, symbol, timeframe, start, end):
        return data[(data.index>=pd.Timestamp(start, unit='s')) & (data.index<pd.Timestamp(end, unit='s'))].copy()
    monkeypatch.setattr(provider, '_fetch_ohlcv', fetch)
    params=fixture()['params'] if tool==TOOL else dict(lookback=3, exit_lookback=2)
    result=TestClient(provider.app).post('/cutie/backtest', json=request(params,tool)).json()
    assert result['result_status']=='success', result
    assert captured[0]['exclusive_orders'] is (tool!=TOOL)
    assert captured[0]['cash'] > 100000
    assert result['trades']
    assert all(set(t)==TRADE_KEYS for t in result['trades'])
    if tool==TOOL:
        groups=result['raw_report']['turtle_groups']
        seqs=[seq for group in groups for seq in group['trade_seqs']]
        assert sorted(seqs)==[t['seq'] for t in result['trades']]
        assert len(seqs)==len(set(seqs))
        assert all(set(group)=={'group_id','trade_seqs','units','n','stop'} for group in groups)
        groups=strip_n_stop(groups)
        assert all(group['units']==len(group['trade_seqs']) for group in groups)
        group_by_seq={seq:group['group_id'] for group in groups for seq in group['trade_seqs']}
        assert result['assumptions']['unit_risk_pct_definition']==provider._TURTLE_RISK_DESCRIPTION
        scaled_fx=fixture()
        scaled_fx['cash']=captured[0]['cash']
        expected=hand(scaled_fx)
        assert result['assumptions']['units_skipped']==expected['units_skipped']==1
        scale=Decimal(100000)/Decimal(str(captured[0]['cash']))
        assert len(result['trades'])==len(expected['trades'])
        for actual, trade in zip(result['trades'],expected['trades']):
            assert group_by_seq[actual['seq']]==trade['group_id']
            assert actual['qty']==provider.canonical_decimal_str(Decimal(trade['size'])*scale)
            for side in ('entry','exit'):
                assert Decimal(actual[side+'_price'])==Decimal(str(trade[side+'_price']))
            assert actual['opened_at']==int(data.index[trade['entry_bar']].timestamp())
            assert actual['closed_at']==int(data.index[trade['exit_bar']].timestamp())
    else:
        assert all('group_id' not in t for t in result['trades'])
        assert 'units_skipped' not in result['assumptions']
        assert 'turtle_groups' not in result['raw_report']


def boundary_fixture(close=101):
    closes=[100.]*10+[close, 100.,100.,100.]
    return dict(closes=closes, opens=closes, highs=[101.]*10+[max(close,101),101.,101.,101.],
                lows=[99.]*14, cash=100000,
                params=dict(entry_period=3,exit_period=2,atr_period=2,max_units=1))


def test_channel_equality_does_not_enter():
    stats=run(boundary_fixture())
    assert not stats['_strategy'].position and len(stats['_trades'])==0


def test_channel_strict_breakout_enters_next_open():
    fx=boundary_fixture(101.1);fx['lows'][12]=90
    stats=run(fx)
    assert stats['_trades'].EntryBar.tolist()==[11]
    assert stats['_trades'].EntryPrice.tolist()==[100.]


def test_zero_integer_quantity_does_not_order():
    fx=boundary_fixture(101.1);fx['cash']=101
    stats=run(fx, {**fx['params'],'unit_risk_pct':0.0001})
    assert not stats['_strategy'].position and len(stats['_trades'])==0


def test_first_eligible_signal_bar_is_not_delayed_by_engine_warmup():
    fx=boundary_fixture(101.1)
    for key in ('opens','closes','highs','lows'):
        fx[key]=fx[key][7:]
    fx['lows'][5]=90
    stats=run(fx)
    assert stats['_trades'].EntryBar.tolist()==[4]  # index3 has exactly three prior bars


def test_stop_precedes_add_even_if_close_is_favorable():
    fx=fixture()
    fx['lows'][18]=110  # same bar fills add at130; raised stop113 is touched
    stats=run(fx)
    first=stats['_trades'][stats['_trades'].Tag=='turtle-1']
    assert sorted(first.EntryBar.tolist())==[16,18]
    assert first.ExitBar.tolist()==[19,19]


def test_exit_fill_bar_cannot_start_another_group():
    fx=fixture()
    fx['closes'][23]=180;fx['highs'][23]=181  # breakout on the exit-fill bar
    stats=run(fx)
    assert 24 not in set(stats['_trades'].EntryBar)


def test_atr_is_wilder_first_range_and_causal_with_frozen_group_n():
    fx=fixture()
    full=run(fx)['_strategy']
    alpha=1/fx['params']['atr_period']
    n=fx['highs'][0]-fx['lows'][0]
    assert full.atr[0]==n
    for i in range(1,len(fx['closes'])):
        tr=max(fx['highs'][i]-fx['lows'][i],abs(fx['highs'][i]-fx['closes'][i-1]),abs(fx['lows'][i]-fx['closes'][i-1]))
        n=alpha*tr+(1-alpha)*n
        assert full.atr[i]==pytest.approx(n)
    shorter={k: v[:40] if isinstance(v,list) else v for k,v in fx.items()}
    prefix=run(shorter)['_strategy']
    assert list(prefix.atr)==list(full.atr[:40])


@pytest.mark.parametrize('key', ['stop_loss_pct','risk_layer_enabled','leverage','time_stop_enabled'])
def test_unavailable_layers_are_rejected_before_fetch(monkeypatch,key):
    monkeypatch.setattr(provider, '_fetch_ohlcv', lambda *a: pytest.fail('fetch must not be called'))
    result=TestClient(provider.app).post('/cutie/backtest',json=request({key:1})).json()
    assert result['error_type']=='INVALID_PARAMS'


@pytest.mark.parametrize('bad', ['tag', 'seq', 'coverage', 'time'])
def test_turtle_groups_fail_closed_on_inconsistent_evidence(bad):
    stats=run(fixture())['_trades']
    result=provider._build_result_v2_trades(stats,Decimal(1),Decimal(0),Decimal(0))
    if bad=='tag':
        stats.loc[stats.index[0],'Tag']=None
    elif bad=='seq':
        result[0]['seq']=2
    elif bad=='coverage':
        result.pop()
    else:
        result[0]['opened_at']+=1
    with pytest.raises(ValueError,match='turtle group mapping'):
        provider._build_turtle_groups(stats,result)


def test_turtle_groups_empty_evidence_and_stable_order():
    assert provider._build_turtle_groups(None,[])==[]
    stats=run(fixture())['_trades'].iloc[::-1]
    result=provider._build_result_v2_trades(stats,Decimal(1),Decimal(0),Decimal(0))
    assert provider._build_turtle_groups(stats,result)==[
        dict(group_id='turtle-1',trade_seqs=[1,2,3,4],units=4),
        dict(group_id='turtle-2',trade_seqs=[5],units=1),dict(group_id='turtle-3',trade_seqs=[6],units=1)]


def directional_hand(fx):
    """Scalar 1x margin account, signed PnL, next-open fills and Wilder N.

    Uses only raw OHLC lists and arithmetic, never provider/engine functions.
    An add consumes open-price notional; existing margin and unrealized PnL
    are marked at this bar's close, matching the engine's 1x account boundary.
    """
    p = fx['params']
    cash, atr, previous = fx['cash'], None, None
    held, trades, groups, skipped = [], [], [], 0
    pending, reason, blocked = None, None, -1
    n, q, last, side = 0., 0, None, 0
    for i, close in enumerate(fx['closes']):
        op, hi, lo = (fx[k][i] for k in ('opens', 'highs', 'lows'))
        tr = hi-lo if previous is None else max(hi-lo, abs(hi-previous), abs(lo-previous))
        atr = tr if atr is None else (atr*(p['atr_period']-1)+tr)/p['atr_period']
        previous = close
        if reason:
            groups[-1].update(exit_signal=i-1, exit_bar=i, reason=reason)
            for t in held:
                cash += t['size']*(op-t['entry_price'])
                trades.append({**t, 'exit_bar': i, 'exit_price': op})
            held, reason, blocked = [], None, i
        if pending:
            size, gid = pending
            equity = cash + sum(t['size']*(close-t['entry_price']) for t in held)
            margin = max(0, equity-sum(abs(t['size'])*close for t in held))
            if abs(size)*op <= margin:
                held.append(dict(entry_bar=i, entry_price=op, size=size, group_id=gid))
                last = op
            else:
                skipped += 1
            pending = None
        if i < max(p['entry_period'], p['exit_period'], p['atr_period']) or i == blocked:
            continue
        if held:
            stop = last-side*p['stop_atr_multiplier']*n
            if (lo <= stop if side == 1 else hi >= stop):
                reason = 'stop'
            elif (close < min(fx['lows'][i-p['exit_period']:i]) if side == 1
                  else close > max(fx['highs'][i-p['exit_period']:i])):
                reason = 'channel'
            elif len(held) < p['max_units'] and side*(close-last) >= p['add_step_atr']*n:
                pending = side*q, held[0]['group_id']
        else:
            long = p['direction'] in ('long', 'both') and close > max(fx['highs'][i-p['entry_period']:i])
            short = p['direction'] in ('short', 'both') and close < min(fx['lows'][i-p['entry_period']:i])
            assert not (long and short)
            if long or short:
                n, side = atr, 1 if long else -1
                q = math.floor(cash*p['unit_risk_pct']/100/(p['stop_atr_multiplier']*n)) if n else 0
                if q:
                    gid = 'turtle-'+str(len(groups)+1)
                    groups.append(dict(group_id=gid, signal=i, n=n, q=q, side=side))
                    pending = side*q, gid
    assert not held and not pending and not reason
    return dict(trades=trades, groups=groups, units_skipped=skipped)


def directional_fixture(direction):
    return json.loads((FIXTURE.parent / f'turtle_{direction}_golden.json').read_text())


def strip_n_stop(groups):
    """TURTLE-NSTOP 新增 n / stop 两键；旧口径断言在测试侧剔除后逐字节比，金样不重抓。"""
    return [{k: v for k, v in g.items() if k not in ('n', 'stop')} for g in groups]


def expected_groups(expected):
    return [dict(group_id=g['group_id'],
                 trade_seqs=[i+1 for i,t in enumerate(expected['trades']) if t['group_id']==g['group_id']],
                 units=sum(t['group_id']==g['group_id'] for t in expected['trades']))
            for g in expected['groups']]


@pytest.mark.parametrize('direction', ['short', 'both'])
def test_directional_scalar_golden(direction):
    fx=directional_fixture(direction)
    assert len(fx['closes'])>=120
    assert directional_hand(fx)==fx['expected']
    assert [g['reason'] for g in fx['expected']['groups']]==['stop','channel','channel']
    assert [g['units'] for g in expected_groups(fx['expected'])]==[4,1,1]
    assert fx['expected']['units_skipped']==1
    assert [g['side'] for g in fx['expected']['groups']]==([-1,-1,-1] if direction=='short' else [1,-1,-1])
    assert all(lo<=min(op,c)<=max(op,c)<=hi for op,hi,lo,c in zip(*[fx[k] for k in ('opens','highs','lows','closes')]))


@pytest.mark.parametrize('direction', ['short', 'both'])
def test_directional_engine_golden_and_unique_position_direction(direction):
    fx=directional_fixture(direction);data=frame(fx)
    parent=provider._build_turtle(fx['params'])['strategy']
    seen=[]
    class Observed(parent):
        def sell(self, *a, **kw):
            assert all(t.is_short for t in self.trades), 'sell must never reduce a long group'
            return super().sell(*a, **kw)
        def buy(self, *a, **kw):
            assert all(t.is_long for t in self.trades), 'buy must never reduce a short group'
            return super().buy(*a, **kw)
        def next(self):
            super().next()
            sides={1 if t.is_long else -1 for t in self.trades}
            assert len(sides)<=1
            assert len({t.tag for t in self.trades})<=1
            seen.append((len(self.data)-1, sides))
    stats=Backtest(data,Observed,cash=fx['cash'],exclusive_orders=False,finalize_trades=False).run()
    actual=stats['_trades'].sort_values(['ExitBar','EntryBar'])
    assert len(actual)==len(fx['expected']['trades'])
    for (_,t),e in zip(actual.iterrows(),fx['expected']['trades']):
        assert t.Size==e['size'] and t.Tag==e['group_id']
        for side in ('entry','exit'):
            assert t[side.title()+'Bar']==e[side+'_bar']
            assert t[side.title()+'Time']==data.index[e[side+'_bar']]
            assert t[side.title()+'Price']==e[side+'_price']
    assert stats['_strategy'].units_skipped==1
    assert not stats['_strategy'].position and seen
    result=provider._build_result_v2_trades(actual,Decimal(1),Decimal(0),Decimal(0))
    assert provider._build_turtle_groups(actual,result)==expected_groups(fx['expected'])
    assert all(set(t)==TRADE_KEYS for t in result)
    assert [t['side'] for t in result]==['long' if t['size']>0 else 'short' for t in fx['expected']['trades']]
    assert [t['qty'] for t in result]==[str(abs(t['size'])) for t in fx['expected']['trades']]


@pytest.mark.parametrize('direction', ['short', 'both'])
def test_futures_direction_registered_route(monkeypatch,tmp_path,direction):
    fx=directional_fixture(direction);data=frame(fx)
    monkeypatch.setattr(provider,'_fetch_ohlcv',lambda *a:data.copy())
    monkeypatch.setattr(provider,'_fetch_template_warmup',lambda *a:data.iloc[:0].copy())
    monkeypatch.setattr(provider,'REPORTS_DIR',tmp_path)
    monkeypatch.setattr(Backtest,'plot',lambda *a,**kw:None)
    def forbidden(*a,**kw):
        pytest.fail('Turtle must not build a TimeContext')
    monkeypatch.setattr(provider.TimeContext,'build',forbidden)
    body=request(fx['params']);body['backtest']['market']='futures'
    result=TestClient(provider.app).post('/cutie/backtest',json=body).json()
    assert result['result_status']=='success',result
    assert result['raw_report']['provider_summary'].startswith(f'Turtle {direction.title()} (3/2/2) on ')
    assert 'time_layer' not in result['assumptions']
    assert result['trades']
    engine_cash=max(fx['closes'])*100000
    expected=directional_hand({**fx,'cash':engine_cash})
    scale=Decimal(100000)/Decimal(str(engine_cash))
    assert strip_n_stop(result['raw_report']['turtle_groups'])==expected_groups(expected)
    group_by_seq={seq:g['group_id'] for g in expected_groups(expected) for seq in g['trade_seqs']}
    assert result['assumptions']['units_skipped']==expected['units_skipped']==1
    assert len(result['trades'])==len(expected['trades'])
    for t,e in zip(result['trades'],expected['trades']):
        assert set(t)==TRADE_KEYS and group_by_seq[t['seq']]==e['group_id']
        assert t['qty']==provider.canonical_decimal_str(Decimal(abs(e['size']))*scale)
        assert t['side']==('long' if e['size']>0 else 'short')
        for side in ('entry','exit'):
            assert Decimal(t[side+'_price'])==Decimal(str(e[side+'_price']))
        assert t['opened_at']==int(data.index[e['entry_bar']].timestamp())
        assert t['closed_at']==int(data.index[e['exit_bar']].timestamp())


@pytest.mark.parametrize('direction,name', [('long','Turtle'),('short','Turtle Short'),('both','Turtle Both')])
def test_executed_names(direction,name):
    assert provider._build_turtle({'direction':direction})['executed_name']==name+' (20/10/20)'


def short_boundary(close=99):
    fx=boundary_fixture()
    fx['params']['direction']='short'
    fx['closes'][10]=close
    fx['lows'][10]=min(99,close)
    return fx


def test_short_entry_excludes_current_bar_and_equality_is_not_signal():
    equal=run(short_boundary())
    assert equal['_trades'].empty and not equal['_strategy'].position
    fx=short_boundary(98.9);fx['highs'][12]=110
    assert run(fx)['_trades'].EntryBar.tolist()==[11]


def test_short_stop_uses_high_not_low():
    fx=directional_fixture('short')
    fx['highs'][18]=88  # fill70, stop87: Low54 misses while High88 touches
    first=run(fx)['_trades'].query("Tag=='turtle-1'")
    assert sorted(first.EntryBar.tolist())==[16,18]
    assert first.ExitBar.tolist()==[19,19]
    assert fx['lows'][18]<87<=fx['highs'][18]


def test_short_add_uses_actual_fill_frozen_n_and_one_per_bar():
    first=run(directional_fixture('short'))['_trades'].query("Tag=='turtle-1'").sort_values('EntryBar')
    assert first.EntryBar.tolist()==[16,18,19,21]
    assert first.EntryPrice.tolist()==[86,70,52,37]
    assert first.Size.tolist()==[-117]*4
    assert first.ExitBar.tolist()==[23]*4  # High55 crosses updated stop54


def test_short_exit_channel_excludes_current_high():
    fx=directional_fixture('short')
    second=run(fx)['_trades'].query("Tag=='turtle-2'")
    assert second.ExitBar.tolist()==[54]
    assert fx['closes'][53]>max(fx['highs'][51:53])
    assert fx['closes'][53]<=fx['highs'][53]
    assert fx['highs'][53]<91+2*fx['expected']['groups'][1]['n']


def test_both_reverse_signal_on_exit_fill_bar_is_blocked():
    fx=directional_fixture('both');stats=run(fx)
    assert fx['closes'][23]<min(fx['lows'][20:23])
    assert 24 not in set(stats['_trades'].EntryBar)
    assert 23 not in set(stats['_trades'].EntryBar)
    assert sorted(stats['_trades'].Tag.unique())==['turtle-1','turtle-2','turtle-3']
    assert stats['_trades'].query("Tag=='turtle-2'").EntryBar.tolist()==[51]


def test_both_empty_position_selects_only_one_direction():
    for close,side in [(101.1,1),(98.9,-1)]:
        fx=boundary_fixture(close) if side==1 else short_boundary(close)
        fx['params']['direction']='both'
        fx['lows'][12]=90;fx['highs'][12]=110
        stats=run(fx)
        assert len(stats['_trades'])==1
        assert stats['_trades'].EntryBar.tolist()==[11]
        assert (stats['_trades'].Size.iloc[0]>0)==(side==1)


@pytest.mark.parametrize('direction', ['short','both'])
def test_short_channel_exit_precedes_add(direction):
    fx=directional_fixture('short')
    fx['params'].update(direction=direction,exit_period=1)
    # N=8.5 freezes on signal15. First fill86, favorable close80 queues add.
    # Add gaps up to95; close89 is favorable by6 (>4.25), but also exceeds
    # prior High85. Channel exit must close both units rather than queue a third.
    for i,ohlc in {16:(86,87,84,85),17:(85,85,79,80),18:(95,96,88,89)}.items():
        for k,v in zip(('opens','highs','lows','closes'),ohlc): fx[k][i]=v
    first=run(fx)['_trades'].query("Tag=='turtle-1'")
    assert sorted(first.EntryBar.tolist())==[16,18]
    assert first.ExitBar.tolist()==[19,19]
