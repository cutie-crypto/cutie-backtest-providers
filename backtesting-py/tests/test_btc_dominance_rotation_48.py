"""C48 handwritten next-open goldens; no network.
BTC100, SOL10/ETH20, cash1000, zero costs, step1. Warmup BTC.D50; day0 40:
EMA20=50+(2/21)*(40-50)=49.047619..., buy SOL50/ETH25 day1; day6 60 -> sell day7.
"""
import asyncio
import json
import sys
from copy import deepcopy
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as api
from canonical_json import canonical_json_sha256
from portfolio_btc_dominance_rotation import TOOL_ID, parse_dominance, run_dominance
from portfolio_result_v4 import _metric_reference
from portfolio_rotation import DAY, RotationError, fetch_rotation
T0 = 1735689600  # 2025-01-01 UTC
SYMBOLS = ('SOLUSDT', 'ETHUSDT')


def rehash(series):
    series['hash'] = canonical_json_sha256({k: v for k, v in series.items() if k != 'hash'})
    return series


def request():
    pool = rehash({'selected_at': T0, 'source': 'coins.coingecko.market_cap_rank', 'size': 2,
                   'symbols': [{'symbol': s, 'rank': i+2} for i, s in enumerate(SYMBOLS)],
                   'excluded': [], 'version': 'coin_pool.v1'})
    return {'run_id': 'C48', 'provider_tool_id': TOOL_ID, 'market': 'spot', 'timeframe': '1d',
            'symbol': 'SOLUSDT', 'start_at': T0, 'end_at': T0+13*DAY,
            'initial_capital': '1000', 'fee_bps': '0', 'slippage_bps': '0',
            'provider_params': {'coin_pool': pool},
            'instrument_rules': {s: {'symbol': s, 'price_tick': '1', 'qty_step': '1',
                                     'min_qty': '0', 'min_notional': '0'} for s in SYMBOLS}}


def series():
    return rehash({'metric': 'btc_dominance', 'source': 'coinglass', 'unit': 'percent',
                   'points': [{'ts': T0+i*DAY, 'available_at': T0+(i+1)*DAY,
                               'value': '50' if i < 0 else '40' if i < 6 else '60'}
                              for i in range(-72, 13, 3)]})


def streams(config):
    return {s: ([{'ts': t, 'open': price, 'close': price}
                 for t in range(config.fetch_start, config.end, DAY)], 'handwritten.fixture', False, True)
            for s, price in [('SOLUSDT', '10'), ('ETHUSDT', '20'), ('BTCUSDT', '100')]}


def replay(req=None, metric=None, data=None):
    req = request() if req is None else req
    config = parse_dominance(req['provider_params'], req)
    return run_dominance(config, streams(config) if data is None else data, req,
                         metric_series=series() if metric is None else metric)


def http(req, metric):
    class Request:
        async def json(self):
            return {'backtest': req, 'metric_series': metric}
    return json.loads(asyncio.run(api.run_backtest(Request(), authorization=None)).body)


def test_entry_handwritten_positive_negative_and_btc_range_boundary():
    output, decisions, assumptions = replay()
    assert [(f['ts'], f['symbol'], f['side'], f['qty'], f['price']) for f in output.result['fills'][:2]] == [
        (T0+DAY, 'SOLUSDT', 'buy', '50', '10'), (T0+DAY, 'ETHUSDT', 'buy', '25', '20')]
    assert decisions[0]['decision_at'] == T0+DAY and decisions[0]['value_ts'] == T0
    assert all(p['qty'] == '0' for p in output.result['snapshots'][0]['positions'])
    assert '可用时间按点位日期 +1 天推定，非实际抓取时间' in assumptions.values()
    assert '历史点以首次入库为准（上游修订不回写）' in assumptions.values()
    assert '不换回 BTC' in assumptions['position_mode'] and '做空镜像未开放' in assumptions['position_mode']
    neutral = series()
    for p in neutral['points']:
        p['value'] = '50'
    assert replay(metric=rehash(neutral))[0].result['fills'] == []
    req = request()
    cfg = parse_dominance(req['provider_params'], req)
    data = streams(cfg)
    for p in data['BTCUSDT'][0]:
        if p['ts'] >= T0:
            p['close'] = '103.01'
    assert replay(req, data=data)[0].result['fills'] == []
    for p in data['BTCUSDT'][0]:
        if p['ts'] >= T0:
            p['close'] = '103'
    assert replay(req, data=data)[0].result['fills'][0]['ts'] == T0+DAY


