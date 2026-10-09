"""Round-fill clock, entry-only gates, callbacks and immutable result.v2."""
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path
import sys

import pandas as pd
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p
import test_time_layer_compatibility as compat
from scale_in_out_ledger import LedgerBar, run_scale_in_out
from strategy_time_layer import TimeConfig, TimeContext, _TIME_PARAM_SCHEMA_PROPERTIES
from test_time_layer import BAD_PARAMS, DEFAULTS

PARAMS = compat.capture.LEDGER_PARAMS


def bars(closes=None, *, start='2026-01-01T00:00:00+00:00', count=12):
    closes = closes if closes is not None else [100]*count
    ts = int(datetime.fromisoformat(start).timestamp())
    return [LedgerBar(ts+i*3600, ts+(i+1)*3600, D(100), D(str(close)))
            for i, close in enumerate(closes)]


def clock(rows, **params):
    return TimeContext.build(TimeConfig.parse(dict(time_layer_enabled=True, **params)),
                             '1h', [datetime.fromtimestamp(b.open_time, timezone.utc) for b in rows])


def run(rows=None, actions=None, *, ctx=None, n=0, signal=None, on_fill=None, cash=10000, **params):
    rows = rows if rows is not None else bars()
    ctx = ctx if ctx is not None else clock(rows, **params)
    actions = actions if actions is not None else {0: 'buy'}
    return run_scale_in_out(rows, signal or (lambda i: actions.get(i, 'hold')),
        initial_capital=D(cash), buy_notional=D(100), sell_notional=D(50),
        fee_bps=D(0), slippage_bps=D(0), start_at=rows[0].open_time,
        end_at=rows[-1].close_time, on_fill=on_fill, time_context=ctx, max_holding_bars=n)


def build(name, extra):
    return p.TOOL_SPECS['local.backtesting_py.'+name]['build']({**PARAMS[name], **extra})


@pytest.mark.parametrize('name', PARAMS)
def test_schema_and_defaults(name):
    schema = p.TOOL_SPECS['local.backtesting_py.'+name]['param_schema_properties']
    assert {k for k in schema if k.startswith('time_')} == set(_TIME_PARAM_SCHEMA_PROPERTIES)
    assert schema['max_holding_bars'] == p._FIXED_RISK_PARAM_SCHEMA_PROPERTIES['max_holding_bars']
    original = build(name, {})
    disabled = build(name, {**DEFAULTS, 'max_holding_bars': 0})
    assert original['min_bars'] == disabled['min_bars']
    assert 'time_config' not in disabled['scale_in_out']
    enabled = build(name, {'time_layer_enabled': True})['scale_in_out']
    assert enabled['max_holding_bars'] == 0
    assert enabled['time_config'] == TimeConfig(enabled=True)


@pytest.mark.parametrize('name', PARAMS)
@pytest.mark.parametrize('extra', BAD_PARAMS + [
    {'max_holding_bars': v} for v in (-1, 1000001, True, False, 1.0, 0.0, '3', None)
] + [
    {'time_reset_period': 'day'}, {'time_reset_at': '00:00'}, {'time_range_start': '00:00'},
    {'time_range_end': '01:00'}, {'time_vwap_price': 'hlc3'}, {'time_entry_at': '12:00'},
    {'time_entry_weekday': 1}, {'time_entry_monthday': 1}, {'time_calendar': 'none'},
    {'time_flatten_weekdays': 1}, {'time_flatten_weekdays': 1.0},
    {'time_flatten_at': '24:00'}, {'time_flatten_at': 1200},
])
def test_strict_invalid_build(name, extra):
    with pytest.raises(ValueError, match='INVALID_PARAMS:'):
        build(name, {'time_layer_enabled': True, **extra})


@pytest.mark.parametrize('name', PARAMS)
@pytest.mark.parametrize('enabled', [False, None])
def test_holding_limit_requires_time_gate(name, enabled):
    extra = dict(max_holding_bars=3)
    if enabled is not None:
        extra['time_layer_enabled'] = enabled
    with pytest.raises(ValueError, match='max_holding_bars requires risk_layer_enabled=true'):
        build(name, extra)


