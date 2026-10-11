"""3a: opt-in schema, shared price kernel and real next-open execution."""
from __future__ import annotations
import sys
from pathlib import Path

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Exercise this checkout's schema contract, not a stale site-packages validator.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'validator'))
import cutie_backtesting_provider as p

NEW = dict(risk_layer_enabled=True, atr_stop_multiplier=2, risk_atr_period=14, take_profit_r=2)


def test_defaults_leave_legacy_risk_dict_unchanged():
    assert p._parse_fixed_risk_params({}) == {}
    defaults = {k: s['default'] for k, s in p._FIXED_RISK_PARAM_SCHEMA_PROPERTIES.items() if 'default' in s}
    assert p._parse_fixed_risk_params(defaults) == {}
    assert p._parse_fixed_risk_params(dict(defaults, stop_loss_pct=5)) == {'stop_loss_pct': .05}
    assert p._parse_fixed_risk_params(NEW) == NEW


@pytest.mark.parametrize('params', [
    {'atr_stop_multiplier': 2, 'risk_atr_period': 14}, {'risk_atr_period': 14}, {'take_profit_r': 2},
    dict(NEW, risk_layer_enabled=False), dict(NEW, risk_atr_period=0), dict(NEW, risk_atr_period=1),
    dict(NEW, atr_stop_multiplier=0), dict(NEW, stop_loss_pct=5), dict(NEW, take_profit_pct=5),
    {'risk_layer_enabled': True, 'take_profit_r': 2},
])
def test_invalid_cross_field_combinations_fail_closed(params):
    with pytest.raises(ValueError, match='INVALID_PARAMS:'):
        p._parse_fixed_risk_params(params)


@pytest.mark.parametrize('key', list(p._FIXED_RISK_PARAM_SCHEMA_PROPERTIES))
@pytest.mark.parametrize('bad', [True, False, float('nan'), float('inf'), -float('inf'), '2', None, 10**400])
def test_risk_types_and_nonfinite_rejected_by_schema_and_builder(key, bad):
    if key == 'risk_layer_enabled' and type(bad) is bool:
        return
    params = {key: bad}
    assert p._validate_params_against_schema(params, p._FIXED_RISK_PARAM_SCHEMA_PROPERTIES)
    with pytest.raises(ValueError, match='INVALID_PARAMS:'):
        p._parse_fixed_risk_params(params)


def test_new_keys_merged_into_runtime_mixins_and_excluded_from_other_runners():
    actual = set(enumerate_mixin_cases())
    assert fixture_names() <= actual
    assert len(actual) >= 19
    for tool_id, spec in p.TOOL_SPECS.items():
        included = spec.get('runner') not in ('kernel_v3', 'scale_in_out_ledger', 'turtle_group', p.ROTATION_RUNNER)
        for key in NEW:
            supported = included or (spec.get('runner') == p.TURTLE_RUNNER and key in p._TURTLE_RISK_KEYS)
            if tool_id in ('local.backtesting_py.opening_range_breakout', 'local.backtesting_py.asia_range_breakout', 'local.backtesting_py.calendar_schedule'):
                supported = key == 'risk_layer_enabled'
            if tool_id == 'local.backtesting_py.event_window' and key in ('atr_stop_multiplier', 'risk_atr_period'):
                supported = False  # P-EVENT0: no risk_warmup prefix, ATR keys withheld
            if tool_id.removeprefix('local.backtesting_py.') in p.MACRO_SCHEMAS:
                supported = tool_id.endswith('macro_release_breakout') and key == 'take_profit_r'
            if tool_id in (p.FUNDING_REVERSAL_TOOL_ID, p.TOP_LSR_TOOL_ID):
                supported = False  # P2/S3: Q18-style intrinsic stop, none of the new risk keys
            assert (key in spec['param_schema_properties']) == supported


