"""Real registered HTTP/Backtest execution, with independently hand-calculated clocks."""
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd
import pytest
from backtesting import Backtest
from fastapi.testclient import TestClient
from test_risk_layer import p
from strategy_time_layer import TimeContext

TOOL = 'local.backtesting_py.calendar_schedule'
SCHEDULE = dict(time_entry_at='02:00', time_max_holding_minutes=120)


def frame(start='2026-01-01', count=12, freq='h'):
    return pd.DataFrame(dict(Open=[100.]*count, High=[101.]*count, Low=[99.]*count,
        Close=[100.]*count, Volume=[1.]*count), index=pd.date_range(start, periods=count, freq=freq))


def direct(data, params=None, timeframe='1h', trade_on_close=False):
    built = p.TOOL_SPECS[TOOL]['build'](SCHEDULE if params is None else params)
    cls = built['strategy']
    cls._calendar_timeframe = timeframe
    if cls._time_config is not None:
        cls._time_context = TimeContext.build(cls._time_config, timeframe, data.index)
    return Backtest(data, cls, cash=10000, commission=0, exclusive_orders=True,
        finalize_trades=True, trade_on_close=trade_on_close).run()


def http_run(monkeypatch, tmp_path, data, params=None, market='spot', timeframe='1h'):
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', Path(tmp_path))
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a: data.copy())
    monkeypatch.setattr(p, '_fetch_template_warmup', lambda *a: pytest.fail('calendar has no indicator warmup'))
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **k: None)
    request = dict(run_id='f2_test', provider_tool_id=TOOL, provider_params=SCHEDULE if params is None else params,
        symbol='BTCUSDT', market=market, timeframe=timeframe, start_at=int(data.index[0].timestamp()),
        end_at=int(data.index[-1].timestamp()) + int(pd.Timedelta(timeframe).total_seconds()),
        initial_capital='10000', fee_bps='0', slippage_bps='0')
    return TestClient(p.app).post('/cutie/backtest', json={'backtest': request}).json()


def test_defaults_schema_and_request_isolation():
    first = p.TOOL_SPECS[TOOL]['build']({})
    second = p.TOOL_SPECS[TOOL]['build'](SCHEDULE)
    assert first['strategy'] is not second['strategy']
    assert first['strategy']._time_config is None
    assert first['strategy']._risk == {'stop_loss_pct': .03}
    assert first['calendar_config'].at == ''
    schema = p.TOOL_SPECS[TOOL]['param_schema_properties']
    assert [schema[k]['default'] for k in ('time_entry_at', 'time_entry_weekday', 'time_entry_monthday')] == ['', -1, 0]
    assert schema['stop_loss_pct']['default'] == 3
    assert not {'atr_stop_multiplier', 'trailing_stop_pct', 'tp1_r'} & schema.keys()


@pytest.mark.parametrize('side', ['long', 'short'])
def test_causal_entry_holding_from_fill_next_open(side):
    data = frame()
    data.iloc[1] = [100, 103, 99, 102, 1]  # trigger bar closes at 02:00
    data.iloc[2] = [104, 105, 100, 103, 1]  # executable next open
    data.iloc[4] = [106, 107, 105, 106, 1]  # holding deadline next-open exit
    stats = direct(data, {**SCHEDULE, 'direction': side, 'calendar_stop_enabled': False})
    trade = stats['_trades'].iloc[0]
    assert (trade.EntryBar, trade.EntryPrice, trade.ExitBar, trade.ExitPrice) == (2, 104, 4, 106)
    assert (trade.Size > 0) == (side == 'long')
    record = stats['_strategy'].calendar_events[0]
    assert record['event_utc'] == '2026-01-01T02:00:00+00:00'
    assert record['exit_reason'] == 'time_expiry'


