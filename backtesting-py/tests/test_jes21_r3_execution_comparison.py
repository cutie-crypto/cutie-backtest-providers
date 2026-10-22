"""JES21-R3-11c: actual built-class evidence, never name/boolean attestation."""
import copy
import sys
from pathlib import Path
import pandas as pd
import pytest
from backtesting import Backtest
from fastapi.testclient import TestClient
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p
TOOL = 'local.backtesting_py.ema_cross'
PARAMS = dict(ema_fast=5, ema_slow=20, stop_loss_pct=2)
CONTEXT = dict(symbol='BTCUSDT', market='spot', timeframe='1h', start_at=1767225600,
               end_at=1767513600, fee_bps='10', slippage_bps='5')


def body():
    spec = dict(schema='cutie.strategy_spec.v1', strategy_type='trend', symbol='BTCUSDT',
                market='spot', timeframe='1h', direction='long', parameters=dict(ema_fast=5, ema_slow=20),
                rules=dict(entry=['EMA(5) crosses above EMA(20)'], exit=['opposite cross'], risk=[]),
                stop_loss=dict(type='fixed_pct', pct=2), take_profit=None, position_sizing=None)
    return dict(schema='cutie.external_backtest.request.v1', provider={}, backtest=dict(
        run_id='jes21_r3', provider_tool_id=TOOL, provider_params=copy.deepcopy(PARAMS), initial_capital='10000',
        strategy=dict(strategy_name='Same name', strategy_spec=spec, rules=copy.deepcopy(spec['rules'])), **CONTEXT))


def semantics(request=None, params=None, tool=TOOL, built=None):
    request = body() if request is None else request
    params = copy.deepcopy(PARAMS if params is None else params)
    built = p.TOOL_SPECS[tool]['build'](params) if built is None else built
    return p._strategy_semantics(request, built['executed_name'], built=built, tool_id=tool, params=params, context=CONTEXT)


def test_complete_closed_spec_matches_actual_class():
    assumptions, limits, raw = semantics()
    assert limits['matches_current_strategy'] is True
    assert assumptions['strategy_binding'] == limits['strategy_match'] == 'match'
    assert raw['execution_comparison']['schema'] == 'execution_comparison.v1'
    assert raw['execution_comparison']['status'] == 'match'
    assert all(i['status'] == 'match' and i['reason'] for i in raw['execution_comparison']['items'])
    assert raw['execution_parameters']['provider_params']['ema_fast'] == 5
    assert raw['execution_parameters']['stop_loss'] == dict(status='known', type='fixed_pct', value='0.02', unit='pct_fraction', source='built.strategy._risk')
    assert raw['matches_current_strategy'] is True


@pytest.mark.parametrize('mutation', ['rules', 'name_only', 'boolean', 'extra_condition', 'conflict'])
def test_same_name_is_not_rule_proof(mutation):
    request = body()
    s = request['backtest']['strategy']
    if mutation == 'rules':
        s['strategy_spec']['rules']['entry'] = ['RSI(14) < 30']
        s['rules'] = copy.deepcopy(s['strategy_spec']['rules'])
    elif mutation == 'name_only':
        s.pop('strategy_spec')
    elif mutation == 'boolean':
        s.clear()
        s['matches_current_strategy'] = True
    elif mutation == 'extra_condition':
        s['strategy_spec']['conditions'] = {'volume': '> 100'}
    else:
        s['rules']['exit'] = ['hold forever']
    _, limits, raw = semantics(request)
    assert limits['matches_current_strategy'] is not True
    assert raw['execution_comparison']['status'] in ('unknown', 'mismatch')


@pytest.mark.parametrize('stop,status', [(2, 'match'), (3, 'mismatch'), (None, 'unknown')])
def test_fixed_stop_two_three_or_missing_execution_evidence(stop, status):
    params = dict(PARAMS, stop_loss_pct=stop) if stop else dict(ema_fast=5, ema_slow=20)
    built = p._build_ema_cross(params)
    if stop is None:
        built = dict(built, strategy=None)
    _, limits, raw = semantics(params=params, built=built)
    assert raw['execution_comparison']['status'] == status
    assert limits['matches_current_strategy'] is {'match': True, 'mismatch': False, 'unknown': None}[status]


def test_native_atr_is_known_without_fixed_stop_key():
    _, _, raw = semantics(params=dict(entry_period=20, exit_period=10), tool='local.backtesting_py.turtle')
    stop = raw['execution_parameters']['stop_loss']
    assert stop['status'] == 'known' and stop['type'] == 'atr'
    assert stop['unit'] == 'atr_multiple' and stop['value'] == '2'