@pytest.mark.parametrize('key', NEW)
def test_scale_in_out_rejects_every_new_key_even_disabled(key):
    assert p._scale_in_out_rejection({key: False if key == 'risk_layer_enabled' else 0}, {}, 'spot')

from decimal import Decimal as D
import pandas as pd
from backtesting import Backtest, Strategy
from strategy_risk_overlay import initial_risk_state, decide_exit, risk_assumptions


def bars_frame(rows):
    return pd.DataFrame(rows, columns=['Open', 'High', 'Low', 'Close'],
                        index=pd.date_range('2026-01-01', periods=len(rows), freq='h')).assign(Volume=1)


def manual_run(rows, params, side='long', entry_bar=1, warm=None, second_entry=None):
    class Manual(p._FixedRiskMixin, Strategy):
        _risk = p._parse_fixed_risk_params(params)

        def init(self):
            self._risk_init()
            self.states = []
            self.reasons = []

        def next(self):
            if self.position:
                exited = self._risk_check_exit()
                if self._risk.get('risk_layer_enabled'):
                    self.states.append(self._risk_state)
                    self.reasons.append(self._risk_exit_reason if exited else None)
                if exited:
                    return
            if not self.position and len(self.data) - 1 in (entry_bar, second_entry):
                self._risk_buy() if side == 'long' else self._risk_sell()
    if warm is not None:
        Manual._warmup_bars = len(warm)
        Manual._warmup_cols = {c: warm[c].to_numpy() for c in p._WARMUP_COLUMNS}
    return Backtest(bars_frame(rows), Manual, cash=100000, exclusive_orders=True,
                    finalize_trades=True).run()


@pytest.mark.parametrize('side', ['long', 'short'])
@pytest.mark.parametrize('kind', ['stop_loss', 'take_profit'])
def test_wick_only_touch_exits_next_open_not_at_stop_or_take(side, kind):
    stop = (94, 101) if side == 'long' else (99, 106)
    take = (99, 111) if side == 'long' else (89, 101)
    low, high = stop if kind == 'stop_loss' else take
    rows = [[100, 101, 99, 100]] * 3 + [[100, high, low, 100], [103, 104, 102, 103], [100, 101, 99, 100]]
    result = manual_run(rows, dict(risk_layer_enabled=True, stop_loss_pct=5, take_profit_pct=10), side)
    trade = result['_trades'].iloc[0]
    assert trade.EntryBar == 2 and trade.ExitBar == 4
    assert trade.ExitPrice == 103  # deliberately different from either protective price
    assert result['_strategy'].reasons[-1] == kind
    legacy = manual_run(rows, dict(stop_loss_pct=5, take_profit_pct=10), side)
    assert legacy['_trades'].iloc[0].ExitBar == 5


@pytest.mark.parametrize('side', ['long', 'short'])
def test_real_same_bar_dual_touch_stop_reason_wins(side):
    rows = [[100, 101, 99, 100]] * 3 + [[100, 111, 89, 100], [102, 103, 101, 102], [100, 101, 99, 100]]
    result = manual_run(rows, dict(risk_layer_enabled=True, stop_loss_pct=5, take_profit_pct=10), side)
    assert result['_trades'].iloc[0].ExitBar == 4
    assert result['_strategy'].reasons[-1] == 'stop_loss'


@pytest.mark.parametrize('side,stop,take', [('long', 95, 110), ('short', 105, 90)])
def test_r_targets_use_positive_frozen_initial_distance(side, stop, take):
    risk = p._parse_fixed_risk_params(dict(risk_layer_enabled=True, stop_loss_pct=5, take_profit_r=2))
    state = initial_risk_state(risk=risk, entry_price=100, direction=side)
    assert state.initial_stop == D(stop)
    assert state.initial_distance == D(5)
    assert state.take_price == D(take)
    assert decide_exit(state, high=take if side == 'long' else 101,
                       low=take if side == 'short' else 99) == 'take_profit'


