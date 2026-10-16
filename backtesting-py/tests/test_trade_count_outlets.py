"""PROV-TRADECOUNT-P: handwritten closed-count fixtures, no network."""
import importlib.util
import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import cutie_backtesting_provider as api
import test_basket_signal_http as basket_http
import test_btc_dominance_rotation_48 as dominance
import test_plot0_zero_trade as single_http
import test_portfolio_10d as portfolio
import test_portfolio_rotation_10e as weekly
import test_result_v2 as v2
import test_rsi_scale_in_out as scale
import test_strategy_kernel as kernel
import test_strategy_kernel_basket_sim as basket
from portfolio_result_v4 import assemble_result_v4
from portfolio_runner import DailyBar, run_portfolio
from strategy_kernel import compile_strategy_v3, simulate_v3
from strategy_execution import build_result_v3


def assembled():
    r = portfolio.golden_run().result
    return assemble_result_v4(
        initial_cash=Decimal('10000'), snapshots=r['snapshots'], equity_curve=r['equity_curve'],
        fills=r['fills'], btc_benchmark=[{'ts': b['ts'], 'close_price': b['close_price']}
                                       for b in r['btc_benchmark']],
        price_manifests=r['input_manifests']['prices'])


def rotation_http(monkeypatch, module):
    req = module.request()
    parser = weekly.parse_rotation if module is weekly else dominance.parse_dominance
    cfg = parser(req['provider_params'], req)
    data = module.streams(cfg)
    monkeypatch.setattr(api, '_verify_bearer', lambda _: None)
    monkeypatch.setattr(api, '_fetch_ohlcv_raw', lambda exchange, market, symbol, timeframe, start, end:
        ([[r['ts']*1000, r['open'], r['close'], r['open'], r['close'], '1']
          for r in data[symbol][0]], 'fixture', False, True))
    body = module.http(req) if module is weekly else module.http(req, module.series())
    assert body['result_status'] == 'success', body
    assert body['raw_report']['metrics'] == body['metrics']
    return body


@pytest.mark.parametrize('outlet,expected', [
    ('assembler', 2), ('runner', 2), ('weekly', 4), ('dominance', 2),
    ('weekly_http', 4), ('dominance_http', 2),
])
def test_v4_outlet_closed_count_string(monkeypatch, outlet, expected):
    builders = {'assembler': assembled, 'runner': lambda: portfolio.golden_run().result,
                'weekly': lambda: weekly.replay()[0].result,
                'dominance': lambda: dominance.replay()[0].result,
                'weekly_http': lambda: rotation_http(monkeypatch, weekly),
                'dominance_http': lambda: rotation_http(monkeypatch, dominance)}
    result = builders[outlet]()
    count = result['metrics']['trade_count']
    assert type(count) is str
    assert count == str(expected)
    assert expected == sum(f['side'] == 'sell' for f in result['fills'])


@pytest.mark.parametrize('weights,expected', [([], 0), (['.2', '.4'], 0),
                                            (['.2', '.4', '.3'], 1), (['.2', '.4', '.3', '0'], 2)])
def test_partial_sale_and_multiple_fifo_lots_count_sell_records(weights, expected):
    # Two buy lots; partial reduction then a sale consuming both remaining lots.
    # Two sells count as two, not four total fills or three FIFO closed fragments.
    d, t, day = Decimal, portfolio.T0, portfolio.DAY
    bars = [DailyBar(t+i*day, {'ETHUSDT': d('100')}, {'ETHUSDT': d('100')}) for i in range(5)]
    manifests = [{'source': 'handwritten.fixture', 'symbol': s, 'market': 'spot', 'timeframe': '1d',
                  'start_at': t, 'end_at': t+4*day, 'kline_count': 5, 'checksum_algo': 'sha256',
                  'checksum': 'a'*64} for s in ('BTCUSDT', 'ETHUSDT')]
    result = run_portfolio(initial_cash=d('1000'), specs={'ETHUSDT': portfolio.SpotSpec(d('1'), d('0'), d('0'))},
        bars=bars, target_weights={t+i*day: {'ETHUSDT': d(w)} for i, w in enumerate(weights)},
        fee_bps=d('0'), btc_benchmark=[{'ts': b.ts, 'close_price': '100'} for b in bars],
        price_manifests=manifests).result
    assert type(result['metrics']['trade_count']) is str
    assert result['metrics']['trade_count'] == str(expected)
    if expected == 2:
        assert [(f['side'], f['qty']) for f in result['fills']] == [
            ('buy', '2'), ('buy', '2'), ('sell', '1'), ('sell', '3')]