def test_exit_all_to_cash_and_unsellable_order_fails():
    output, decisions, _ = replay()
    assert [(f['ts'], f['symbol'], f['side'], f['qty']) for f in output.result['fills'][2:]] == [
        (T0+7*DAY, 'SOLUSDT', 'sell', '50'), (T0+7*DAY, 'ETHUSDT', 'sell', '25')]
    assert decisions[1]['reason'] == 'btc_dominance_above_ema'
    assert output.result['snapshots'][7]['cash'] == '1000'
    assert all(p['qty'] == '0' for p in output.result['snapshots'][7]['positions'])
    assert output.result['metrics'] == {'total_return': '0', 'max_drawdown': '0', 'fill_count': '4'}
    req = request()
    req['instrument_rules']['SOLUSDT']['min_notional'] = '100'
    cfg = parse_dominance(req['provider_params'], req)
    data = streams(cfg)
    for p in data['SOLUSDT'][0]:
        if p['ts'] >= T0+7*DAY:
            p['open'] = p['close'] = '1'
    with pytest.raises(RotationError, match='minimum order') as error:
        replay(req, data=data)
    assert error.value.reason == 'liquidation_below_minimum'
    req = request()
    for rule in req['instrument_rules'].values():
        rule['min_notional'] = '2000'
    output, decisions, _ = replay(req)
    assert output.result['fills'] == []
    assert [d['action'] for d in decisions] == ['entry']*6


def test_available_at_one_second_after_close_and_nonmonotone_arrivals():
    metric = series()
    metric['points'][24]['available_at'] = T0+DAY+1
    output, decisions, _ = replay(metric=rehash(metric))
    assert output.result['fills'][0]['ts'] == T0+2*DAY
    assert decisions[0]['decision_at'] == T0+2*DAY
    metric['points'][24]['available_at'] = T0+DAY
    assert replay(metric=rehash(metric))[0].result['fills'][0]['ts'] == T0+DAY
    metric['points'][23]['available_at'] = T0+5*DAY  # older late point must not hide newer day0
    assert replay(metric=rehash(metric))[0].result['fills'][0]['ts'] == T0+DAY
    changed = deepcopy(metric)
    changed['points'][-1]['value'] = '0'
    before, after = replay(metric=rehash(metric)), replay(metric=rehash(changed))
    assert before[0].result['fills'] == after[0].result['fills']
    assert before[1][:2] == after[1][:2]


def test_gap_and_closed_point_identity_value_hash_validation(monkeypatch):
    monkeypatch.setattr(api, '_verify_bearer', lambda _: None)
    monkeypatch.setattr(api, '_fetch_basket_leg_klines', lambda *a: pytest.fail('invalid metric fetched prices'))
    cases = []
    gap = series()
    del gap['points'][10:12]  # 9-day gap; 3/6/7-day gaps allowed
    cases.append(rehash(gap))
    for field, val in [('source', 'wrong'), ('metric', 'open_interest'), ('unit', 'usd'), ('points', []), ('hash', '0'*64)]:
        bad = series()
        bad[field] = val
        cases.append(bad)
    for field, val in [('ts', T0+1), ('ts', True), ('available_at', T0-100*DAY),
                       ('available_at', 1.5), ('value', '100.01'), ('value', '-0.1'), ('value', '50.0'),
                       ('value', '5e1'), ('value', 'NaN'), ('value', 50), ('extra', 'x')]:
        bad = series()
        bad['points'][10][field] = val
        cases.append(bad)
    missing = series()
    del missing['points'][10]['value']
    cases.append(missing)
    duplicate = series()
    duplicate['points'][10]['ts'] = duplicate['points'][9]['ts']
    cases.append(rehash(duplicate))
    missing = series()
    del missing['unit']
    cases.append(missing)
    for bad in cases:
        response = http(request(), bad)
        assert response['result_status'] == 'failed', bad
        assert response['error_type'] == 'ENGINE_ERROR', bad  # #47 metric validation mapping
    metric = series()
    metric['points'][10]['ts'] -= DAY
    _metric_reference(rehash(metric), T0, T0+12*DAY)
    metric = series()
    del metric['points'][10]
    _metric_reference(rehash(metric), T0, T0+12*DAY)
    req = request()
    req['provider_params']['btcd_ema_period'] = 100
    with pytest.raises(RotationError) as error:
        replay(req)
    assert error.value.code == 'INSUFFICIENT_DATA'
    cfg = parse_dominance(request()['provider_params'], request())
    data = streams(cfg)
    data['BTCUSDT'] = (data['BTCUSDT'][0][1:], *data['BTCUSDT'][1:])
    with pytest.raises(RotationError) as error:
        fetch_rotation(cfg, lambda s, a, b: data[s])
    assert error.value.code == 'INSUFFICIENT_DATA'


