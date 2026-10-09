"""Independent filter goldens, registered entry wiring and immutable off-state proof."""
from __future__ import annotations
import importlib.util
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest
from backtesting import Backtest, Strategy
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p
import test_risk_overlay_compatibility as compat
from strategy_entry_filters import FilterConfig, FILTER_PARAM_SCHEMA_PROPERTIES, entry_mask

spec = importlib.util.spec_from_file_location('filter_capture', Path(__file__).parent / 'fixtures/capture_entry_filters_5b8320a.py')
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)
DEFAULTS = {key: value['default'] for key, value in FILTER_PARAM_SCHEMA_PROPERTIES.items()}
CASES = capture.cases()
BASELINE = json.loads((Path(__file__).parent / 'fixtures/entry_filters_5b8320a.json').read_text())
SINGLE_NAMES = sorted({name for name, _ in CASES.values()})
EXCLUDED = [key.removeprefix('local.backtesting_py.') for key, value in p.TOOL_SPECS.items()
            if value.get('runner') in ('kernel_v3', p.SCALE_IN_OUT_RUNNER, p.TURTLE_RUNNER, p.ROTATION_RUNNER)
            or key in p.FILTER_LAYER_UNWIRED_TOOLS]


def params(kind):
    values = dict(filter_layer_enabled=True, **{f'filter_{kind}_enabled': True})
    if kind == 'ema':
        values['filter_ema_period'] = 2
    if kind == 'macd':
        values.update(filter_macd_fast=2, filter_macd_slow=3)
    if kind == 'supertrend':
        values.update(filter_supertrend_atr_period=5, filter_supertrend_multiplier=1)
    return values


def frame(closes):
    return pd.DataFrame(dict(Open=closes, High=np.array(closes)+1, Low=np.array(closes)-1,
                             Close=closes, Volume=100), index=pd.date_range('2026-01-01', periods=len(closes), freq='h'))


@pytest.mark.parametrize('kind', ['ema', 'macd', 'supertrend'])
def test_independent_flat_up_down_golden(kind):
    # EMA2: at 110 => 106 2/3; at 90 => 95 5/9.
    # DIF(2,3): at 110 => 5/3; at 90 => -35/18.
    # ST(5,1): old upper=102, 110 crosses up; retained lower=106.2, 90 crosses down.
    data = frame([100]*10 + [110, 90])
    mask = entry_mask(FilterConfig.parse(params(kind)), data, p._supertrend_arrays)
    assert mask.tolist() == [False]*10 + [True, False]


def test_ema_equality_and_independent_recurrence():
    data = frame([100, 100, 110, 100, 90, 100, 120, 80])
    # Recursive EMA2 closes give signs = 0,0,+,-,-,+,+,-.
    actual = entry_mask(FilterConfig.parse(params('ema')), data, p._supertrend_arrays)
    assert actual.tolist() == [False, False, True, False, False, True, True, False]


def test_and_requires_every_enabled_filter():
    values = {**params('ema'), **params('macd'), **params('supertrend')}
    data = frame([100]*10+[110, 109, 90])
    # At 109: EMA2=108.222..., DIF=1.222... >0, ST stays up; 90 fails all.
    actual = entry_mask(FilterConfig.parse(values), data, p._supertrend_arrays)
    assert actual.tolist() == [False]*10+[True, True, False]
    # EMA price direction already positive after a small bounce; DIF remains negative.
    data = frame([100]*10+[80, 81, 90])
    assert entry_mask(FilterConfig.parse(params('ema')), data, p._supertrend_arrays)[-1]
    assert not entry_mask(FilterConfig.parse(values), data, p._supertrend_arrays)[-1]


@pytest.mark.parametrize('kind', ['ema', 'macd', 'supertrend'])
def test_future_prices_cannot_change_prefix(kind):
    before = frame([100]*10+[110, 90, 100, 100])
    after = frame([100]*10+[110, 90, 10000, 1])
    config = FilterConfig.parse(params(kind))
    a = entry_mask(config, before, p._supertrend_arrays)
    b = entry_mask(config, after, p._supertrend_arrays)
    np.testing.assert_array_equal(a[:12], b[:12])