@pytest.mark.parametrize('name', PARAMS)
@pytest.mark.parametrize('n', [0, 1, 1000000])
def test_holding_boundaries(name, n):
    assert build(name, dict(time_layer_enabled=True, max_holding_bars=n))['scale_in_out']['max_holding_bars'] == n


@pytest.mark.parametrize('action', ['sell', ('sell_lot',), 'sell_all'])
def test_entry_gate_never_blocks_sells(action):
    result = run(actions={0:'buy', 1:action}, time_session_start='01:00', time_session_end='02:00')
    assert result.trades
    assert result.trades[0]['closed_at'] == bars()[2].open_time
    assert result.time_expiry_fills == 0


def test_blocked_buy_is_not_insufficient_cash():
    calls = []
    result = run(actions={0:'buy'}, cash=1, on_fill=lambda *a, **kw: calls.append((a, kw)),
                 time_session_start='02:00', time_session_end='03:00')
    assert result.buy_fills == 0
    assert result.skipped_buys_insufficient_cash == 0
    assert calls[0][1] == {'reason': 'time_entry_blocked'}
    assert calls[0][0][:3] == (0, 'buy', False)


@pytest.mark.parametrize('end_on_open', [False, True])
def test_tail_never_evaluates_unfillable_entry(end_on_open):
    rows = bars(count=3)
    seen = []
    result = run_scale_in_out(rows, lambda i: seen.append(i) or ('buy' if i >= 1 else 'hold'),
        initial_capital=D(10000), buy_notional=D(100), sell_notional=D(100), fee_bps=D(0),
        slippage_bps=D(0), start_at=rows[0].open_time,
        end_at=rows[-1].open_time if end_on_open else rows[-1].close_time,
        time_context=clock(rows))
    assert seen == ([0] if end_on_open else [0,1])
    assert result.buy_fills == (0 if end_on_open else 1)


@pytest.mark.parametrize('n', [2, 3, 4])
def test_round_clock_first_actual_fill_n_minus_n_plus(n):
    result = run(n=n)
    assert result.trades[0]['opened_at'] == bars()[1].open_time
    assert result.trades[0]['closed_at'] == bars()[n+1].open_time
    assert result.time_expiry_fills == 1


@pytest.mark.parametrize('partial', ['sell', ('sell_lot',)])
def test_add_and_partial_sell_never_reset_round_clock(partial):
    result = run(actions={0:'buy', 1:'buy', 2:partial, 3:'buy'}, n=3)
    assert result.buy_fills == 2
    assert result.trades[-1]['closed_at'] == bars()[4].open_time
    assert result.time_expiry_fills == 1
    assert result.snapshots[3].lot_qty_sum > 0


def test_failed_buy_does_not_start_clock():
    result = run(actions={0:('buy', D(20000)), 2:'buy'}, n=3)
    assert result.skipped_buys_insufficient_cash == 1
    assert result.trades[0]['opened_at'] == bars()[3].open_time
    assert result.trades[0]['closed_at'] == bars()[6].open_time


@pytest.mark.parametrize('start', ['2026-03-08T06:00:00+00:00', '2026-11-01T04:00:00+00:00'])
def test_minutes_use_actual_fill_utc_across_dst(start):
    rows = bars(start=start)
    result = run(rows, time_timezone='America/New_York', time_max_holding_minutes=120)
    assert result.trades[0]['closed_at'] == rows[3].open_time
    assert result.trades[0]['closed_at']-result.trades[0]['opened_at'] == 7200


def test_flatten_mid_bar_records_delay():
    rows = bars(start='2026-01-01T08:00:00+00:00')
    ctx = clock(rows, time_flatten_at='10:30')
    result = run(rows, ctx=ctx)
    assert result.trades[0]['closed_at'] == rows[3].open_time
    assert ctx.flatten_delays == [0.5]


@pytest.mark.parametrize('action', ['buy', 'sell', ('sell_lot',), 'sell_all'])
def test_expiry_overrides_template_and_reason(action):
    calls, decisions = [], []
    def signal(i):
        decisions.append(i)
        return {0:'buy', 2:action}.get(i, 'hold')
    def on_fill(i, a, filled, price, *, reason=None):
        calls.append((i,a,filled,reason))
    result = run(n=2, signal=signal, on_fill=on_fill)
    assert 2 not in decisions
    assert calls == [(1,'buy',True,None),(3,'sell_all',True,'time_expiry')]
    assert result.time_expiry_fills == 1


