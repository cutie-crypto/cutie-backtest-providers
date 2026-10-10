"""TURTLE-TIME: turtle group runner consumes the shared 9-key time layer.

Gate: a new group's first unit and every add both pass the entry gate (both raise
exposure); a blocked breakout is dropped, never queued. Holding minutes / flatten
cutoffs count from the group's first fill and close the whole group as
time_expiry; a same-bar group stop still wins.
"""
from __future__ import annotations
import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest
from backtesting import Backtest
from fastapi.testclient import TestClient

import cutie_backtesting_provider as p
from strategy_time_layer import TimeContext, _TIME_PARAM_SCHEMA_PROPERTIES

_spec = importlib.util.spec_from_file_location(
    'turtle_time_capture', Path(__file__).parent / 'fixtures/capture_turtle_time_off_4d29dfa.py')
capture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(capture)
BASELINE = json.loads(capture.FIXTURE.read_text())
TOOL = 'local.backtesting_py.turtle'
# 4d29dfa turtle schema, before the time layer was wired.
PRE_TIME_KEYS = {'risk_layer_enabled', 'max_holding_bars', 'take_profit_pct', 'entry_period', 'exit_period',
                 'atr_period', 'unit_risk_pct', 'add_step_atr', 'max_units', 'stop_atr_multiplier',
                 'direction', 'exchange'}
FLAT = [100, 101, 99, 100]
MONDAY = '2026-01-05'  # bar k opens at k:00 UTC; its decision time is (k+1):00


def frame(rows, start=MONDAY):
    return pd.DataFrame(rows, columns=['Open', 'High', 'Low', 'Close'], dtype=float,
                        index=pd.date_range(start, periods=len(rows), freq='h', tz=None)).assign(Volume=1)


def breakout_rows(after, tail=4):
    """Bars 0-9 flat, bar 10 closes above the prior 3-bar high, then `after` repeated."""
    return [FLAT] * 10 + [[100, 104, 99, 103]] + [after] * tail


def add_rows(tail=5):
    """Breakout at bar 10 (fill bar 11 open 103); bar 11 closes far above the fill -> add signal."""
    return [FLAT] * 10 + [[100, 104, 99, 103], [103, 110, 103, 110]] + [[110, 111, 109, 110]] * tail


def run(data, params, finalize=True):
    cls = p._build_turtle({'entry_period': 3, 'exit_period': 2, 'atr_period': 2, **params})['strategy']
    if cls._time_config is not None:
        cls._time_context = TimeContext.build(cls._time_config, '1h', data.index)
    stats = Backtest(data, cls, cash=100000, exclusive_orders=False, finalize_trades=finalize).run()
    trades = stats['_trades'].sort_values(['EntryBar']).reset_index(drop=True)
    return stats['_strategy'], cls, trades


# ---- off state: complete responses byte-identical to 4d29dfa ----

def test_baseline_fixture_is_pinned_to_4d29dfa():
    assert BASELINE['baseline_sha'] == capture.BASELINE_SHA
    assert set(BASELINE['responses']) == set(capture.CASES)


@pytest.mark.parametrize('case', sorted(capture.CASES))
def test_omitted_time_keys_response_byte_identical(case):
    assert capture.response(case) == BASELINE['responses'][case]


# ---- schema ----

def test_turtle_schema_gains_exactly_the_nine_time_keys():
    props = p.TOOL_SPECS[TOOL]['param_schema_properties']
    assert len(_TIME_PARAM_SCHEMA_PROPERTIES) == 9
    assert set(props) == PRE_TIME_KEYS | set(_TIME_PARAM_SCHEMA_PROPERTIES)
    for key, spec in _TIME_PARAM_SCHEMA_PROPERTIES.items():
        assert props[key] == spec
    for key in p._TURTLE_RISK_KEYS:
        assert props[key] == p._FIXED_RISK_PARAM_SCHEMA_PROPERTIES[key]
    assert not set(props) & (set(p._LEVERAGE_PARAM_SCHEMA_PROPERTIES) | set(p.POSITION_SIZE_KEYS))
    assert not any(key.startswith('filter_') for key in props)


@pytest.mark.parametrize('params', [
    {'time_session_start': '06:00', 'time_session_end': '10:00'},  # not enabled
    {'time_layer_enabled': True, 'time_session_start': '06:00'},  # unpaired
    {'time_layer_enabled': True, 'time_stop_enabled': True},  # unknown time key
    {'time_layer_enabled': True, 'leverage': 2},
    {'time_layer_enabled': True, 'filter_layer_enabled': True},
    {'time_layer_enabled': True, 'stop_loss_pct': 3},
])
def test_invalid_time_or_unwired_keys_rejected_before_fetch(monkeypatch, params):
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a: pytest.fail('fetch must not be called'))
    body = json.loads(json.dumps({'backtest': dict(run_id='tt', provider_tool_id=TOOL, provider_params=params,
        symbol='BTCUSDT', market='futures', timeframe='1h', start_at=1767225600, end_at=1767272400,
        initial_capital='100000', fee_bps='0', slippage_bps='0')}))
    assert TestClient(p.app).post('/cutie/backtest', json=body).json()['error_type'] == 'INVALID_PARAMS'


