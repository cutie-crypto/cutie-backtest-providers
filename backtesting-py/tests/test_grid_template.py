"""R3-T2 grid: hand-counted fixtures, independent Fraction oracle, HTTP and mutations."""
from __future__ import annotations

import inspect
import json
import sys
from decimal import Context, Decimal, Inexact, localcontext
from fractions import Fraction
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as provider
from scale_in_out_ledger import LedgerBar, round_cash, run_scale_in_out

D = Decimal
TOOL = 'local.backtesting_py.grid'
START = 1735689600
DAY = 86400
FIXTURE = json.loads((Path(__file__).parent / 'fixtures/grid_golden.json').read_text())
CASES = FIXTURE['cases']
BASE = dict(lower_price=100, upper_price=150, grid_count=5, amount_per_grid=100)


def _bars(case):
    return [LedgerBar(START+i*DAY, START+(i+1)*DAY, D(str(o)), D(str(c)))
            for i, (o, c) in enumerate(zip(case['opens'], case['closes']))]


def _reference(case):
    """Independent loop: exact rational levels, money and LIFO lots; no provider build.

    Fixture geometric lines are hand-computed powers of two, independently verified
    by L_i ** n == lower ** (n-i) * upper ** i (no approximate root in this oracle).
    """
    p = case['params']
    n = p.get('grid_count', 20)
    lo, hi = Fraction(str(p['lower_price'])), Fraction(str(p['upper_price']))
    if p.get('grid_mode', 'arithmetic') == 'arithmetic':
        lines = [lo+(hi-lo)*i/n for i in range(n+1)]
    else:
        lines = list(map(Fraction, case['expected_grid_lines']))
        assert all(v**n == lo**(n-i)*hi**i for i, v in enumerate(lines))
    levels = [(-1 if Fraction(str(c)) < lo else n+1 if Fraction(str(c)) > hi else
               sum(Fraction(str(c)) >= line for line in lines)-1) for c in case['closes']]
    cash = Fraction(case['initial_capital'])
    amount = Fraction(str(p['amount_per_grid']))
    fee, slip = Fraction(case['fee_bps'])/10000, Fraction(case['slippage_bps'])/10000
    lots, trades, actions, fills, losses = [], [], [], [], []
    pending, ref, reset, skipped = None, levels[0], False, 0
    def rounded(value):
        # Positive fees/slippage, half-up to ledger's eight decimal cash places.
        return Fraction((value*10**8+Fraction(1, 2)).numerator //
                        (value*10**8+Fraction(1, 2)).denominator, 10**8)
    for i, (o, c, lv) in enumerate(zip(case['opens'], case['closes'], levels)):
        price = Fraction(str(o))
        if pending == 'buy':
            qty = Fraction((amount/price*10**12).__floor__(), 10**12)
            notional = qty*price
            cost = notional + rounded(notional*fee) + rounded(notional*slip)
            if cash >= cost and qty:
                cash -= cost
                lots.append((i, price, qty, rounded(notional*fee)+rounded(notional*slip)))
                fills.append((i, 'buy'))
            else:
                skipped += 1
        elif pending in ('sell_lot', 'sell_all') and lots:
            fills.append((i, pending))
            for _ in range(1 if pending == 'sell_lot' else len(lots)):
                opened, entry, qty, costs = lots.pop()
                exit_costs = rounded(qty*price*fee)+rounded(qty*price*slip)
                pnl = qty*(price-entry)-costs-exit_costs
                cash += qty*price-exit_costs
                trades.append((opened, i, qty, entry, price, pnl))
        losses.append(sum((q*(Fraction(str(c))-e)-cost for _, e, q, cost in lots), Fraction(0)))
        pending = None
        if i == 0 or i == len(levels)-1:
            continue
        if lv == -1:
            if p.get('below_lower_action', 'pause') == 'stop_loss' and not reset:
                reset = True
                if lots:
                    pending = 'sell_all'
        elif reset and 0 <= lv <= n:
            ref, reset = lv, False
        elif 0 <= lv <= n and lv < ref:
            pending, ref = 'buy', ref-1
        elif lv > ref:
            if lots:
                pending, ref = 'sell_lot', ref+1
            else:
                ref = min(lv, n)
        if pending:
            actions.append(dict(bar=i, action=pending, amount=str(p['amount_per_grid']) if pending == 'buy' else None))
    for opened, entry, qty, costs in reversed(lots):
        price = Fraction(str(case['closes'][-1]))
        exit_costs = rounded(qty*price*fee)+rounded(qty*price*slip)
        trades.append((opened, len(levels), qty, entry, price, qty*(price-entry)-costs-exit_costs))
    return levels, actions, fills, trades, skipped, min([Fraction(0), *losses])


