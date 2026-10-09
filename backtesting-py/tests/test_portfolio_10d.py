"""10D 手算表（期望值独立手写，初始10000，手续费10bps，滑点0，qty_step=1）。

初点 ETH/SOL close=90/45，现金10000，仓位0/0，NAV10000。
第1期：目标 .4/.4；next open=100/50，买ETH40 fee4、SOL80 fee4；
       现金1992，仓位40/80；close=110/55 => NAV10792。
第2期：目标 0/1；next open=120/60，开盘NAV=11592；先卖ETH40 fee4.8，
       可用现金6787.2；SOL差额6792，费用预留后可买113（gross6780 fee6.78）；
       现金0.42，仓位0/193；close=130/58 => NAV11194.42。
第3期：目标 .5/0；next open=140/50，开盘NAV9650.42；先卖SOL193 fee9.65；
       ETH目标4825.21下取34，gross4760 fee4.76；现金4876.01，仓位34/0；
       close=150/52 => NAV9976.01。
收益=(9976.01-10000)/10000=-.002399；回撤1218.41/11194.42=>8位half-even。
BTC注入close=100/110/105/120 => benchmark=10000/11000/10500/12000。
"""
from __future__ import annotations

import json
import sys
from copy import deepcopy
from decimal import Decimal, Inexact, localcontext
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from canonical_json import canonical_json_sha256
from portfolio_ledger import OpenPrices, PortfolioInvariantError, PortfolioLedger, SpotSpec, decimal_text, reconcile_snapshots
from portfolio_result_v4 import assemble_result_v4
from portfolio_runner import DailyBar, run_portfolio

D = Decimal
T0 = 1735689600
DAY = 86400
FIXTURE = Path(__file__).parent / 'fixtures' / 'portfolio_10d_golden.json'


def specs(step='1', min_qty='0', minimum='0'):
    return {s: SpotSpec(D(step), D(min_qty), D(minimum)) for s in ('ETHUSDT', 'SOLUSDT')}


def manifests():
    return [{'source': 'handwritten.fixture', 'symbol': s, 'market': 'spot', 'timeframe': '1d',
             'start_at': T0, 'end_at': T0+3*DAY, 'kline_count': 4, 'checksum_algo': 'sha256',
             'checksum': 'a'*64} for s in ('BTCUSDT', 'ETHUSDT', 'SOLUSDT')]


def golden_run(**overrides):
    bars = [DailyBar(T0+i*DAY, dict(zip(('ETHUSDT', 'SOLUSDT'), map(D, o))),
                     dict(zip(('ETHUSDT', 'SOLUSDT'), map(D, c))))
            for i, (o, c) in enumerate([(('80', '40'), ('90', '45')), (('100', '50'), ('110', '55')),
                                       (('120', '60'), ('130', '58')), (('140', '50'), ('150', '52'))])]
    params = dict(initial_cash=D('10000'), specs=specs(), bars=bars,
                  target_weights={T0: {'ETHUSDT': D('.4'), 'SOLUSDT': D('.4')},
                                  T0+DAY: {'SOLUSDT': D('1')}, T0+2*DAY: {'ETHUSDT': D('.5')}},
                  fee_bps=D('10'), btc_benchmark=[{'ts': T0+i*DAY, 'close_price': p}
                                                 for i, p in enumerate(('100', '110', '105', '120'))],
                  price_manifests=manifests())
    params.update(overrides)
    return run_portfolio(**params)


def ledger(capital='10000', **kwargs):
    return PortfolioLedger(D(capital), kwargs.pop('specs', specs()), D(kwargs.pop('fee', '10')),
                           D(kwargs.pop('slip', '0')))


def rebalance(l, weights, prices=('100', '50'), decision=T0):
    return l.rebalance(decision, {s: D(w) for s, w in weights.items()},
                       OpenPrices(decision+DAY, dict(zip(l.specs, map(D, prices)))))


def test_three_period_handwritten_golden_next_raw_open():
    result = golden_run().result
    expected = json.loads(FIXTURE.read_text())
    assert result == expected
    assert [(f['qty'], f['price'], f['fee'], f['slippage']) for f in result['fills']] == [
        ('40', '100', '4', '0'), ('80', '50', '4', '0'), ('40', '120', '4.8', '0'),
        ('113', '60', '6.78', '0'), ('193', '50', '9.65', '0'), ('34', '140', '4.76', '0')]
    assert [s['cash'] for s in result['snapshots']] == ['10000', '1992', '0.42', '4876.01']
    assert [p['equity'] for p in result['equity_curve']] == ['10000', '10792', '11194.42', '9976.01']
    assert golden_run().remaining_cost_basis == {'ETHUSDT': D('4764.76'), 'SOLUSDT': D('0')}