def test_confirmed_no_stop_is_none_but_unknown_class_stays_unknown():
    _, _, raw = semantics(params=dict(ema_fast=5, ema_slow=20))
    assert raw['execution_parameters']['stop_loss']['status'] == 'none'
    _, _, raw = semantics(built=dict(executed_name='SampleStrategy', strategy=type('SampleStrategy', (), {})))
    assert raw['execution_parameters']['stop_loss']['status'] == 'unknown'
    assert raw['execution_comparison']['status'] == 'unknown'


def test_defaults_from_built_class_and_explicit_units_not_size_guessing():
    _, _, raw = semantics(params=dict(stop_loss_pct=0.5, position_size_pct=25))
    ep = raw['execution_parameters']
    assert ep['provider_params']['ema_fast'] == 20
    assert ep['stop_loss']['value'] == '0.005'
    assert ep['position']['unit'] == 'pct_fraction' and ep['position']['value'] == '0.25'


@pytest.mark.parametrize('tool,params', [(TOOL, PARAMS), ('local.backtesting_py.dca', {'amount': 100})])
@pytest.mark.parametrize('legacy', [False, True])
def test_both_http_call_paths_have_evidence(monkeypatch, tmp_path, tool, params, legacy):
    request = body()
    bt = request['backtest']
    bt.update(provider_tool_id=tool, provider_params=copy.deepcopy(params), fee_bps='0', slippage_bps='0')
    if legacy:
        bt.pop('strategy')
    closes = [100 + (i % 30) for i in range(80)]
    df = pd.DataFrame(dict(Open=closes, High=[v+1 for v in closes], Low=[v-1 for v in closes], Close=closes, Volume=[1]*80), index=pd.date_range('2026-01-01', periods=80, freq='h', tz='UTC'))
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *args: df.copy())
    monkeypatch.setattr(p, '_fetch_template_warmup', lambda *args: df.iloc[:20].copy())
    monkeypatch.setattr(Backtest, 'plot', lambda *args, **kwargs: None)
    response = TestClient(p.app).post('/cutie/backtest', json=request).json()
    assert response['result_status'] == 'success', response
    raw = response['raw_report']
    assert raw['execution_parameters']['provider_tool_id'] == tool
    assert raw['execution_parameters']['fee_bps'] == '0'
    assert raw['execution_comparison']['status'] == ('match' if tool == TOOL and not legacy else 'unknown')
    assert raw['strategy_semantics']['strategy_match'] == response['limitations']['strategy_match']
    assert raw['strategy_semantics']['matches_current_strategy'] == response['limitations']['matches_current_strategy']
    assert 'execution_parameters' not in response['metrics']


def test_unmapped_class_risk_dictionary_does_not_prove_consumption():
    cls = type('SampleStrategy', (), {'_risk': {'stop_loss_pct': 0.02}})
    _, _, raw = semantics(built=dict(executed_name='Same name', strategy=cls))
    assert raw['execution_parameters']['stop_loss']['status'] == 'unknown'


def test_turtle_percent_unit_compares_equivalent_fixed_take_profit():
    request = body()
    request['backtest']['strategy']['strategy_spec']['take_profit'] = dict(type='fixed_pct', pct=2)
    _, _, raw = semantics(request, params=dict(risk_layer_enabled=True, take_profit_pct=2), tool='local.backtesting_py.turtle')
    item = next(i for i in raw['execution_comparison']['items'] if i['path'] == 'take_profit')
    assert item['status'] == 'match'


@pytest.mark.parametrize('extra', ['legacy_risk_timing', 'snapshot_condition', 'conflicting_asset'])
def test_extra_semantics_cannot_get_complete_match(extra):
    request = body()
    params = dict(PARAMS)
    if extra == 'legacy_risk_timing':
        params['risk_layer_enabled'] = False
    elif extra == 'snapshot_condition':
        request['backtest']['strategy']['entry_conditions'] = ['volume > 100']
    else:
        request['backtest']['strategy']['asset'] = 'ETH'
    _, limits, raw = semantics(request, params=params)
    assert limits['matches_current_strategy'] is not True
    assert raw['execution_comparison']['status'] in ('unknown', 'mismatch')


def test_artifact_without_legacy_build_never_inherits_template_match():
    _, limits, raw = p._strategy_semantics({'schema': 'cutie.strategy_execution_request.v1',
        'strategy_spec': body()['backtest']['strategy']['strategy_spec']}, 'Same name')
    assert limits['matches_current_strategy'] is None
    assert raw['execution_comparison']['status'] == 'unknown'