def test_atr_uses_first_tr_wilder_seed_gap_and_preentry_history():
    # TR = 2, 11, 4; seed=2, then 6.5, then 5.25, not SMA's 7.5.
    rows = [[100, 101, 99, 100], [110, 111, 109, 110], [110, 112, 108, 110],
            [120, 150, 115, 120], [121, 122, 120, 121], [121, 122, 120, 121]]
    result = manual_run(rows, dict(risk_layer_enabled=True, atr_stop_multiplier=1, risk_atr_period=2,
                                   take_profit_r=2), entry_bar=2)
    state = result['_strategy'].states[0]
    assert state.entry_price == 120  # fill differs from signal close 110
    assert state.initial_distance == D('5.25')
    assert state.initial_stop == D('114.75')
    assert state.take_price == D('130.50')
    assert result['_strategy'].reasons[0] == 'take_profit'  # fill bar high hits target
    assert result['_trades'].iloc[0].ExitPrice == 121
    assert result['_trades'].iloc[0].ExitBar == 4


def test_atr_includes_warmup_prefix_and_does_not_recompute_after_entry():
    warm = bars_frame([[100, 102, 98, 100], [110, 111, 109, 110]])
    rows = [[110, 112, 108, 110], [110, 111, 109, 110], [110, 112, 108, 110],
            [110, 150, 107, 110], [110, 111, 109, 110]]
    result = manual_run(rows, dict(risk_layer_enabled=True, atr_stop_multiplier=2, risk_atr_period=4), warm=warm)
    # TR 4,11,4,2 => ATR 4 -> 5.75 -> 5.3125 -> 4.484375; D=8.968750.
    assert result['_strategy'].states[0].initial_distance == D('8.968750')
    assert all(state is result['_strategy'].states[0] for state in result['_strategy'].states)


@pytest.mark.parametrize('rows,params,match', [
    ([[100, 101, 99, 100]] * 5, dict(risk_layer_enabled=True, atr_stop_multiplier=1, risk_atr_period=4), 'history'),
    ([[1, 10, .1, 1]] * 5, dict(risk_layer_enabled=True, atr_stop_multiplier=2, risk_atr_period=2), 'stop'),
    ([[100, 101, 99, 100]] * 5, dict(risk_layer_enabled=True, stop_loss_pct=99, take_profit_r=100), 'take-profit'),
])
def test_unavailable_or_nonpositive_initial_levels_fail_explicitly(rows, params, match):
    with pytest.raises(ValueError, match=match):
        manual_run(rows, params, side='short' if match == 'take-profit' else 'long')


def test_risk_state_resets_for_reentry_at_different_fill():
    rows = [[100, 101, 99, 100]] * 3 + [[100, 101, 94, 100],
            [102, 103, 101, 102], [200, 201, 199, 200], [200, 201, 189, 200], [198, 199, 197, 198]]
    result = manual_run(rows, dict(risk_layer_enabled=True, stop_loss_pct=5, take_profit_r=2), second_entry=4)
    assert list(result['_trades'].EntryPrice) == [100, 200]
    assert list(result['_trades'].ExitBar) == [4, 7]
    assert result['_strategy'].states[-1].initial_distance == 10


def test_assumptions_only_added_for_opt_in():
    assert risk_assumptions({}) == {}
    assumptions = risk_assumptions(NEW)['risk_layer']
    assert assumptions['fill'] == 'next_bar_open_market'
    assert assumptions['fill_price_is_stop_price'] is False
    assert assumptions['atr_seed'] == 'first_true_range'

from fastapi.testclient import TestClient
from test_risk_overlay_compatibility import PARAMS, frame, enumerate_mixin_cases, fixture_names, tool_name


@pytest.mark.parametrize('name', PARAMS)
@pytest.mark.parametrize('exits', [dict(stop_loss_pct=.1, take_profit_r=2),
                                   dict(atr_stop_multiplier=.1, risk_atr_period=5, take_profit_r=2)])