def test_sell_before_buy_uses_same_period_proceeds():
    fills = golden_run().result['fills']
    assert [(f['side'], f['symbol'], f['qty']) for f in fills[2:4]] == [
        ('sell', 'ETHUSDT', '40'), ('buy', 'SOLUSDT', '113')]
    assert golden_run().result['snapshots'][2]['cash'] == '0.42'


def test_floor_remainder_and_minimum_reasons():
    l = ledger('100', fee='0', specs=specs(step='.3'))
    fills = rebalance(l, {'ETHUSDT': '1'}, prices=('70', '50'))
    assert fills[0]['qty'] == '1.2'
    assert l.cash == D('16')
    m = ledger('100', fee='0', specs=specs(step='.1', minimum='30'))
    assert rebalance(m, {'ETHUSDT': '.2'}) == []
    assert m.rejections[-1]['reason'] == 'below_min_notional'
    n = ledger('100', fee='0', specs=specs(step='.1', min_qty='.3'))
    assert rebalance(n, {'ETHUSDT': '.2'}) == []
    assert n.rejections[-1]['reason'] == 'below_min_qty'


def test_fully_allocated_reserves_fee_and_slippage_before_floor():
    l = ledger('100', fee='10', slip='5', specs=specs(step='.1'))
    f = rebalance(l, {'ETHUSDT': '1'}, prices=('10', '50'))[0]
    assert f == {'seq': 1, 'ts': T0+DAY, 'symbol': 'ETHUSDT', 'side': 'buy', 'qty': '9.9',
                 'price': '10', 'fee': '0.099', 'slippage': '0.0495'}
    assert l.cash == D('.8515')
    assert l.mark({'ETHUSDT': D('10'), 'SOLUSDT': D('50')}).equity == D('99.8515')
    poor = ledger('0.1')
    assert rebalance(poor, {'ETHUSDT': '1'}) == []
    assert poor.rejections[-1]['reason'] == 'insufficient_cash'


def test_reconciliation_rejects_one_smallest_unit():
    result = golden_run().result
    broken = deepcopy(result['equity_curve'])
    broken[2]['equity'] = '11194.41999999'  # 人为给报告估值侧多扣一个1e-8单位。
    with pytest.raises(PortfolioInvariantError, match='equity reconciliation'):
        reconcile_snapshots(D('10000'), result['snapshots'], broken, result['fills'])
    broken_fills = deepcopy(result['fills'])
    broken_fills[0]['fee'] = '4.00000001'  # 独立流水侧同样必须拒绝任意差额。
    with pytest.raises(PortfolioInvariantError):
        reconcile_snapshots(D('10000'), result['snapshots'], result['equity_curve'], broken_fills)
    broken_snaps = deepcopy(result['snapshots'])
    broken_snaps[1]['cash'] = '1991.99999999'
    broken_snaps[1]['positions'][0]['qty'] = '40.00000000009090909090909090909091'
    with pytest.raises((PortfolioInvariantError, Inexact)):
        reconcile_snapshots(D('10000'), broken_snaps, result['equity_curve'], result['fills'])


def test_v4_exact_closed_keysets():
    r = golden_run().result
    assert set(r) == {'schema_version', 'fills', 'snapshots', 'equity_curve', 'btc_benchmark', 'metrics', 'input_manifests'}
    assert all(set(x) == {'seq', 'ts', 'symbol', 'side', 'qty', 'price', 'fee', 'slippage'} for x in r['fills'])
    assert all(set(x) == {'ts', 'cash', 'positions'} for x in r['snapshots'])
    assert all(set(p) == {'symbol', 'qty', 'close_price'} for s in r['snapshots'] for p in s['positions'])
    assert all(set(x) == {'ts', 'equity'} for x in r['equity_curve'])
    assert all(set(x) == {'ts', 'equity', 'close_price'} for x in r['btc_benchmark'])
    assert set(r['metrics']) == {'total_return', 'max_drawdown', 'fill_count'}
    assert set(r['input_manifests']) == {'prices', 'metric_series'}
    assert all(set(m) == {'source', 'symbol', 'market', 'timeframe', 'start_at', 'end_at', 'kline_count', 'checksum_algo', 'checksum'}
               for m in r['input_manifests']['prices'])