def _run(case, *, params=None):
    bars = _bars(case)
    built = provider._build_grid(case['params'] if params is None else params)
    config = built['scale_in_out']
    signal, on_fill = config['signal_factory'](bars)
    actions, fills = [], []
    def traced_signal(i):
        raw = signal(i)
        if raw != 'hold':
            action = raw[0] if isinstance(raw, tuple) else raw
            actions.append(dict(bar=i, action=action, amount=str(raw[1]) if action == 'buy' else None))
        return raw
    def traced_fill(i, action, filled, price):
        fills.append((i, action, filled, price))
        on_fill(i, action, filled, price)
    result = run_scale_in_out(bars, traced_signal, initial_capital=D(case['initial_capital']),
                             buy_notional=config['buy_notional'], sell_notional=config['sell_notional'],
                             fee_bps=D(case['fee_bps']), slippage_bps=D(case['slippage_bps']),
                             start_at=START, end_at=bars[-1].close_time,
                             lot_order=config['lot_order'], on_fill=traced_fill)
    return result, config['extra_assumptions'](result), actions, fills


@pytest.mark.parametrize('case', CASES, ids=lambda c: c['name'])
def test_hand_counted_golden_and_next_open_fills(case):
    assert len(case['closes']) >= 40
    levels, actions, expected_fills, trades, skipped, loss = _reference(case)
    assert levels == case['expected_levels']
    assert actions == case['expected_actions']
    assert len(trades) == case['expected_trades']
    config = provider._build_grid(case['params'])['scale_in_out']
    level = inspect.getclosurevars(config['signal_factory']).nonlocals['level']
    assert [level(D(str(c))) for c in case['closes']] == levels
    result, a, actual_actions, fills = _run(case)
    assert actual_actions == actions
    assert [(i, action) for i, action, filled, _ in fills if filled] == expected_fills
    assert [i for i, _, _, _ in fills] == [s['bar']+1 for s in actions]
    assert all(price == D(str(case['opens'][i])) for i, _, _, price in fills)
    assert len(result.trades) == len(trades)
    for got, (opened, closed, qty, entry, exit_, pnl) in zip(result.trades, trades):
        assert got['opened_at'] == START+opened*DAY
        assert got['closed_at'] == START+closed*DAY
        assert tuple(Fraction(got[k]) for k in ('qty', 'entry_price', 'exit_price', 'pnl')) == (qty, entry, exit_, pnl)
    assert result.skipped_buys_insufficient_cash == skipped == case.get('expected_skipped_buys', 0)
    assert Fraction(result.max_unrealized_loss) == loss
    assert Fraction(a['max_unrealized_loss']) == loss
    assert a['grid_fills'] == sum(action != 'sell_all' for _, action in expected_fills)
    assert a['stop_loss_triggered'] == sum(action == 'sell_all' for _, action in expected_fills)
    cell_times = {START+i*DAY for i, action in expected_fills if action == 'sell_lot'}
    pnls = [t['pnl'] for t in result.trades if t['closed_at'] in cell_times]
    with localcontext(Context(prec=80)):
        expected_avg = round_cash(sum(pnls)/len(pnls)) if pnls else None
    assert (D(a['grid_cell_profit_avg']) if pnls else a['grid_cell_profit_avg']) == expected_avg
    if case['name'] == 'arithmetic':
        # 7 笔整批卖出 pnl 合计 57.81，÷7 = 8.258571428…，按 1e-8 量化输出
        assert a['grid_cell_profit_avg'] == '8.25857143'
    assert a['position_mode'] == 'grid'


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(provider, 'REPORTS_DIR', tmp_path / 'reports')
    return TestClient(provider.app)


def _fetch(case):
    opens, closes = [125]+case['opens'], [125]+case['closes']
    frame = pd.DataFrame(dict(Open=opens, Close=closes,
                              High=[max(o,c)+1 for o,c in zip(opens,closes)],
                              Low=[min(o,c)-1 for o,c in zip(opens,closes)], Volume=[10]*len(opens)),
                         index=pd.to_datetime([START+(i-1)*DAY for i in range(len(opens))], unit='s'))
    def fetch(exchange, market, symbol, timeframe, start, end):
        return frame.loc[(frame.index >= pd.to_datetime(start, unit='s')) &
                         (frame.index < pd.to_datetime(end, unit='s'))].copy()
    return fetch


