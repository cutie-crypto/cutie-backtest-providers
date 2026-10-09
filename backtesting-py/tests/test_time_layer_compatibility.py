"""Immutable off-state evidence and real template/HTTP entry wiring."""
from __future__ import annotations
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys

import pandas as pd
import pytest
from backtesting import Backtest, Strategy
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as provider
import test_risk_overlay_compatibility as compat
from strategy_time_layer import TimeConfig, TimeContext
from test_time_layer import BAD_PARAMS, DEFAULTS

spec = importlib.util.spec_from_file_location('time_capture', Path(__file__).parent / 'fixtures/capture_time_layer_11e8cfb.py')
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)
BASELINE = json.loads((Path(__file__).parent / 'fixtures/time_layer_ledger_11e8cfb.json').read_text())
ADDED_BASELINE = json.loads((Path(__file__).parent / 'fixtures/time_layer_single_da027cd.json').read_text())
assert BASELINE['single'].keys().isdisjoint(ADDED_BASELINE['single'])
BASELINE['single'] = {**BASELINE['single'], **ADDED_BASELINE['single']}
F5_BASELINE = json.loads((Path(__file__).parent / 'fixtures/time_layer_single_f5_b42210b.json').read_text())
assert BASELINE['single'].keys().isdisjoint(F5_BASELINE['single'])
BASELINE['single'].update(F5_BASELINE['single'])
PATTERN_BASELINE = json.loads((Path(__file__).parent / 'fixtures/9t1_candle_off.json').read_text())
assert BASELINE['single'].keys().isdisjoint(PATTERN_BASELINE['single'])
BASELINE['single'].update(PATTERN_BASELINE['single'])
PATTERN2_BASELINE = json.loads((Path(__file__).parent / 'fixtures/9t2_candle_off.json').read_text())
assert BASELINE['single'].keys().isdisjoint(PATTERN2_BASELINE['single'])
BASELINE['single'].update(PATTERN2_BASELINE['single'])
PATTERN3_BASELINE = json.loads((Path(__file__).parent / 'fixtures/9t3_bottom_off.json').read_text())
assert BASELINE['single'].keys().isdisjoint(PATTERN3_BASELINE['single'])
BASELINE['single'].update(PATTERN3_BASELINE['single'])
DIVERGENCE_BASELINE = json.loads((Path(__file__).parent / 'fixtures/9t4_divergence_off.json').read_text())
assert BASELINE['single'].keys().isdisjoint(DIVERGENCE_BASELINE['single'])
BASELINE['single'].update(DIVERGENCE_BASELINE['single'])
CHAN_BASELINE = json.loads((Path(__file__).parent / 'fixtures/9t6_chan_off.json').read_text())
assert BASELINE['single'].keys().isdisjoint(CHAN_BASELINE['single'])
BASELINE['single'].update(CHAN_BASELINE['single'])
FIB_BASELINE = json.loads((Path(__file__).parent / 'fixtures/time_layer_single_9t5_b42210b.json').read_text())
assert BASELINE['single'].keys().isdisjoint(FIB_BASELINE['single'])
BASELINE['single'].update(FIB_BASELINE['single'])
NEW_BASELINE = json.loads((Path(__file__).parent / 'fixtures/time_layer_calendar_templates.json').read_text())
assert BASELINE['single'].keys().isdisjoint(NEW_BASELINE['single'])
BASELINE['single'].update(NEW_BASELINE['single'])
SHORTPAT2_BASELINE = json.loads((Path(__file__).parent / 'fixtures/shortpat2_time_off.json').read_text())
assert BASELINE['single'].keys().isdisjoint(SHORTPAT2_BASELINE['single'])
BASELINE['single'].update(SHORTPAT2_BASELINE['single'])
F1_CASES = {'opening_range_breakout', 'asia_range_breakout'}
F2_CASES = {'calendar_schedule'}
MIXINS = {name: cls for name, cls in compat.enumerate_mixin_cases().items()
          if name != "red_streak_rsi"}
LEGACY_MIXINS = {name: cls for name, cls in MIXINS.items() if name not in F1_CASES | F2_CASES}