def test_all_success_outlets_have_trade_count(monkeypatch, tmp_path):
    """Runtime coverage of every metrics-producing family and HTTP pass-through."""
    results = {'v4_assembler': assembled(), 'v4_runner': portfolio.golden_run().result,
               'weekly': weekly.replay()[0].result, 'dominance': dominance.replay()[0].result,
               'weekly_http': rotation_http(monkeypatch, weekly),
               'dominance_http': rotation_http(monkeypatch, dominance), 'single_v2': v2._mtm_scenario()}
    plan = kernel.compile_spec(kernel.make_spec())
    results['spec_single'] = kernel.simulate(plan,
        [kernel.frame(0), kernel.frame(1, high='106', low='94', close='101')],
        kernel.initial_state(plan, kernel.execution_params()))
    a, b = basket.section_3_rows()
    simulation = simulate_v3(compile_strategy_v3(basket.signal_spec()), {'a': a, 'b': b},
        basket.rules(), '10000', start_at=basket.START_AT, end_at=basket.T0+4*basket.H4)
    results['basket_sim'] = simulation
    manifests = basket.build_data_manifests_v3(legs=basket.signal_spec()['market']['legs'],
        leg_klines={'a': a, 'b': b}, source='binance_futures', market='futures', timeframe='4h',
        start_at=basket.START_AT, end_at=basket.T0+4*basket.H4)
    results['basket_v3'] = build_result_v3(simulation=simulation, data_manifests=manifests)
    case = basket_http.CASES['sma_cross_basic']
    basket_http._install_leg_fetch(monkeypatch, case)
    results['basket_http'] = basket_http._post(basket_http._basket_request(case))
    monkeypatch.setattr(api, 'REPORTS_DIR', tmp_path/'reports')
    monkeypatch.setattr(api, 'AUTH_TOKEN', '')
    monkeypatch.setattr(api, '_fetch_ohlcv', scale._range_fetch(scale._jessie_frame()))
    results['scale_http'] = scale._post(TestClient(api.app))
    results['single_http'] = single_http.post(single_http.frame(single_http.VALUES), lambda *a, **kw: None)[0]
    path = Path(__file__).resolve().parents[2]/'freqtrade'/'cutie_freqtrade_provider.py'
    spec = importlib.util.spec_from_file_location('tradecount_freqtrade', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fixture = tmp_path/'freqtrade.json'
    fixture.write_text(json.dumps({'strategy': {'Fixture': {'total_trades': 1,
        'trades': [{'pair': 'BTC/USDT', 'profit_abs': 0}]}}}))
    results['freqtrade'] = module._parse_freqtrade_result(fixture, 'BTC/USDT')
    for outlet, result in results.items():
        if 'result_status' in result:
            assert result['result_status'] == 'success', (outlet, result)
        count = result['metrics']['trade_count']
        expected = (sum(f['side'] == 'sell' for f in result['fills'])
                    if result.get('schema_version') == 'cutie.backtest_result.v4' else len(result['trades']))
        assert type(count) is (str if 'fills' in result else int), outlet
        assert int(count) == expected, outlet
    for module in (weekly, dominance):
        entry = api._catalog_tool(module.TOOL_ID, deepcopy(api.TOOL_SPECS[module.TOOL_ID]), ['BTCUSDT'])
        assert 'trade_count' in entry['output_schema']['metrics']