@pytest.mark.parametrize('kind', ['ema', 'macd', 'supertrend'])
def test_signal_close_gate_and_exit_unrestricted(kind):
    config = FilterConfig.parse(params(kind))
    class Manual(p._FixedRiskMixin, Strategy):
        _filter_config = config
        def init(self):
            self._risk_init()
        def next(self):
            if self.position and self._risk_check_exit():
                return
            if len(self.data) - 1 == 10:
                self._risk_buy()
            elif self.position:
                self.position.close()
    data = frame([100]*10+[110, 90, 120, 80])
    trades = Backtest(data, Manual, cash=100000, exclusive_orders=True, finalize_trades=True).run()['_trades']
    # Signal is bar 10 (up), future bar 11 is down; entry fills at 11, exit at 12.
    assert trades[['EntryBar', 'ExitBar']].values.tolist() == [[11, 12]]


def test_blocked_signal_is_discarded_not_delayed():
    class Manual(p._FixedRiskMixin, Strategy):
        _filter_config = FilterConfig.parse(params('ema'))
        def init(self):
            self._risk_init()
        def next(self):
            if len(self.data) - 1 == 10:
                self._risk_buy()
    data = frame([100]*10+[90, 110, 120, 130])
    assert Backtest(data, Manual, cash=100000, finalize_trades=True).run()['_trades'].empty


def test_tail_cannot_backfill_entry():
    class Manual(p._FixedRiskMixin, Strategy):
        _filter_config = FilterConfig.parse(params('ema'))
        def init(self):
            self._risk_init()
        def next(self):
            if len(self.data) == 12:
                self._risk_buy()
    assert Backtest(frame([100]*11+[110]), Manual, cash=100000, finalize_trades=True).run()['_trades'].empty


@pytest.mark.parametrize('kind', ['ema', 'macd', 'supertrend'])
def test_risk_exit_still_fires_when_filter_blocks(kind):
    class Manual(p._FixedRiskMixin, Strategy):
        _filter_config = FilterConfig.parse(params(kind))
        _risk = {'stop_loss_pct': .05}
        def init(self):
            self._risk_init()
        def next(self):
            if self.position and self._risk_check_exit():
                return
            if len(self.data)-1 == 10:
                self._risk_buy()
    data = frame([100]*10+[110, 100, 90, 85, 80])
    trades = Backtest(data, Manual, cash=100000, finalize_trades=True).run()['_trades']
    assert trades[['EntryBar', 'ExitBar']].values.tolist() == [[11, 13]]


@pytest.fixture
def client(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('invalid filter request fetched market data')
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, '_fetch_ohlcv', forbidden)
    monkeypatch.setattr(p, '_fetch_template_warmup', forbidden)
    return TestClient(p.app)


def rejected(client, name, values, **backtest):
    body = capture.capture.request_body(name, values)
    body['backtest'].update(backtest)
    res = client.post('/cutie/backtest', json=body)
    assert res.status_code == 200
    assert res.json()['error_type'] == 'INVALID_PARAMS', res.json()


BAD = [dict(filter_ema_enabled=True), dict(filter_ema_period=50), dict(filter_macd_fast=5),
       dict(filter_supertrend_multiplier=2), dict(filter_layer_enabled=True),
       {**params('ema'), 'filter_ema_period': True}, {**params('ema'), 'filter_ema_period': 2.0},
       {**params('ema'), 'filter_ema_period': 501}, {**params('macd'), 'filter_macd_slow': 2},
       {**params('macd'), 'filter_macd_fast': 4}, {**params('supertrend'), 'filter_supertrend_multiplier': float('nan')},
       {**params('ema'), 'filter_macd_fast': 5}, dict(filter_timeframe='1d'), dict(filter_unknown=False),
       dict(filter_layer_enabled='false')]


@pytest.mark.parametrize('values', [v for v in BAD if not any(isinstance(x, float) and np.isnan(x) for x in v.values())])
def test_invalid_filters_fail_before_fetch(client, values):
    rejected(client, 'ema_cross', values)


@pytest.mark.parametrize('values', BAD)
def test_builder_rejects_invalid_config(values):
    with pytest.raises(ValueError, match='INVALID_PARAMS'):
        p._build_ema_cross(values)


@pytest.mark.parametrize('name', ['ema_pullback', 'breakout', 'cci_rsi'])
@pytest.mark.parametrize('direction', ['short', 'both'])
def test_short_both_rejected_before_fetch(client, name, direction):
    rejected(client, name, {**params('ema'), 'direction': direction})