@pytest.mark.parametrize('name', compat.PARAMS)
@pytest.mark.parametrize('risk_case', compat.RISK_CASES)
@pytest.mark.parametrize('warm', [False, True])
@pytest.mark.parametrize('risk_disabled', [False, True])
def test_disabled_matches_immutable_single_fingerprints(monkeypatch, name, risk_case, warm, risk_disabled):
    tool = provider.TOOL_SPECS['local.backtesting_py.' + compat.tool_name(name)]
    original = tool['build']
    monkeypatch.setitem(tool, 'build', lambda params: original({**params, **DEFAULTS}))
    assert compat.fingerprint(name, risk_case, warm, risk_disabled) == compat.fixture_cases()[f'{name}/{risk_case}/{int(warm)}']


@pytest.mark.parametrize('name', capture.LEDGER_PARAMS)
def test_ledger_unchanged(name):
    assert capture.ledger_fingerprint(name) == BASELINE['ledger'][name]


@pytest.mark.parametrize('name', LEGACY_MIXINS)
@pytest.mark.parametrize('warm', [False, True])
def test_disabled_response_and_start_unchanged(monkeypatch, name, warm):
    params = {'direction': 'short'} if name.endswith('_short') else {}
    tool = provider.TOOL_SPECS['local.backtesting_py.' + compat.tool_name(name)]
    expected = BASELINE['single'][name]
    built = tool['build']({**params, **DEFAULTS})
    assert built['min_bars'] == expected['min_bars']
    def forbidden(*args, **kwargs):
        raise AssertionError('disabled layer must not construct a TimeContext')
    monkeypatch.setattr(provider.TimeContext, 'build', forbidden)
    body = capture.response(compat.tool_name(name), {**params, **DEFAULTS}, warm)
    assert 'time_layer' not in body['assumptions']
    assert 'time_layer' not in body['raw_report']
    assert all(not any(key.startswith('time_') for key in trade) for trade in body['trades'])
    assert body['assumptions']['indicator_warmup_bars'] == expected[str(int(warm))]['warmup_bars']
    assert capture.digest(body['assumptions']) == expected[str(int(warm))]['assumptions_sha256']
    assert capture.digest(body['raw_report']) == expected[str(int(warm))]['raw_report_sha256']


def test_schema_only_runtime_single_position_templates():
    actual = {tool.removeprefix('local.backtesting_py.') for tool, spec in provider.TOOL_SPECS.items()
              if 'time_layer_enabled' in spec['param_schema_properties']}
    assert actual == {compat.tool_name(name) for name in MIXINS} | set(capture.LEDGER_PARAMS) | {"red_streak_rsi"}
    assert set(BASELINE['single']) | F1_CASES | F2_CASES == set(MIXINS)