def test_legacy_four_argument_callback_still_runs_with_time():
    calls = []
    run(n=2, on_fill=lambda i,a,filled,price: calls.append((i,a,filled)))
    assert calls == [(1,'buy',True),(3,'sell_all',True)]


def test_next_round_starts_new_first_fill_clock():
    result = run(actions={0:'buy', 4:'buy', 5:'buy'}, n=3)
    assert result.time_expiry_fills == 2
    assert [t['closed_at'] for t in result.trades] == [bars()[4].open_time, bars()[8].open_time, bars()[8].open_time]


def test_end_liquidation_has_no_callback_or_expiry_count():
    calls = []
    result = run(n=100, on_fill=lambda *a, **kw: calls.append((a,kw)))
    assert result.trades[0]['closed_at'] == bars()[-1].close_time
    assert len(calls) == 1
    assert result.time_expiry_fills == 0


def template_run(name, rows, *, n=0, **extra):
    config = build(name, dict(time_layer_enabled=True, **extra))['scale_in_out']
    ctx = clock(rows, **extra)
    signal, callback = config['signal_factory'](rows, time_context=ctx)
    result = run(rows, n=n, ctx=ctx, signal=signal, on_fill=callback)
    return result, config['extra_assumptions'](result)


def test_grid_expiry_is_not_stop_loss_and_resets_round():
    rows = bars([130,110,100,90,100,90,85,85,85,85])
    result, stats = template_run('grid', rows, n=2)
    assert result.time_expiry_fills >= 2
    assert stats['stop_loss_triggered'] == 0
    assert result.buy_fills >= 2


def test_grid_blocked_buy_restores_reference_level():
    rows = bars([110,104,104,104,104,104])
    result, stats = template_run('grid', rows, time_session_start='03:00', time_session_end='04:00')
    assert result.buy_fills == 1
    assert result.trades[0]['opened_at'] == rows[3].open_time
    assert result.skipped_buys_insufficient_cash == 0


def test_dca_blocked_dip_restores_attempt_and_expiry_clears_round():
    rows = bars([100,90,90,90,90,90], start='2026-01-01T23:00:00+00:00')
    config = build('dca', dict(time_layer_enabled=True, max_dip_adds=1))['scale_in_out']
    ctx = clock(rows)
    signal, callback = config['signal_factory'](rows, time_context=ctx)
    assert signal(0)[0] == 'buy'
    callback(1, 'buy', True, D(100))
    assert signal(1)[0] == 'buy'
    callback(1, 'buy', False, D(90), reason='time_entry_blocked')
    assert signal(2)[0] == 'buy'
    callback(3, 'buy', True, D(100))
    callback(4, 'sell_all', True, D(100), reason='time_expiry')
    assert signal(4) == 'hold'
    stats = config['extra_assumptions'](run(rows, actions={}))
    assert stats['rounds_completed'] == 1
    assert stats['dip_adds_total'] == 1


@pytest.mark.parametrize('interval', ['daily','weekly'])
def test_dca_enabled_calendar_uses_local_period(interval):
    rows = bars(count=5, start='2026-01-04T14:00:00+00:00')
    config = build('dca', dict(time_layer_enabled=True, interval=interval))['scale_in_out']
    local = clock(rows, time_timezone='Asia/Tokyo')
    signal, _ = config['signal_factory'](rows, time_context=local)
    assert signal(0)[0] == 'buy'
    legacy, _ = config['signal_factory'](rows)
    assert legacy(0) == 'hold'