def _post(client, case, *, params=None, market='spot', extra=None):
    payload = dict(run_id='grid_test', provider_tool_id=TOOL,
                   provider_params={'exchange':'binance', **(case['params'] if params is None else params)},
                   symbol='BTCUSDT', market=market, timeframe='1d', start_at=START,
                   end_at=START+len(case['closes'])*DAY, initial_capital=case['initial_capital'],
                   fee_bps=case['fee_bps'], slippage_bps=case['slippage_bps'], **(extra or {}))
    response = client.post('/cutie/backtest', json={'backtest':payload})
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize('case', CASES, ids=lambda c:c['name'])
def test_http_assumptions_match_ledger(client, monkeypatch, case):
    monkeypatch.setattr(provider, '_fetch_ohlcv', _fetch(case))
    body = _post(client, case)
    assert body['result_status'] == 'success', body
    result, assumptions, _, _ = _run(case)
    assert {k:body['assumptions'][k] for k in assumptions} == assumptions
    assert body['metrics']['trade_count'] == case['expected_trades']
    assert body['assumptions']['buy_fills'] == result.buy_fills
    assert body['assumptions']['sell_fills'] == result.sell_fills
    assert body['assumptions']['skipped_buys_insufficient_cash'] == result.skipped_buys_insufficient_cash
    assert body['equity_curve'][-1]['ts'] == result.equity_points[-1][0]
    assert D(body['equity_curve'][-1]['equity']) == result.final_cash
    for got, expected in zip(body['trades'], sorted(result.trades, key=lambda t:(t['closed_at'], t['opened_at']))):
        assert (got['opened_at'],got['closed_at']) == (expected['opened_at'], expected['closed_at'])
        assert all(D(got[k]) == expected[k] for k in ('qty','entry_price','exit_price','fee','slippage','pnl'))


def test_two_parameter_changes_change_trade_counts(client, monkeypatch):
    case = CASES[0]
    monkeypatch.setattr(provider, '_fetch_ohlcv', _fetch(case))
    counts = []
    for count in (None, 5, 10):
        params = {k:v for k,v in BASE.items() if k != 'grid_count'}
        if count is not None:
            params['grid_count'] = count
        body = _post(client, case, params=params)
        assert body['result_status'] == 'success', body
        counts.append(body['metrics']['trade_count'])
    assert len(set(counts)) == 3, counts


@pytest.mark.parametrize('bad', [
    {'lower_price':None}, {'upper_price':None}, {'amount_per_grid':None},
    {'lower_price':0}, {'upper_price':0}, {'amount_per_grid':0},
    {'lower_price':-1,'grid_mode':'geometric'}, {'lower_price':150}, {'lower_price':160},
    {'grid_count':4}, {'grid_count':101}, {'grid_count':5.5}, {'grid_count':True},
    {'grid_mode':'invalid'}, {'below_lower_action':'invalid'},
    {'amount_per_grid':True}, {'lower_price':'NaN'}, {'upper_price':'Infinity'},
])
def test_invalid_parameters(bad):
    params = {**BASE, **bad}
    params = {k:v for k,v in params.items() if v is not None}
    with pytest.raises(ValueError, match='INVALID_PARAMS'):
        provider._build_grid(params)


@pytest.mark.parametrize('bad', [
    {'lower_price':None}, {'upper_price':None}, {'amount_per_grid':None},
    {'lower_price':0}, {'upper_price':100}, {'amount_per_grid':-1},
    {'grid_count':4}, {'grid_count':101}, {'grid_mode':'invalid'}, {'below_lower_action':'invalid'},
])
def test_invalid_parameters_over_http(client, monkeypatch, bad):
    monkeypatch.setattr(provider, '_fetch_ohlcv', _fetch(CASES[0]))
    params = {k:v for k,v in {**BASE, **bad}.items() if v is not None}
    body = _post(client, CASES[0], params=params)
    assert (body['result_status'],body['error_type']) == ('failed','INVALID_PARAMS')


@pytest.mark.parametrize('market,params,extra,needle', [
    ('futures', BASE, {}, 'spot market only'),
    ('spot', {**BASE,'stop_loss_pct':5}, {}, 'stop_loss_pct'),
    ('spot', {**BASE,'take_profit_pct':5}, {}, 'take_profit_pct'),
    ('spot', {**BASE,'position_size_notional':500}, {}, 'position_size_notional'),
    ('spot', BASE, {'risk_policy':{'schema':'x'}}, 'risk_policy'),
    ('spot', BASE, {'signal_execution':{'schema':'x'}}, 'signal_execution'),
])
def test_rejections(client, monkeypatch, market, params, extra, needle):
    monkeypatch.setattr(provider, '_fetch_ohlcv', _fetch(CASES[0]))
    body = _post(client, CASES[0], market=market, params=params, extra=extra)
    assert (body['result_status'],body['error_type']) == ('failed','INVALID_PARAMS')
    assert needle in body['error_message']