@pytest.mark.parametrize('name', MIXINS)
@pytest.mark.parametrize('risk_enabled', [False, True])
def test_each_template_gates_entries_but_allows_outside_exits(name, risk_enabled):
    if name in PATTERN3_BASELINE['single']:
        from test_9t3_patterns import compatibility_frame
        data = compatibility_frame(name)
    elif name in PATTERN2_BASELINE['single']:
        from test_9t2_patterns import compatibility_frame
        data = compatibility_frame(name)
    elif name in PATTERN_BASELINE['single']:
        from test_9t1_engulf_pin import compatibility_frame
        data = compatibility_frame(name)
    else:
        data = pd.concat([compat.frame()] * 6, ignore_index=True)
    data.index = pd.date_range('2026-01-01', periods=len(data), freq='h')
    params = {**compat.PARAMS.get(name, {}), 'stop_loss_pct': 3, 'take_profit_pct': 5,
              'risk_layer_enabled': risk_enabled, 'time_layer_enabled': True,
              'time_session_start': '06:00', 'time_session_end': '10:00'}
    if name in F1_CASES:
        params['flatten_at'] = '23:00'  # this test uses 1h candles
        params.pop('stop_loss_pct')
        params.pop('take_profit_pct')
    if name in F2_CASES:
        params.update(time_entry_at='07:00', time_max_holding_minutes=240, calendar_stop_enabled=False)
        params.pop('stop_loss_pct')
        params.pop('take_profit_pct')
    if name in PATTERN_BASELINE['single'] or name in PATTERN2_BASELINE['single'] or name in PATTERN3_BASELINE['single']:
        params.pop('stop_loss_pct')
        params.pop('take_profit_pct')
    if name in DIVERGENCE_BASELINE['single']:
        from test_9t4_divergence import hand_frame, hand_params
        data = hand_frame()
        # Signal23 at07:00, fill24 at08:00; MACD exit28 at12:00.
        data.index = pd.date_range('2026-01-01 08:00', periods=len(data), freq='h')
        params.update(hand_params(name))
        params.pop('stop_loss_pct')
        params.pop('take_profit_pct')
        if name.startswith('rsi'):
            params['rsi_exit_above'] = 100
            data.iloc[27, 1] = 150
    if name in SHORTPAT2_BASELINE['single']:
        from test_short_pat2 import hand_frame, hand_params
        data = hand_frame()
        data.index = pd.date_range('2026-01-01 08:00', periods=len(data), freq='h')
        params.update(hand_params(name))
        params.pop('stop_loss_pct')
        params.pop('take_profit_pct')
        if name.startswith('rsi'):
            params['rsi_exit_below'] = 0
            data.iloc[27, 2] = 50
    if name == 'ichimoku_cloud_breakout':
        params.update(tenkan_period=5, kijun_period=10, senkou_b_period=20)
    if name == 'chan_3buy':
        from test_9t6_chan_3buy import frame as chan_frame
        data = chan_frame()
        data.index = pd.date_range('2026-01-01 18:00', periods=len(data), freq='h')
        params.pop('stop_loss_pct')
        params.pop('take_profit_pct')
    if name == 'fibonacci_retracement':
        from test_9t5_fibonacci import frame as fibonacci_frame
        data = fibonacci_frame()
        data.loc[data.index[10], ['High', 'Close']] = [117, 116]
        params['swing_n'] = 2
    tf = '1h'
    if name == 'us_open_momentum':
        data.index = pd.date_range('2026-01-01', periods=len(data), freq='15min')
        params.update(direction='long', time_timezone='America/New_York', time_session_start='09:30', time_session_end='10:30')
        tf = '15m'
    if name == 'cme_weekend_gap':
        params.update(direction='long', time_session_start='21:00', time_session_end='23:30')
    cls = provider.TOOL_SPECS['local.backtesting_py.' + compat.tool_name(name)]['build'](params)['strategy']
    ctx = TimeContext.build(cls._time_config, tf, data.index)
    cls._time_context = ctx
    trades = Backtest(data, cls, cash=100000, exclusive_orders=True, finalize_trades=True).run()['_trades']
    assert len(trades) > 0, name
    assert all(ctx.allow_entry(value) for value in trades.EntryTime), name
    assert any(not ctx.allow_entry(value) for value in trades.ExitTime), name


@pytest.mark.parametrize('side', ['long', 'short'])
def test_entry_uses_close_boundary_and_exit_is_unrestricted(side):
    data = pd.DataFrame(dict(Open=[100]*9, High=[101]*9, Low=[90]*9, Close=[94]*9, Volume=[1]*9),
                        index=pd.date_range('2026-01-01', periods=9, freq='h'))
    class Manual(provider._FixedRiskMixin, Strategy):
        _risk = {'stop_loss_pct': .03}
        _time_config = TimeConfig.parse(dict(time_layer_enabled=True, time_session_start='02:00', time_session_end='03:00'))
        _time_context = TimeContext.build(_time_config, '1h', data.index)
        def init(self):
            self._risk_init()
        def next(self):
            if self.position:
                if self._risk_check_exit():
                    return
                self.position.close()
            else:
                (self._risk_buy if side == 'long' else self._risk_sell)()
    trades = Backtest(data, Manual, cash=10000, finalize_trades=True).run()['_trades']
    assert len(trades) == 1
    assert trades.iloc[0].EntryBar == 2
    assert trades.iloc[0].ExitBar == 3


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(provider, 'AUTH_TOKEN', '')
    def forbidden(*args, **kwargs):
        raise AssertionError('invalid params must fail before fetching market data')
    monkeypatch.setattr(provider, '_fetch_ohlcv', forbidden)
    monkeypatch.setattr(provider, '_fetch_template_warmup', forbidden)
    return TestClient(provider.app)