def test_default_both_is_rejected_before_fetch(client):
    rejected(client, 'cci_rsi', params('ema'))


@pytest.mark.parametrize('name', EXCLUDED)
@pytest.mark.parametrize('key,value', list(DEFAULTS.items()))
def test_unwired_rejects_even_default_filter_keys(client, name, key, value):
    rejected(client, name, {key: value}, market='spot' if name in capture.capture.LEDGER_PARAMS else 'futures')
    assert key not in p.TOOL_SPECS['local.backtesting_py.'+name]['param_schema_properties']


def test_monthly_and_signal_execution_rejected_before_fetch(client):
    rejected(client, 'ema_cross', params('ema'), timeframe='1M')
    rejected(client, 'ema_cross', params('ema'), signal_execution={})


@pytest.mark.parametrize('case', CASES)
@pytest.mark.parametrize('warm', [False, True])
@pytest.mark.parametrize('explicit', [False, True])
def test_runtime_disabled_fingerprints(case, warm, explicit, monkeypatch):
    name, values = CASES[case]
    if explicit:
        values = {**values, **DEFAULTS}
    def forbidden(*args, **kwargs):
        pytest.fail('disabled filters performed indicator work')
    monkeypatch.setattr(p, 'entry_mask', forbidden)
    assert capture.snapshot(name, values, warm) == BASELINE['cases'][case+'/'+str(int(warm))]


def test_unwired_list_names_registered_tools_without_filter_keys():
    unwired = p.FILTER_LAYER_UNWIRED_TOOLS
    assert len(unwired) == 12 and len(set(unwired)) == 12
    for tool_id in unwired:
        assert tool_id in p.TOOL_SPECS, tool_id
        assert tool_id.removeprefix('local.backtesting_py.') in EXCLUDED
        assert not [k for k in p.TOOL_SPECS[tool_id]['param_schema_properties'] if k.startswith('filter_')], tool_id
        assert not set(SINGLE_NAMES) & {tool_id.removeprefix('local.backtesting_py.')}


FROZEN_UNWIRED_WAVE_B = frozenset('local.backtesting_py.' + n for n in (
    'opening_range_breakout asia_range_breakout calendar_schedule red_streak_rsi bullish_engulfing '
    'hammer_pin_bar morning_star three_white_soldiers bullish_doji_reversal inside_bar_breakout '
    'double_bottom inverse_head_shoulders').split())


def test_filter_unwired_list_only_shrinks_and_expires():
    """The unwired list may only shrink. A tool that gains filter keys or @_with_filter_config
    must leave it. At 7-P3 close-out replace the first assertion with
    `assert not p.FILTER_LAYER_UNWIRED_TOOLS`."""
    assert set(p.FILTER_LAYER_UNWIRED_TOOLS) <= FROZEN_UNWIRED_WAVE_B
    for tool_id in p.FILTER_LAYER_UNWIRED_TOOLS:
        spec = p.TOOL_SPECS[tool_id]
        assert not [k for k in spec['param_schema_properties'] if k.startswith('filter_')], tool_id
        assert not getattr(spec['build'], '_supports_entry_filters', False), tool_id


def test_runtime_schema_and_baseline_cover_every_single_direction():
    actual = {key.removeprefix('local.backtesting_py.') for key, tool in p.TOOL_SPECS.items()
              if 'filter_layer_enabled' in tool['param_schema_properties']}
    assert actual == set(SINGLE_NAMES)
    assert set(BASELINE['cases']) == {case+'/'+str(int(warm)) for case in CASES for warm in (False, True)}
    for name in SINGLE_NAMES:
        assert issubclass(p.TOOL_SPECS['local.backtesting_py.'+name]['build']({})['strategy'], p._FilterLayerMixin)


@pytest.mark.parametrize('name', compat.PARAMS)
@pytest.mark.parametrize('risk_case', compat.RISK_CASES)
@pytest.mark.parametrize('warm', [False, True])
@pytest.mark.parametrize('risk_disabled', [False, True])
def test_explicit_filter_off_preserves_risk_goldens(name, risk_case, warm, risk_disabled, monkeypatch):
    tool = p.TOOL_SPECS['local.backtesting_py.'+compat.tool_name(name)]
    original = tool['build']
    monkeypatch.setitem(tool, 'build', lambda values: original({**values, **DEFAULTS}))
    assert compat.fingerprint(name, risk_case, warm, risk_disabled) == compat.fixture_cases()[f'{name}/{risk_case}/{int(warm)}']