def test_schema_defaults_bounds_catalog_and_unwired_params(monkeypatch):
    monkeypatch.setattr(api, '_verify_bearer', lambda _: None)
    monkeypatch.setattr(api, '_fetch_basket_leg_klines', lambda *a: pytest.fail('invalid params fetched'))
    cfg = parse_dominance(request()['provider_params'], request())
    assert (cfg.btcd_ema_period, cfg.btc_range_days, cfg.btc_range_pct) == (20, 7, 3)
    entry = api._catalog_tool(TOOL_ID, api.TOOL_SPECS[TOOL_ID], ['BTCUSDT'])
    props = entry['param_schema']['properties']
    assert (props['btcd_ema_period']['minimum'], props['btcd_ema_period']['maximum']) == (5, 100)
    assert props['btc_range_days']['default'] == 7 and props['btc_range_pct']['default'] == 3
    assert entry['output_schema']['schema_version'] == 'cutie.backtest_result.v4'
    assert entry['markets'] == ['spot'] and entry['timeframes'] == ['1d']
    assert entry['execution']['timeout_ms'] == 120000
    assert not any(key.startswith('time_') for key in props)
    for key, value in [('btcd_ema_period', 4), ('btcd_ema_period', 101), ('btcd_ema_period', 5.5),
                       ('btcd_ema_period', True), ('btc_range_days', 0), ('btc_range_days', 1.5),
                       ('btc_range_pct', -1), ('top_k', 2), ('btc_risk_enabled', False),
                       ('time_layer_enabled', True), ('position_size_risk_pct', 1)]:
        req = request()
        req['provider_params'][key] = value
        assert http(req, series())['error_type'] == 'INVALID_PARAMS', (key, value)
    for period in (5, 100):
        req = request()
        req['provider_params']['btcd_ema_period'] = period
        assert parse_dominance(req['provider_params'], req).btcd_ema_period == period
    req = request()
    req['provider_params']['btc_range_days'] = 30
    assert parse_dominance(req['provider_params'], req).fetch_start == T0-30*DAY


def test_illegal_symbols_and_modes_rejected_before_fetch(monkeypatch):
    monkeypatch.setattr(api, '_verify_bearer', lambda _: None)
    monkeypatch.setattr(api, '_fetch_basket_leg_klines', lambda *a: pytest.fail('invalid symbol/mode fetched'))
    for symbol in ('BTCUSDT', 'SOLUSD', ''):
        req = request()
        pool = req['provider_params']['coin_pool']
        pool['symbols'][0]['symbol'] = symbol
        rehash(pool)
        req['instrument_rules'][symbol] = {**req['instrument_rules'].pop('SOLUSDT'), 'symbol': symbol}
        assert http(req, series())['error_type'] == 'INVALID_PARAMS', symbol
    for field, value in [('market', 'futures'), ('timeframe', '4h'), ('direction', 'short'), ('leverage', 2)]:
        req = request()
        req[field] = value
        assert http(req, series())['error_type'] == 'INVALID_PARAMS'


def test_metric_series_interval_extra_key_rejected(monkeypatch):
    monkeypatch.setattr(api, '_verify_bearer', lambda _: None)
    monkeypatch.setattr(api, '_fetch_basket_leg_klines', lambda *a: pytest.fail('interval key fetched prices'))
    metric = rehash({**series(), 'interval': '3d'})
    with pytest.raises(ValueError, match='identity'):
        replay(metric=metric)
    assert http(request(), metric)['error_type'] == 'ENGINE_ERROR'


def test_http_routes_to_dominance_runner_and_v4_manifest(monkeypatch):
    monkeypatch.setattr(api, '_verify_bearer', lambda _: None)
    req = request()
    cfg = parse_dominance(req['provider_params'], req)
    data = streams(cfg)
    calls = []
    def raw(exchange, market, symbol, timeframe, start, end):
        assert (market, timeframe, start, end) == ('spot', '1d', cfg.fetch_start, cfg.end)
        calls.append(symbol)
        return [[r['ts']*1000, r['open'], r['close'], r['open'], r['close'], '1'] for r in data[symbol][0]], 'fixture', False, True
    monkeypatch.setattr(api, '_fetch_ohlcv_raw', raw)
    metric = series()
    response = http(req, metric)
    assert response['result_status'] == 'success'
    assert response['schema_version'] == 'cutie.backtest_result.v4'
    assert response['metrics'] == {'total_return': '0', 'max_drawdown': '0', 'fill_count': '4'}
    assert response['fills'] == replay()[0].result['fills']
    assert response['input_manifests']['metric_series'] == {
        'metric': 'btc_dominance', 'source': 'coinglass', 'unit': 'percent', 'hash': metric['hash'],
        'start_at': T0-72*DAY, 'end_at': T0+12*DAY, 'point_count': 29}
    assert sorted(calls) == ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']
    assert response['raw_report']['decisions'][0]['value_ts'] == T0
    assert set(response['fills'][0]) == {'seq', 'ts', 'symbol', 'side', 'qty', 'price', 'fee', 'slippage'}


@pytest.mark.parametrize('tail_days,accepted', [(1, True), (7, True), (8, False), (-1, False)])
def test_coinglass_sparse_tail_coverage_boundary(tail_days, accepted):
    # Shift every real point, preserving valid spacing/count/hash: only the tail boundary changes.
    metric = series()
    for point in metric['points']:
        point['ts'] -= tail_days*DAY
        point['available_at'] -= tail_days*DAY
    rehash(metric)
    if not accepted:
        with pytest.raises(ValueError, match='warmup days plus interval'):
            replay(metric=metric)
        return
    output, _, _ = replay(metric=metric)
    assert output.result['input_manifests']['metric_series']['end_at'] == T0+(12-tail_days)*DAY
    assert output.result['snapshots'][-1]['ts'] == T0+12*DAY