def test_pool_order_retained_and_snapshot_sorted():
    pool = {'symbols': [{'symbol': 'SOLUSDT', 'rank': 1}, {'symbol': 'ETHUSDT', 'rank': 2}]}
    r = golden_run(coin_pool=pool).result
    assert [f['symbol'] for f in r['fills'][:2]] == ['SOLUSDT', 'ETHUSDT']
    assert [p['symbol'] for p in r['snapshots'][1]['positions']] == ['ETHUSDT', 'SOLUSDT']
    assert r['snapshots'] == golden_run().result['snapshots']
    with pytest.raises(ValueError, match='frozen pool'):
        golden_run(coin_pool={'symbols': [{'symbol': 'SOLUSDT', 'rank': 1}]})


def test_fifo_partial_lots_costs_and_slippage_separately_deducted():
    l = ledger('1000', fee='10', slip='5')
    rebalance(l, {'ETHUSDT': '.2'}, prices=('100', '50'))  # 2ETH cost200.3
    rebalance(l, {'ETHUSDT': '.5'}, prices=('100', '50'), decision=T0+DAY)  # 2ETH cost200.3
    assert l.cost_basis()['ETHUSDT'] == D('400.6')
    rebalance(l, {'ETHUSDT': '.1'}, prices=('100', '50'), decision=T0+2*DAY)  # 差额300.06下取卖3，留最后一批1个
    assert l.quantities()['ETHUSDT'] == D('1')
    assert l.cost_basis()['ETHUSDT'] == D('100.15')
    assert l.lots['ETHUSDT'][0].opened_at == T0+2*DAY
    assert l.cash == D('898.95')
    rebalance(l, {}, prices=('100', '50'), decision=T0+3*DAY)
    assert l.quantities()['ETHUSDT'] == 0 and l.lots['ETHUSDT'] == []
    assert l.cost_basis()['ETHUSDT'] == 0 and l.cash == D('998.8')


@pytest.mark.parametrize('weights', [{'ETHUSDT': D('1.1')}, {'ETHUSDT': D('-1')}, {'OTHER': D('1')}, {'ETHUSDT': 0.5}])
def test_invalid_weights_leave_state_unchanged(weights):
    l = ledger()
    with pytest.raises(ValueError):
        l.rebalance(T0, weights, OpenPrices(T0+DAY, {'ETHUSDT': D('100'), 'SOLUSDT': D('50')}))
    assert l.cash == D('10000') and l.fills == [] and l.quantities() == {'ETHUSDT': D('0'), 'SOLUSDT': D('0')}


@pytest.mark.parametrize('bad', [D('NaN'), D('Infinity'), D('-1'), D('0'), 100.0])
def test_invalid_prices_rejected(bad):
    with pytest.raises(ValueError):
        ledger().mark({'ETHUSDT': bad, 'SOLUSDT': D('50')})


def test_nonexact_decimal128_rejected_atomically_and_caller_context_independent():
    l = ledger('9999999999999999999999999999999999', specs=specs(step='.00001'))
    with pytest.raises(Inexact):
        rebalance(l, {'ETHUSDT': '1'})
    assert l.fills == [] and l.cash == D('9999999999999999999999999999999999')
    with localcontext() as ctx:
        ctx.prec = 6
        assert golden_run().result == json.loads(FIXTURE.read_text())
        assert decimal_text(D('1234567890123456789012345678901234')) == '1234567890123456789012345678901234'


def test_time_boundaries_last_decision_and_future_data_do_not_change_prefix():
    l = ledger()
    with pytest.raises(ValueError, match='times'):
        l.rebalance(T0, {}, OpenPrices(T0, {'ETHUSDT': D('100'), 'SOLUSDT': D('50')}))
    original = golden_run().result
    decisions = {T0: {'ETHUSDT': D('.4'), 'SOLUSDT': D('.4')}, T0+DAY: {'SOLUSDT': D('1')},
                 T0+2*DAY: {'ETHUSDT': D('1')}, T0+3*DAY: {}}
    updated = golden_run(target_weights=decisions)
    assert updated.result['snapshots'][:3] == original['snapshots'][:3]
    assert updated.result['fills'][:4] == original['fills'][:4]
    assert updated.rejections[-1] == {'decision_ts': T0+3*DAY, 'reason': 'no_next_open'}


def test_metric_manifest_assembled_from_injected_frozen_series():
    series = {'metric': 'btc_dominance', 'source': 'coingecko', 'unit': 'percent',
              'points': [{'ts': T0+i*DAY, 'available_at': T0+i*DAY, 'value': '60'} for i in range(-20, 4)]}
    series['hash'] = canonical_json_sha256(series)
    r = golden_run(metric_series=series).result
    assert r['input_manifests']['metric_series'] == {
        'metric': 'btc_dominance', 'source': 'coingecko', 'unit': 'percent', 'hash': series['hash'],
        'start_at': T0-20*DAY, 'end_at': T0+3*DAY, 'point_count': 24}
    assert set(r['input_manifests']['metric_series']) == {'metric', 'source', 'unit', 'hash', 'start_at', 'end_at', 'point_count'}
    series['points'][0]['value'] = '61'
    with pytest.raises(ValueError, match='hash mismatch'):
        golden_run(metric_series=series)