@pytest.mark.parametrize('params', BAD_PARAMS + [
    {'time_weekdays': 1}, {'time_timezone': 'Asia/Tokyo'},
    {'time_layer_enabled': False, 'time_session_start': '06:00', 'time_session_end': '10:00'},
])
def test_http_invalid_time_before_fetch(client, params):
    if 'time_layer_enabled' not in params and params not in ({'time_weekdays': 1}, {'time_timezone': 'Asia/Tokyo'}):
        params = {'time_layer_enabled': True, **params}
    response = client.post('/cutie/backtest', json=capture.request_body('ema_cross', params))
    assert response.status_code == 200
    assert response.json()['error_type'] == 'INVALID_PARAMS'


EXCLUDED = [name.removeprefix('local.backtesting_py.') for name, spec in provider.TOOL_SPECS.items()
            if spec.get('runner') == 'kernel_v3']


@pytest.mark.parametrize('name', EXCLUDED)
@pytest.mark.parametrize('key,value', list(DEFAULTS.items()) + [('time_calendar', 'none')])
def test_unwired_templates_reject_even_default_time_keys(client, name, key, value):
    body = capture.request_body(name, {key: value})
    body['backtest']['market'] = 'spot' if name in capture.LEDGER_PARAMS else 'futures'
    response = client.post('/cutie/backtest', json=body)
    assert response.json()['error_type'] == 'INVALID_PARAMS'
    assert key not in provider.TOOL_SPECS['local.backtesting_py.' + name]['param_schema_properties']


def test_http_enabled_assumptions_and_gap(monkeypatch):
    params = dict(time_layer_enabled=True, time_session_start='06:00', time_session_end='10:00')
    body = capture.response('ema_cross', params)
    assert body['trades']
    assert all(6 <= datetime.fromtimestamp(trade['opened_at'], timezone.utc).hour < 10 for trade in body['trades'])
    layer = body['assumptions']['time_layer']
    assert layer == dict(timezone='UTC', tzdata_version=layer['tzdata_version'], session_start='06:00',
        session_end='10:00', weekdays=127, decision_time='bar_close', gate='entry_only', fill='next_bar_open')
    assert layer['tzdata_version']
    monkeypatch.setattr(provider, 'AUTH_TOKEN', '')
    monkeypatch.setattr(provider, '_fetch_ohlcv', lambda *a: compat.frame().iloc[60:].drop(compat.frame().index[100]))
    monkeypatch.setattr(provider, '_fetch_template_warmup', lambda *a: pytest.fail('gap must fail before warmup'))
    response = TestClient(provider.app).post('/cutie/backtest', json=capture.request_body('ema_cross', params))
    assert response.json()['error_type'] == 'TIME_DATA_GAP'


def test_request_clocks_are_isolated_and_disabled_build_stays_empty():
    build = provider.TOOL_SPECS['local.backtesting_py.ema_cross']['build']
    first = build({'time_layer_enabled': True})['strategy']
    second = build({'time_layer_enabled': True, 'time_timezone': 'Asia/Tokyo'})['strategy']
    disabled = build(DEFAULTS)['strategy']
    first._time_context = TimeContext.build(first._time_config, '1h', compat.frame().index)
    assert second._time_context is None
    assert first._time_config.timezone_name == 'UTC'
    assert second._time_config.timezone_name == 'Asia/Tokyo'
    assert disabled._time_config is None
    assert disabled._time_context is None


def test_no_new_entry_on_last_bar_without_next_open():
    data = compat.frame().iloc[:5]
    class LastBar(provider._FixedRiskMixin, Strategy):
        _time_config = TimeConfig.parse({'time_layer_enabled': True})
        _time_context = TimeContext.build(_time_config, '1h', data.index)
        def init(self):
            self._risk_init()
        def next(self):
            if len(self.data) == len(data):
                self._risk_buy()
    trades = Backtest(data, LastBar, cash=10000, finalize_trades=True).run()['_trades']
    assert trades.empty


