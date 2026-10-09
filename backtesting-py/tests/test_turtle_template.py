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
        dict(group_id='turtle-1',trade_seqs=[1,2,3,4]),
        dict(group_id='turtle-2',trade_seqs=[5]), dict(group_id='turtle-3',trade_seqs=[6])]
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
@pytest.mark.parametrize('market', ['spot', 'futures'])
def test_unavailable_direction_rejected_before_fetch(monkeypatch, direction, market):
    def fail(*a, **kw):
        pytest.fail('fetch must not be called')
    monkeypatch.setattr(provider, '_fetch_ohlcv', fail)
    body=request({'direction': direction});body['backtest']['market']=market
    result=TestClient(provider.app).post('/cutie/backtest', json=body).json()
    assert result['error_type']=='INVALID_PARAMS'
    assert result['error_message']=='turtle short/both is not available yet'


@pytest.mark.parametrize('bad', ['LONG', '', True, None, 1, []])
def test_direction_type_enum(bad):
    with pytest.raises(ValueError, match='^INVALID_PARAMS:'):
        provider._build_turtle({'direction': bad})


def test_schema_runner_and_risk_definition():
    spec=provider.TOOL_SPECS[TOOL]
    props=spec['param_schema_properties']
    assert spec['runner']==provider.TURTLE_RUNNER and spec['exclusive_orders'] is False
    assert not set(props) & (set(provider._FIXED_RISK_PARAM_SCHEMA_PROPERTIES) | set(provider._LEVERAGE_PARAM_SCHEMA_PROPERTIES))
    assert not any(k.startswith('time_') for k in props)
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
        assert all(set(group)=={'group_id','trade_seqs'} for group in groups)
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
        dict(group_id='turtle-1',trade_seqs=[1,2,3,4]),
        dict(group_id='turtle-2',trade_seqs=[5]),dict(group_id='turtle-3',trade_seqs=[6])]