@pytest.mark.parametrize('field,value', [('market', 'futures'), ('checksum', 'BAD'), ('kline_count', 3), ('start_at', T0+DAY)])
def test_invalid_manifest_rejected(field, value):
    ms = manifests()
    ms[0][field] = value
    with pytest.raises(ValueError, match='manifest'):
        golden_run(price_manifests=ms)


def test_benchmark_btc_position_alignment_and_half_even():
    from portfolio_result_v4 import _ratio
    assert _ratio(1, 200000000) == '0'
    assert _ratio(3, 200000000) == '0.00000002'
    r = json.loads(FIXTURE.read_text())
    for s in r['snapshots']:
        s['positions'][0]['symbol'] = 'BTCUSDT'
    for f in r['fills']:
        if f['symbol'] == 'ETHUSDT':
            f['symbol'] = 'BTCUSDT'
    with pytest.raises(ValueError, match='BTC position'):
        assemble_result_v4(initial_cash=D('10000'), snapshots=r['snapshots'], equity_curve=r['equity_curve'],
                           fills=r['fills'], btc_benchmark=[{k: v for k, v in b.items() if k != 'equity'} for b in r['btc_benchmark']],
                           price_manifests=r['input_manifests']['prices'])


def test_two_coin_full_allocation_fee_reservation():
    l = ledger('100', fee='10', slip='5', specs=specs(step='.1'))
    fills = rebalance(l, {'ETHUSDT': '.5', 'SOLUSDT': '.5'}, prices=('10', '10'))
    assert [f['qty'] for f in fills] == ['5', '4.9']
    assert [f['fee'] for f in fills] == ['0.05', '0.049']
    assert [f['slippage'] for f in fills] == ['0.025', '0.0245']
    assert l.cash == D('.8515')


def test_snapshot_state_mismatch_rejected_even_when_nav_matches():
    r = golden_run().result
    broken = deepcopy(r['snapshots'])
    broken[1]['cash'] = '1882'  # +1ETH、-110现金，报告NAV保持10792。
    broken[1]['positions'][0]['qty'] = '41'
    with pytest.raises(PortfolioInvariantError, match='cash/quantity'):
        reconcile_snapshots(D('10000'), broken, r['equity_curve'], r['fills'])


def test_second_fill_arithmetic_error_does_not_commit_first_fill():
    l = ledger()
    with pytest.raises(Inexact):
        rebalance(l, {'ETHUSDT': '.5', 'SOLUSDT': '.5'}, prices=('100', '123.4567890123456789012345678901234'))
    assert l.cash == D('10000') and l.fills == [] and l.cost_basis() == {'ETHUSDT': D('0'), 'SOLUSDT': D('0')}


def test_mutating_future_candle_does_not_change_past():
    from dataclasses import replace
    import portfolio_runner
    observed = []
    original = portfolio_runner.run_portfolio
    # Capture only injected test bars; no kernel/mock-price execution path.
    def capture(**kwargs):
        observed.append(kwargs['bars'])
        return original(**kwargs)
    from unittest.mock import patch
    with patch(__name__+'.run_portfolio', capture):
        baseline = golden_run().result
    changed = list(observed[0])
    changed[-1] = replace(changed[-1], open_prices={'ETHUSDT': D('1000'), 'SOLUSDT': D('1000')},
                          close_prices={'ETHUSDT': D('1'), 'SOLUSDT': D('1')})
    updated = golden_run(bars=changed).result
    assert updated['snapshots'][:3] == baseline['snapshots'][:3]
    assert updated['equity_curve'][:3] == baseline['equity_curve'][:3]
    assert updated['fills'][:4] == baseline['fills'][:4]


@pytest.mark.parametrize('container', ['equity_curve', 'btc_benchmark'])
def test_structural_timestamp_never_implicitly_coerced(container):
    r = json.loads(FIXTURE.read_text())
    bench = [{k: v for k, v in b.items() if k != 'equity'} for b in r['btc_benchmark']]
    points = bench if container == 'btc_benchmark' else r['equity_curve']
    points[0]['ts'] = float(T0)
    with pytest.raises(ValueError, match='safe integer'):
        assemble_result_v4(initial_cash=D('10000'), snapshots=r['snapshots'], equity_curve=r['equity_curve'],
                           fills=r['fills'], btc_benchmark=bench, price_manifests=r['input_manifests']['prices'])