# ---- entry gate ----

def test_session_blocks_first_unit_and_breakout_is_not_queued():
    data = frame(breakout_rows([103, 103.5, 102.5, 103], tail=6))
    _, _, control = run(data, {'max_units': 1, 'time_layer_enabled': True,
                               'time_session_start': '11:00', 'time_session_end': '23:00'})
    assert list(control.EntryBar) == [11]
    strategy, _, blocked = run(data, {'max_units': 1, 'time_layer_enabled': True,
                                      'time_session_start': '12:00', 'time_session_end': '23:00'})
    # Bar 10 decides at 11:00 (outside); later bars are inside but no longer breakouts.
    assert blocked.empty and strategy._group_sequence == 0


def test_session_blocks_add_unit():
    data = frame(add_rows())
    _, _, allowed = run(data, {'max_units': 2, 'time_layer_enabled': True,
                               'time_session_start': '11:00', 'time_session_end': '13:00'})
    assert list(allowed.EntryBar) == [11, 12]
    assert set(allowed.Tag) == {'turtle-1'}
    strategy, _, blocked = run(data, {'max_units': 2, 'time_layer_enabled': True,
                                      'time_session_start': '11:00', 'time_session_end': '12:00'})
    # Bar 11 add decides at 12:00 (end-exclusive) -> blocked; every later add decision is outside too.
    assert list(blocked.EntryBar) == [11]
    assert strategy._group_sequence == 1 and strategy.units_skipped == 0


@pytest.mark.parametrize('start,weekdays,expected', [
    (MONDAY, 1, [11]),           # Monday only
    (MONDAY, 126, []),           # every day except Monday
    ('2026-01-03', 31, []),      # Saturday data, Mon-Fri mask
    ('2026-01-03', 32, [11]),    # Saturday only
])
def test_weekday_mask_and_weekend(start, weekdays, expected):
    data = frame(breakout_rows([103, 103.5, 102.5, 103], tail=6), start=start)
    _, _, trades = run(data, {'max_units': 1, 'time_layer_enabled': True, 'time_weekdays': weekdays})
    assert list(trades.EntryBar) == expected


def test_enabled_layer_without_gates_matches_disabled_run():
    data = frame(add_rows())
    _, _, off = run(data, {'max_units': 2})
    _, _, on = run(data, {'max_units': 2, 'time_layer_enabled': True})
    # No breakout or add on the last bar here, so an ungated enabled layer changes nothing.
    assert off[['EntryBar', 'ExitBar', 'EntryPrice', 'Size']].equals(on[['EntryBar', 'ExitBar', 'EntryPrice', 'Size']])


def test_enabled_layer_never_enters_on_last_bar():
    # The last bar (10) is a breakout with no next open. Layer off: the group is opened and its
    # market order stays queued (unfillable). Layer on: the shared last-bar guard refuses the entry,
    # so no group and no order exist.
    data = frame([FLAT] * 10 + [[100, 104, 99, 103]])
    off, _, off_trades = run(data, {'max_units': 1}, finalize=False)
    assert off_trades.empty and off._group_sequence == 1 and len(off.orders) == 1
    on, _, on_trades = run(data, {'max_units': 1, 'time_layer_enabled': True}, finalize=False)
    assert on_trades.empty and on._group_sequence == 0 and len(on.orders) == 0


def test_blocked_add_is_not_reissued_when_gate_reopens():
    # Overnight session 13:00-12:00 blocks only the 12:00 decision (bar 11, the add signal);
    # from bar 12 (decision 13:00) the gate is open again but price is back below the add step.
    rows = ([FLAT] * 10 + [[100, 104, 99, 103], [103, 110, 103, 110]]
            + [[104, 104.5, 103.5, 104]] * 4)
    data = frame(rows)
    _, _, allowed = run(data, {'max_units': 2, 'time_layer_enabled': True})
    assert list(allowed.EntryBar) == [11, 12]
    strategy, _, blocked = run(data, {'max_units': 2, 'time_layer_enabled': True,
                                      'time_session_start': '13:00', 'time_session_end': '12:00'})
    assert list(blocked.EntryBar) == [11]
    assert strategy._group_sequence == 1 and strategy.units_skipped == 0


# ---- whole-group holding expiry ----

def test_max_holding_minutes_closes_whole_group_from_first_fill():
    data = frame(add_rows(tail=6))
    strategy, _, trades = run(data, {'max_units': 2, 'time_layer_enabled': True,
                                     'time_max_holding_minutes': 180}, finalize=False)
    # First fill opens 11:00; bar 13 decides at 14:00 (180 min) -> both units exit at bar 14 open.
    assert list(trades.EntryBar) == [11, 12]
    assert list(trades.ExitBar) == [14, 14]
    assert strategy._group_exit_reasons == {'turtle-1': 'time_expiry'}