@pytest.mark.parametrize('name', SINGLE_NAMES)
@pytest.mark.parametrize('risk_enabled', [False, True])
def test_each_registered_template_uses_filter_gate(name, risk_enabled, monkeypatch):
    values = {**compat.PARAMS.get(name, {}), **params('ema'),
              'risk_layer_enabled': risk_enabled, 'stop_loss_pct': 3, 'take_profit_pct': 5}
    if 'direction' in p.TOOL_SPECS['local.backtesting_py.'+name]['param_schema_properties']:
        values['direction'] = 'long'
    if name.endswith('_bullish_divergence'):
        # These templates own a frozen L2 stop and actual-fill 2R target.
        values.pop('stop_loss_pct')
        values.pop('take_profit_pct')
    if name == 'chan_3buy':
        values.pop('stop_loss_pct')
        values.pop('take_profit_pct')
    cls = p.TOOL_SPECS['local.backtesting_py.'+name]['build'](values)['strategy']
    calls = []
    original = p._FilterLayerMixin._filter_allow_entry
    def observe(self):
        result = original(self)
        calls.append(result)
        return result
    monkeypatch.setattr(p._FilterLayerMixin, '_filter_allow_entry', observe)
    data = pd.concat([compat.frame()]*6, ignore_index=True)
    data.index = pd.date_range('2026-01-01', periods=len(data), freq=('15min' if name == 'us_open_momentum' else 'h'))
    trades = Backtest(data, cls, cash=100000, exclusive_orders=True, finalize_trades=True).run()['_trades']
    assert calls, name
    # EMA2 recurrence is independently recomputed using scalar arithmetic.
    ema = float(data.Close.iloc[0])
    allowed = [False]
    for close in data.Close.iloc[1:]:
        ema = 2*float(close)/3 + ema/3
        allowed.append(close > ema)
    assert all(allowed[int(bar)-1] for bar in trades.EntryBar), name


def test_request_config_isolation():
    a = p._build_ema_cross(params('ema'))['strategy']
    b = p._build_ema_cross({})['strategy']
    c = p._build_ema_cross(params('macd'))['strategy']
    assert a._filter_config.ema_enabled and b._filter_config is None and c._filter_config.macd_enabled
    assert a is not b and b is not c


@pytest.mark.parametrize('boundary', ['end_at', 'wall_clock'])
def test_http_drops_unclosed_tail_and_reports_filter(boundary, monkeypatch):
    values = {**params('ema'), 'ema_fast': 2, 'ema_slow': 3}
    body = capture.capture.request_body('ema_cross', values)
    data = frame([100]*10+[90, 110, 120])
    start = int(data.index[0].timestamp())
    cutoff = start+12*3600+1800
    body['backtest'].update(start_at=start, end_at=cutoff if boundary=='end_at' else start+14*3600)
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *args: data.copy())
    monkeypatch.setattr(p, '_fetch_template_warmup', lambda *args: data.iloc[:0].copy())
    monkeypatch.setattr(p.time, 'time', lambda: cutoff if boundary=='wall_clock' else start+20*3600)
    monkeypatch.setattr(Backtest, 'plot', lambda *args, **kwargs: None)
    res = TestClient(p.app).post('/cutie/backtest', json=body).json()
    assert res['result_status'] == 'success', res
    assert res['data_manifest']['kline_count'] == 12
    assert res['raw_report']['entry_filters']['clock'] == 'closed_signal_bar'
    assert 'entry_filters' not in res['metrics'] and 'entry_filters' not in res['data_manifest']
    assert all(t['opened_at'] < start+12*3600 for t in res['trades'])


def test_http_insufficient_filter_history(monkeypatch):
    body = capture.capture.request_body('ema_cross', {**params('ema'), 'filter_ema_period': 500})
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *args: compat.frame().iloc[60:].copy())
    monkeypatch.setattr(p, '_fetch_template_warmup', lambda *args: compat.frame().iloc[:0].copy())
    res = TestClient(p.app).post('/cutie/backtest', json=body).json()
    assert res['error_type'] == 'INSUFFICIENT_DATA'
    assert res['limitations']['reason'] == 'filter_history_insufficient'