def test_future_prices_cannot_change_entry_signal():
    data = frame()
    future = data.copy()
    future.iloc[8] = [700, 701, 699, 700, 1]
    left, right = direct(data)['_trades'], direct(future)['_trades']
    assert tuple(left.iloc[0][['EntryBar', 'EntryPrice']]) == tuple(right.iloc[0][['EntryBar', 'EntryPrice']]) == (2, 100)


def test_weekly_friday_to_monday_and_timezone(monkeypatch, tmp_path):
    data = frame('2026-01-02T00:00', 100)
    params = dict(time_entry_at='20:00', time_entry_weekday=4, time_flatten_at='08:00', time_flatten_weekdays=1)
    response = http_run(monkeypatch, tmp_path, data, params)
    assert response['result_status'] == 'success', response
    trade = response['trades'][0]
    assert trade['opened_at'] == int(datetime(2026, 1, 2, 20, tzinfo=timezone.utc).timestamp())
    assert trade['closed_at'] == int(datetime(2026, 1, 5, 8, tzinfo=timezone.utc).timestamp())
    local = http_run(monkeypatch, tmp_path, data, {**params, 'time_timezone': 'America/New_York'})
    assert local['trades'][0]['opened_at'] == int(datetime(2026, 1, 3, 1, tzinfo=timezone.utc).timestamp())
    assert local['trades'][0]['closed_at'] == int(datetime(2026, 1, 5, 13, tzinfo=timezone.utc).timestamp())
    assert local['raw_report']['calendar_events'][0]['exit_reason'] == 'time_expiry'
    assert local['assumptions']['calendar_schedule']['fill'] == 'next_bar_open_market'


def test_monthday_31_skips_april_and_february():
    data = frame('2026-01-30', 24*123)
    stats = direct(data, dict(time_entry_at='02:00', time_entry_monthday=31, time_max_holding_minutes=60))
    assert list(stats['_trades'].EntryTime) == [pd.Timestamp('2026-01-31T02:00'), pd.Timestamp('2026-03-31T02:00'), pd.Timestamp('2026-05-31T02:00')]


def test_no_add_or_reverse_while_holding():
    stats = direct(frame(count=80), dict(time_entry_at='02:00', time_max_holding_minutes=60*60))
    assert len(stats['_trades']) == 2
    events = stats['_strategy'].calendar_events
    assert [(e['event_utc'], e['reason']) for e in events if e['status'] == 'skipped'] == [
        ('2026-01-02T02:00:00+00:00', 'already_holding'), ('2026-01-03T02:00:00+00:00', 'already_holding')]
    assert stats['_trades'].iloc[0].ExitTime == pd.Timestamp('2026-01-03T14:00')


@pytest.mark.parametrize('start,at,expected', [
    ('2026-03-08T00:00', '02:30', []),
    ('2026-03-08T00:00', '03:00', [pd.Timestamp('2026-03-08T07:00')]),
    ('2026-11-01T00:00', '01:30', [pd.Timestamp('2026-11-01T05:30')]),
])
def test_new_york_dst_real_fills(start, at, expected):
    stats = direct(frame(start, 48, '30min'), dict(time_entry_at=at, time_entry_weekday=6,
        time_timezone='America/New_York', time_max_holding_minutes=30), timeframe='30m')
    assert list(stats['_trades'].EntryTime) == expected
    assert len(stats['_strategy'].calendar_events) == len(expected)


@pytest.mark.parametrize('start,at,expected', [
    ('2026-03-08T00:00', '02:30', '2026-03-09T06:30'),
    ('2026-11-01T00:00', '01:30', '2026-11-01T05:30'),
])
def test_flatten_dst_and_elapsed_utc(start, at, expected):
    stats = direct(frame(start, 100, '30min'), dict(time_entry_at='00:30', time_timezone='America/New_York',
        time_flatten_at=at), timeframe='30m')
    assert stats['_trades'].iloc[0].ExitTime == pd.Timestamp(expected)