def test_grid_lines_include_equality_and_upper_boundary():
    for mode, upper, lines in [('arithmetic',150,[100,110,120,130,140,150]),
                               ('geometric',3200,[100,200,400,800,1600,3200])]:
        config = provider._build_grid({**BASE,'grid_mode':mode,'upper_price':upper})['scale_in_out']
        level = inspect.getclosurevars(config['signal_factory']).nonlocals['level']
        assert [level(D(v)) for v in lines] == list(range(6))
        assert level(D(upper)+D('.001')) == 6
        assert level(D(100)-D('.001')) == -1
        assert [level(D(v)-D('.001')) for v in lines[1:]] == list(range(5))


def test_geometric_irrational_lines_use_explicit_decimal_precision():
    # sqrt(2) is not representable; the 80-digit grid must distinguish prices
    # separated by 1e-60 even under an inherited low-precision Inexact context.
    with localcontext(Context(prec=100)):
        root = D(2).sqrt()
        left, right = root-D('1e-60'), root+D('1e-60')
    with localcontext(Context(prec=12)) as ctx:
        ctx.traps[Inexact] = True
        config = provider._build_grid({**BASE,'lower_price':1,'upper_price':32,'grid_count':10,
                                      'grid_mode':'geometric'})['scale_in_out']
        level = inspect.getclosurevars(config['signal_factory']).nonlocals['level']
        assert (level(left), level(right)) == (0,1)


def test_empty_rise_follows_reference_then_buys_on_pullback():
    case = {**CASES[0], 'opens':[125]*5, 'closes':[110,145,130,130,130]}
    _, _, actions, _ = _run(case)
    assert actions == [dict(bar=2,action='buy',amount='100')]


def test_stop_loss_without_holdings_resets_only_after_reentry():
    case = {**CASES[0], 'params':{**BASE,'below_lower_action':'stop_loss'},
            'opens':[125]*7, 'closes':[150,90,90,120,120,110,110]}
    result, a, actions, _ = _run(case)
    assert actions == [dict(bar=5,action='buy',amount='100')]
    assert a['stop_loss_triggered'] == 0
    assert a['grid_cell_profit_avg'] is None
    assert result.trades[0]['closed_at'] == START+7*DAY
    assert result.trades[0]['exit_price'] == D(110)


def test_failed_fill_does_not_create_phantom_holdings():
    case = {**CASES[0], 'initial_capital':'1', 'opens':[125]*5,'closes':[150,140,150,160,160]}
    result, a, actions, _ = _run(case)
    assert actions == [dict(bar=1,action='buy',amount='100')]
    assert result.skipped_buys_insufficient_cash == 1
    assert result.trades == [] and a['grid_fills'] == 0
    assert a['grid_cell_profit_avg'] is None


def test_catalog_schema_and_runner():
    spec = provider.TOOL_SPECS[TOOL]
    entry = provider._catalog_tool(TOOL, spec, ['BTCUSDT'])
    assert spec['runner'] == provider.SCALE_IN_OUT_RUNNER
    assert spec['markets'] == entry['markets'] == ['spot']
    assert entry['param_schema']['properties'] == {
        'lower_price':{'type':'number','minimum':0}, 'upper_price':{'type':'number','minimum':0},
        'grid_count':{'type':'integer','default':20,'minimum':5,'maximum':100},
        'grid_mode':{'type':'string','default':'arithmetic','enum':['arithmetic','geometric']},
        'amount_per_grid':{'type':'number','minimum':0},
        'below_lower_action':{'type':'string','default':'pause','enum':['pause','stop_loss']},
        'exchange':{'type':'string','default':provider.DEFAULT_EXCHANGE},
        'time_layer_enabled': {'type':'boolean','default':False},
        'time_timezone': {'type':'string','default':'UTC'},
        'time_calendar': {'type':'string','default':'none',
                          'enum':['none','us_equity_regular','cme_btc_regular']},
        'time_session_start': {'type':'string','default':''},
        'time_session_end': {'type':'string','default':''},
        'time_weekdays': {'type':'integer','default':127,'minimum':1,'maximum':127},
        'time_max_holding_minutes': {'type':'integer','default':0,'minimum':0,'maximum':525600},
        'time_flatten_at': {'type':'string','default':''},
        'time_flatten_weekdays': {'type':'integer','default':127,'minimum':1,'maximum':127},
        'max_holding_bars': {'type':'integer','default':0,'minimum':0,'maximum':1000000},
    }
    assert not set(entry['param_schema']['properties']) & (set(provider._FIXED_RISK_PARAM_SCHEMA_PROPERTIES) - {'max_holding_bars'})
    assert spec['description'].endswith("maps to KOL '区间网格'")
    assert 'next bar open' in spec['description']
    built = provider._build_grid({k:v for k,v in BASE.items() if k != 'grid_count'})
    assert built['min_bars'] == 1
    assert built['scale_in_out']['lot_order'] == 'lifo'
    assert built['scale_in_out']['buy_notional'] == built['scale_in_out']['sell_notional'] == D(100)