def test_baseline_templates_reach_enabled_risk_execution(name, exits):
    params = dict(PARAMS[name], risk_layer_enabled=True, **exits)
    cls = p.TOOL_SPECS['local.backtesting_py.' + tool_name(name)]['build'](params)['strategy']
    result = Backtest(frame(), cls, cash=100000, exclusive_orders=True, finalize_trades=True).run()
    assert not result['_trades'].empty
    assert result['_strategy']._risk_state is not None
    assert result['_strategy']._risk_exit_reason in {'stop_loss', 'take_profit'}


def http_request(params):
    data = frame()
    return {'backtest': dict(run_id='risk_layer_http', provider_tool_id='local.backtesting_py.ema_cross',
                            provider_params=dict(ema_fast=5, ema_slow=20, **params), symbol='BTCUSDT',
                            market='spot', timeframe='1h', start_at=int(data.index[60].timestamp()),
                            end_at=int(data.index[-1].timestamp()) + 3600, initial_capital='10000',
                            fee_bps='10', slippage_bps='5')}


@pytest.mark.parametrize('params', [dict(atr_stop_multiplier=2, risk_atr_period=14),
                                   dict(risk_layer_enabled=1), dict(stop_loss_pct=True),
                                   dict(NEW, stop_loss_pct=5), dict(NEW, take_profit_pct=5)])
def test_http_invalid_params_rejected_before_market_data(monkeypatch, params):
    def forbidden(*args):
        raise AssertionError('invalid params must not fetch data')
    monkeypatch.setattr(p, '_fetch_ohlcv', forbidden)
    response = TestClient(p.app).post('/cutie/backtest', json=http_request(params))
    assert response.status_code == 200
    assert response.json()['result_status'] == 'failed'
    assert response.json()['error_type'] == 'INVALID_PARAMS'