@pytest.mark.parametrize('stop,high,low,expected', [(True, 110, 96, 'stop_loss'),
    (True, 110, 99, 'time_expiry'), (False, 110, 96, 'time_expiry')])
def test_stop_then_time_then_take_priority(stop, high, low, expected):
    data = frame()
    data.iloc[3] = [100, high, low, 100, 1]
    stats = direct(data, {**SCHEDULE, 'take_profit_pct': 5, 'calendar_stop_enabled': stop})
    # Avoid an earlier TP on the fill bar; both events occur at the 04:00 decision.
    assert stats['_trades'].iloc[0].ExitBar == 4
    assert stats['_strategy'].calendar_events[0]['exit_reason'] == expected


def test_take_profit_before_later_time_deadline():
    data = frame()
    data.iloc[2] = [100, 106, 99, 101, 1]
    stats = direct(data, {**SCHEDULE, 'take_profit_pct': 5})
    assert stats['_trades'].iloc[0].ExitBar == 3
    assert stats['_strategy'].calendar_events[0]['exit_reason'] == 'take_profit'


def test_stop_frozen_at_signal_and_market_exit():
    data = frame()
    data.iloc[2] = [110, 111, 98, 110, 1]  # 97 frozen signal stop; fill-based stop would be 106.7
    data.iloc[3] = [110, 111, 96, 100, 1]
    data.iloc[4] = [90, 91, 89, 90, 1]
    stats = direct(data)
    assert (stats['_trades'].iloc[0].EntryPrice, stats['_trades'].iloc[0].ExitBar, stats['_trades'].iloc[0].ExitPrice) == (110, 4, 90)
    assert stats['_strategy'].calendar_events[0]['stop_price'] == 97
    assert stats['_strategy'].calendar_events[0]['exit_reason'] == 'stop_loss'


@pytest.mark.parametrize('side,price', [('long', 96), ('short', 104)])
def test_gap_wrong_side_cancels_before_trade(monkeypatch, tmp_path, side, price):
    data = frame()
    data.iloc[2] = [price, price+1, price-1, price, 1]
    response = http_run(monkeypatch, tmp_path, data, {**SCHEDULE, 'direction': side}, market='futures')
    assert response['result_status'] == 'success', response
    assert response['trades'] == []
    event = response['raw_report']['calendar_events'][0]
    assert (event['status'], event['reason'], event['fill_open']) == ('skipped', 'gap_stop_wrong_side', price)


def test_stop_can_be_disabled():
    data = frame()
    data.iloc[2] = [90, 101, 89, 100, 1]
    stats = direct(data, {**SCHEDULE, 'calendar_stop_enabled': False})
    assert stats['_trades'].iloc[0].EntryPrice == 90
    assert stats['_strategy'].calendar_events[0]['stop_price'] is None


def test_no_entry_without_next_bar():
    stats = direct(frame(count=3), dict(time_entry_at='03:00', time_max_holding_minutes=60))
    assert stats['_trades'].empty
    assert stats['_strategy'].calendar_events[0]['reason'] == 'no_next_open'


@pytest.mark.parametrize('params', [
    {'direction': 'short'}, {'direction': 'both'}, {'direction': 'bad'},
    {'time_entry_weekday': 4, 'time_entry_monthday': 1}, {'time_entry_weekday': True},
    {'time_entry_monthday': 31.0}, {'time_entry_at': '2:00'}, {'time_entry_at': None},
    {'time_timezone': 'Invalid/Zone'}, {'time_max_holding_minutes': True},
    {'time_flatten_at': '24:00'}, {'time_max_holding_minutes': 90}, {'calendar_stop_enabled': 0},
    {'calendar_stop_enabled': False, 'stop_loss_pct': 3},
    {'time_entry_at': '02:07'}, {'time_flatten_at': '05:07'},
    {'stop_loss_pct': 1e-320}, {'take_profit_pct': 1e-320}, {'atr_stop_multiplier': 2}, {'time_session_start': '06:00', 'time_session_end': '10:00'},
])
def test_invalid_params_before_fetch(monkeypatch, params):
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a: pytest.fail('invalid calendar fetched'))
    request = dict(provider_tool_id=TOOL, provider_params={**SCHEDULE, **params}, symbol='BTCUSDT',
        market='spot', timeframe='1h', start_at=1767225600, end_at=1767268800)
    assert TestClient(p.app).post('/cutie/backtest', json={'backtest': request}).json()['error_type'] == 'INVALID_PARAMS'