@pytest.mark.parametrize('name', PARAMS)
def test_real_runner_holding_three_bars_and_v2_contract(name):
    body = compat.capture.response(name, dict(PARAMS[name], time_layer_enabled=True, max_holding_bars=3))
    assert body['raw_report']['time_expiry_fills'] >= 1
    assert body['assumptions']['time_layer']['holding']['clock'] == 'round_first_fill'
    assert set(body['metrics']) == {'total_return','max_drawdown','trade_count'}
    data = compat.compat.frame().iloc[60:]
    for trade in body['trades']:
        assert set(trade) == {'seq','opened_at','closed_at','side','qty','entry_price','exit_price','fee','slippage','pnl'}
        assert (trade['closed_at']-trade['opened_at']) // 3600 <= 3
        if trade['closed_at'] < int(data.index[-1].timestamp())+3600:
            bar = data.loc[pd.Timestamp(trade['closed_at'], unit='s')]
            assert D(str(bar.Low)) <= D(trade['exit_price']) <= D(str(bar.High))
            assert D(trade['exit_price']) == D(str(float(bar.Open)))
    if name == 'grid':
        assert body['assumptions']['stop_loss_triggered'] == 0


@pytest.mark.parametrize('name', PARAMS)
def test_disabled_ledger_has_no_clock_or_new_report_keys(name, monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail('disabled ledger constructed a time clock')
    monkeypatch.setattr(TimeContext, 'build', forbidden)
    expected = compat.capture.response(name, PARAMS[name])
    actual = compat.capture.response(name, dict(PARAMS[name], **DEFAULTS, max_holding_bars=0))
    for key in (*compat.capture.V2_KEYS, 'assumptions','raw_report'):
        assert compat.capture.digest(actual[key]) == compat.capture.digest(expected[key])
    assert 'time_layer' not in actual['assumptions']
    assert 'time_expiry_fills' not in actual['raw_report']


@pytest.mark.parametrize('name', PARAMS)
def test_http_gap_is_business_failure_before_warmup(name, monkeypatch):
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    data = compat.compat.frame().iloc[60:].copy().drop(compat.compat.frame().index[100])
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a: data)
    monkeypatch.setattr(p, '_fetch_template_warmup', lambda *a: pytest.fail('gap reached warmup'))
    response = TestClient(p.app).post('/cutie/backtest', json=compat.capture.request_body(name,
        dict(PARAMS[name], time_layer_enabled=True)))
    body = response.json()
    assert body['error_type'] == 'TIME_DATA_GAP'
    assert body['limitations']['reason'] == 'time_data_gap'


@pytest.mark.parametrize('name', PARAMS)
@pytest.mark.parametrize('extra', [{'time_calendar':'none'}, {'time_reset_period':'day'},
    {'time_weekdays':1.0}, {'max_holding_bars':3.0}, {'max_holding_bars':3}])
def test_http_invalid_before_data_fetch(name, extra, monkeypatch):
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a: pytest.fail('invalid reached data fetch'))
    response = TestClient(p.app).post('/cutie/backtest', json=compat.capture.request_body(name, dict(PARAMS[name], **extra)))
    assert response.json()['error_type'] == 'INVALID_PARAMS'


@pytest.mark.parametrize('name', PARAMS)
@pytest.mark.parametrize('extra', [
    {'time_weekdays':1}, {'time_weekdays':127}, {'time_max_holding_minutes':0},
    {'time_max_holding_minutes':525600}, {'time_flatten_at':'00:00'},
    {'time_flatten_at':'23:59', 'time_flatten_weekdays':1},
    {'time_timezone':'America/New_York'}, {'time_session_start':'22:00','time_session_end':'02:00'},
])
def test_time_boundaries_each_template(name, extra):
    assert build(name, dict(time_layer_enabled=True, **extra))['scale_in_out']['time_config'].enabled


@pytest.mark.parametrize('name', PARAMS)
def test_http_enabled_unsupported_timeframe_before_fetch(name, monkeypatch):
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a: pytest.fail('unsupported clock reached fetch'))
    body = compat.capture.request_body(name, dict(PARAMS[name], time_layer_enabled=True))
    body['backtest']['timeframe'] = '1M'
    assert TestClient(p.app).post('/cutie/backtest', json=body).json()['error_type'] == 'INVALID_PARAMS'


def test_template_full_exit_resets_clock():
    result = run(actions={0:'buy', 1:'sell_all', 3:'buy'}, n=3)
    assert [t['closed_at'] for t in result.trades] == [bars()[2].open_time, bars()[7].open_time]
    assert result.time_expiry_fills == 1