@pytest.mark.parametrize('enabled', [False, True])
def test_http_assumptions_and_atr_warmup_window(monkeypatch, tmp_path, enabled):
    from backtesting import Backtest
    data, calls = frame(), []
    def fetch(exchange, market, symbol, timeframe, start, end):
        calls.append((start, end))
        lo, hi = pd.to_datetime(start, unit='s'), pd.to_datetime(end, unit='s')
        return data.loc[(data.index >= lo) & (data.index < hi)].copy()
    monkeypatch.setattr(p, '_fetch_ohlcv', fetch)
    monkeypatch.setattr(p, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(Backtest, 'plot', lambda self, **kwargs: None)
    params = dict(risk_layer_enabled=True, atr_stop_multiplier=2, risk_atr_period=50, take_profit_r=2) if enabled else {}
    response = TestClient(p.app).post('/cutie/backtest', json=http_request(params))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['result_status'] == 'success', body
    assert ('risk_layer' in body['assumptions']) == enabled
    # EMA target is 200 in both cases; this input has only 60 historical bars.
    # ATR's 50 bars cannot reduce the template's longer convergence target.
    expected_warmup = 60
    assert calls[1] == (int(data.index[60].timestamp()) - 202*3600, int(data.index[60].timestamp()))
    assert body['assumptions']['indicator_warmup_bars'] == expected_warmup
    if enabled:
        assert body['assumptions']['risk_layer']['fill_price_is_stop_price'] is False
        assert body['assumptions']['risk_layer']['atr_seed'] == 'first_true_range'
    assert len(calls) == 2
    assert body['trades']


def test_catalog_risk_schema_is_accepted_by_repo_validator():
    from cutie_backtest_provider_validator.schema_subset import validate_param_schema
    for spec in p.TOOL_SPECS.values():
        assert validate_param_schema({'type': 'object', 'additionalProperties': False,
                                      'properties': spec['param_schema_properties']}) == []


@pytest.mark.parametrize('params', [dict(stop_loss_pct=1e-320), dict(take_profit_pct=1e-320),
                                   dict(stop_loss_pct=5, take_profit_r=1e-320)])
def test_unrepresentable_levels_cannot_silently_disable_opt_in_protection(params):
    with pytest.raises(ValueError, match='INVALID_PARAMS:'):
        manual_run([[100, 101, 99, 100]] * 5, dict(risk_layer_enabled=True, **params))


@pytest.mark.parametrize('params', [dict(atr_stop_multiplier=101, risk_atr_period=2),
                                   dict(atr_stop_multiplier=1, risk_atr_period=501),
                                   dict(atr_stop_multiplier=1, risk_atr_period=2.5),
                                   dict(take_profit_r=101, stop_loss_pct=5),
                                   dict(atr_stop_multiplier=-1, risk_atr_period=2), dict(take_profit_r=-1)])
def test_new_numeric_bounds_are_enforced(params):
    with pytest.raises(ValueError, match='INVALID_PARAMS:'):
        p._parse_fixed_risk_params(dict(risk_layer_enabled=True, **params))


def short_breakout_response(monkeypatch):
    # Exactly five warmup bars plus 25 main bars; ATR(50) cannot arm an entry.
    closes = [100 + 2 * i for i in range(30)]
    data = bars_frame([[c, c + 1, c - 1, c] for c in closes])
    fetched_counts = []
    def fetch(exchange, market, symbol, timeframe, start, end):
        lo, hi = pd.to_datetime(start, unit='s'), pd.to_datetime(end, unit='s')
        result = data.loc[(data.index >= lo) & (data.index < hi)].copy()
        fetched_counts.append(len(result))
        return result
    monkeypatch.setattr(p, '_fetch_ohlcv', fetch)
    request = http_request({})
    request['backtest'].update(
        provider_tool_id='local.backtesting_py.breakout',
        provider_params=dict(lookback=2, exit_lookback=2, risk_layer_enabled=True,
                             atr_stop_multiplier=1, risk_atr_period=50),
        start_at=int(data.index[5].timestamp()), end_at=int(data.index[-1].timestamp()) + 3600)
    response = TestClient(p.app).post('/cutie/backtest', json=request)
    assert response.status_code == 200
    assert fetched_counts == [25, 5]
    return response.json()


def test_enabled_layer_invalid_params_message_passes_through_unchanged(monkeypatch):
    """启用层非法参数 ⇒ INVALID_PARAMS 原样透出。"""
    body = short_breakout_response(monkeypatch)
    assert body['result_status'] == 'failed'
    assert body['error_type'] == 'INVALID_PARAMS'
    assert body['error_message'] == 'INVALID_PARAMS:ATR entry history is shorter than risk_atr_period'
    assert body['limitations']['reason'] == 'validation_failure'


def test_plain_engine_exception_remains_engine_error(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError('plain engine failure')
    monkeypatch.setattr(Backtest, 'run', fail)
    body = short_breakout_response(monkeypatch)
    assert body['result_status'] == 'failed'
    assert body['error_type'] == 'ENGINE_ERROR'
    assert body['error_message'] == 'Backtest execution failed: plain engine failure'

# Preserve the original 3a type matrix when the shared schema grows. The 3b
# matrix owns new boolean/integer contracts; existing 3a assertions stay intact.
for _mark_index, _mark in enumerate(test_risk_types_and_nonfinite_rejected_by_schema_and_builder.pytestmark):
    if _mark.name == 'parametrize' and _mark.args[0] == 'key':
        test_risk_types_and_nonfinite_rejected_by_schema_and_builder.pytestmark[_mark_index] = pytest.mark.parametrize(
            'key', ['stop_loss_pct', 'take_profit_pct', 'position_size_pct', 'position_size_notional',
                    'risk_layer_enabled', 'atr_stop_multiplier', 'risk_atr_period', 'take_profit_r']).mark