@pytest.mark.parametrize('params', [{'time_entry_at': '02:00'}, {'time_entry_weekday': 4},
    {'time_entry_monthday': -1}, {'time_entry_weekday': 7}])
def test_incomplete_calendar_rejected(params):
    with pytest.raises(ValueError, match='INVALID_PARAMS:'):
        p.TOOL_SPECS[TOOL]['build'](params)


def test_http_missing_candle_and_unsupported_period(monkeypatch, tmp_path):
    data = frame().drop(pd.Timestamp('2026-01-01T05:00'))
    assert http_run(monkeypatch, tmp_path, data)['error_type'] == 'TIME_DATA_GAP'
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a: pytest.fail('unsupported period fetched'))
    req = dict(provider_tool_id=TOOL, provider_params=SCHEDULE, symbol='BTCUSDT', market='spot',
        timeframe='1M', start_at=1767225600, end_at=1767268800)
    assert TestClient(p.app).post('/cutie/backtest', json={'backtest': req}).json()['error_type'] == 'INVALID_PARAMS'


def test_same_bar_repeat_and_flatten_does_not_reopen():
    stats = direct(frame(count=48), dict(time_entry_at='02:00', time_flatten_at='02:00'))
    assert list(stats['_trades'].EntryTime) == [pd.Timestamp('2026-01-01T02:00'), pd.Timestamp('2026-01-02T02:00')]
    # A cutoff equal to the entry is due on the fill bar, then exit next open.
    assert list(stats['_trades'].ExitTime) == [pd.Timestamp('2026-01-01T03:00'), pd.Timestamp('2026-01-02T03:00')]


def test_first_closed_bar_event_is_not_lost():
    stats = direct(frame(), dict(time_entry_at='01:00', time_max_holding_minutes=60))
    assert len(stats['_trades']) == 1
    assert (stats['_trades'].iloc[0].EntryBar, stats['_trades'].iloc[0].ExitBar) == (1, 2)


def test_first_event_respects_optional_entry_gate():
    stats = direct(frame(), dict(time_entry_at='01:00', time_max_holding_minutes=60,
        time_layer_enabled=True, time_session_start='02:00', time_session_end='03:00'))
    assert stats['_trades'].empty
    assert stats['_strategy'].calendar_events[0]['reason'] == 'entry_gate'


@pytest.mark.parametrize('flatten,accepted', [('23:47', False), ('23:45', True)])
def test_f2_flatten_grid_pre_fetch(monkeypatch, tmp_path, flatten, accepted):
    data = frame(count=96, freq='15min')
    if not accepted:
        monkeypatch.setattr(p, 'AUTH_TOKEN', '')
        monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a: pytest.fail('off-grid F2 flatten fetched'))
        req = dict(provider_tool_id=TOOL, provider_params=dict(time_entry_at='02:00', time_flatten_at=flatten),
            symbol='BTCUSDT', market='spot', timeframe='15m', start_at=1767225600, end_at=1767312000)
        response = TestClient(p.app).post('/cutie/backtest', json={'backtest': req}).json()
        assert response['error_type'] == 'INVALID_PARAMS', response
    else:
        response = http_run(monkeypatch, tmp_path, data, dict(time_entry_at='02:00', time_flatten_at=flatten), timeframe='15m')
        assert response['result_status'] == 'success', response
        assert response['trades'][0]['closed_at'] == 1767311100