def test_flatten_at_closes_whole_group_and_records_delay():
    data = frame(add_rows(tail=6))
    strategy, cls, trades = run(data, {'max_units': 2, 'time_layer_enabled': True,
                                       'time_flatten_at': '12:30'}, finalize=False)
    # Cutoff 12:30 falls inside bar 12 (decision 13:00): close at bar 13 open, delay 0.5 bar.
    assert list(trades.EntryBar) == [11, 12]
    assert list(trades.ExitBar) == [13, 13]
    assert strategy._group_exit_reasons == {'turtle-1': 'time_expiry'}
    assert cls._time_context.flatten_delays == [0.5]


def test_flatten_at_respects_local_timezone():
    data = frame(add_rows(tail=6))
    # 20:30 Asia/Shanghai == 12:30 UTC.
    strategy, _, trades = run(data, {'max_units': 2, 'time_layer_enabled': True,
                                     'time_timezone': 'Asia/Shanghai', 'time_flatten_at': '20:30'},
                              finalize=False)
    assert list(trades.ExitBar) == [13, 13]


def test_same_bar_group_stop_beats_time_expiry():
    rows = [FLAT] * 10 + [[100, 104, 99, 103], [103, 104, 102.5, 103.5], [103.5, 104, 90, 95]] + [FLAT] * 3
    data = frame(rows)
    # Holding due at bar 12 (decision 13:00, 120 min after the 11:00 fill) and bar 12 breaks the stop.
    strategy, _, trades = run(data, {'max_units': 1, 'time_layer_enabled': True,
                                     'time_max_holding_minutes': 120}, finalize=False)
    assert list(trades.ExitBar) == [13]
    assert strategy._group_exit_reasons == {'turtle-1': 'stop'}
    _, _, no_stop = run(frame(rows[:12] + [[103.5, 104, 103, 103.5]] + [FLAT] * 3),
                        {'max_units': 1, 'time_layer_enabled': True, 'time_max_holding_minutes': 120},
                        finalize=False)
    assert list(no_stop.ExitBar) == [13]


# ---- HTTP: assumptions and raw report ----

def http(monkeypatch, tmp_path, data, params):
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a: data.copy())
    monkeypatch.setattr(p, '_fetch_template_warmup', lambda *a: data.iloc[:0].copy())
    monkeypatch.setattr(p, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **k: None)
    body = {'backtest': dict(run_id='turtle_time', provider_tool_id=TOOL,
        provider_params={'entry_period': 3, 'exit_period': 2, 'atr_period': 2, **params},
        symbol='BTCUSDT', market='futures', timeframe='1h',
        start_at=int(data.index[0].timestamp()), end_at=int(data.index[-1].timestamp()) + 3600,
        initial_capital='100000', fee_bps='0', slippage_bps='0')}
    result = TestClient(p.app).post('/cutie/backtest', json=body).json()
    assert result['result_status'] == 'success', result
    return result


@pytest.mark.parametrize('risk', [False, True])
def test_http_time_layer_assumptions_and_group_reason(monkeypatch, tmp_path, risk):
    params = {'max_units': 2, 'time_layer_enabled': True, 'time_flatten_at': '12:30'}
    if risk:
        params.update(risk_layer_enabled=True, max_holding_bars=50)
    off = http(monkeypatch, tmp_path, frame(add_rows(tail=6)), {'max_units': 2})
    result = http(monkeypatch, tmp_path, frame(add_rows(tail=6)), params)
    layer = result['assumptions']['time_layer']
    assert {k: v for k, v in layer.items() if k not in ('tzdata_version', 'holding')} == dict(
        timezone='UTC', session_start='', session_end='', weekdays=127,
        decision_time='bar_close', gate='entry_and_add', fill='next_bar_open')
    assert layer['holding']['bars'] == (50 if risk else 0)
    assert layer['holding']['flatten_at'] == '12:30'
    assert layer['holding']['flatten_delay_bars']['count'] == 1
    assert result['assumptions']['turtle_time_layer']['gate'] == 'entry_and_add'
    assert ('turtle_risk' in result['assumptions']) is risk
    assert result['raw_report']['turtle_groups'][0] == dict(
        group_id='turtle-1', trade_seqs=[1, 2], units=2, exit_reason='time_expiry')
    assert set(result['metrics']) == set(off['metrics'])
    assert all(set(t) == set(off['trades'][0]) for t in result['trades'])


def test_http_time_data_gap(monkeypatch, tmp_path):
    data = frame(add_rows(tail=6)).drop(frame(add_rows(tail=6)).index[5])
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a: data.copy())
    monkeypatch.setattr(p, '_fetch_template_warmup', lambda *a: data.iloc[:0].copy())
    monkeypatch.setattr(p, 'REPORTS_DIR', tmp_path)
    body = {'backtest': dict(run_id='turtle_time', provider_tool_id=TOOL,
        provider_params={'entry_period': 3, 'exit_period': 2, 'atr_period': 2, 'time_layer_enabled': True},
        symbol='BTCUSDT', market='futures', timeframe='1h',
        start_at=int(data.index[0].timestamp()), end_at=int(data.index[-1].timestamp()) + 3600,
        initial_capital='100000', fee_bps='0', slippage_bps='0')}
    assert TestClient(p.app).post('/cutie/backtest', json=body).json()['error_type'] == 'TIME_DATA_GAP'