def test_http_unsupported_timeframe_before_fetch(client):
    body = capture.request_body('ema_cross', {'time_layer_enabled': True})
    body['backtest']['timeframe'] = '1M'
    response = client.post('/cutie/backtest', json=body)
    assert response.json()['error_type'] == 'INVALID_PARAMS'


@pytest.mark.parametrize('name', compat.enumerate_turtle_cases())
@pytest.mark.parametrize('direction', ['long', 'short', 'both'])
def test_turtle_group_time_exemption_preserves_disabled_golden(name, direction):
    from test_turtle_risk_3b import assert_disabled_golden
    assert_disabled_golden(name, direction)
    props = provider.TOOL_SPECS['local.backtesting_py.' + name]['param_schema_properties']
    assert not any(key.startswith('time_') for key in props)


@pytest.mark.parametrize('name', compat.enumerate_turtle_cases())
@pytest.mark.parametrize('key,value', list(DEFAULTS.items()) + [
    ('time_max_holding_minutes', 1), ('time_flatten_at', '12:00'), ('time_flatten_weekdays', 1)])
def test_turtle_time_keys_rejected_even_with_group_risk(client, name, key, value):
    body = capture.request_body(name, {'risk_layer_enabled': True, key: value})
    body['backtest']['market'] = 'futures'
    assert client.post('/cutie/backtest', json=body).json()['error_type'] == 'INVALID_PARAMS'


@pytest.mark.parametrize('name', sorted(F1_CASES))
@pytest.mark.parametrize('warm', [False, True])
def test_f1_disabled_fingerprints(monkeypatch, tmp_path, name, warm):
    from test_f1_range_breakout import market_frame, http_run
    data = market_frame(count=100 if name == 'asia_range_breakout' else 16,
                        signal=28 if name == 'asia_range_breakout' else 4)
    start = data.index[8] if warm else None
    absent = http_run(monkeypatch, tmp_path, data, tool=name, start=start)
    explicit = http_run(monkeypatch, tmp_path, data, tool=name, params=DEFAULTS, start=start)
    assert absent['trades']
    for key in (*capture.V2_KEYS, 'assumptions', 'raw_report'):
        assert capture.digest(absent[key]) == capture.digest(explicit[key]), key
    assert 'time_layer' not in explicit['assumptions']
    assert not any(key.startswith('time_') for trade in explicit['trades'] for key in trade)
    assert explicit['assumptions']['indicator_warmup_bars'] == 0
    assert F1_CASES <= set(MIXINS)


@pytest.mark.parametrize('configured', [False, True])
def test_f2_disabled_bytes_and_no_optional_clock(monkeypatch, tmp_path, configured):
    from test_f2_calendar import frame, http_run, SCHEDULE
    data = frame()
    params = SCHEDULE if configured else {}
    absent = http_run(monkeypatch, tmp_path, data, params)
    original = TimeContext.build.__func__
    def intrinsic_only(cls, config, *args, **kwargs):
        assert config.enabled and config.session_start == '' and config.session_end == ''
        return original(cls, config, *args, **kwargs)
    monkeypatch.setattr(TimeContext, 'build', classmethod(intrinsic_only))
    explicit = http_run(monkeypatch, tmp_path, data, {**DEFAULTS, **params})
    assert absent['result_status'] == explicit['result_status'] == 'success'
    assert bool(absent['trades']) == configured
    for key in (*capture.V2_KEYS, 'assumptions', 'raw_report'):
        assert capture.digest(absent[key]) == capture.digest(explicit[key]), key
    assert 'time_layer' not in explicit['assumptions']
    assert not any(key.startswith('time_') for trade in explicit['trades'] for key in trade)
    assert explicit['assumptions']['indicator_warmup_bars'] == 0
    assert F2_CASES <= set(MIXINS)


@pytest.mark.parametrize("warm", [False, True])
def test_f6_disabled_time_bytes_unchanged(warm):
    omitted = capture.response("red_streak_rsi", {}, warm)
    disabled = capture.response("red_streak_rsi", DEFAULTS, warm)
    assert omitted["trades"]  # Off-state comparison must exercise actual fills.
    for key in (*capture.V2_KEYS, "assumptions", "raw_report"):
        assert capture.digest(omitted[key]) == capture.digest(disabled[key])
    assert "time_layer" not in disabled["assumptions"]
