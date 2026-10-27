"""C38 synthetic 15m candles + mocked central metrics, real HTTP path, no network."""
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import pandas as pd
import pytest
from backtesting import Backtest
from fastapi.testclient import TestClient
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p
import strategy_etf_flow_follow as etf
TOOL = 'local.backtesting_py.etf_flow_follow'


def ts(text):
    return int(pd.Timestamp(text, tz='UTC').timestamp())


def frame(start='2026-03-09', end='2026-03-10'):
    index = pd.date_range(start, end, freq='15min', inclusive='left')
    return pd.DataFrame(dict(Open=100., High=100.5, Low=99.5, Close=100., Volume=1.), index=index)


def rows_for(data, values=None):
    config = etf.EtfFlowConfig.parse({})
    required = config.plan(int(data.index[0].timestamp()), int(data.index[-1].timestamp()) + 900)
    return [{'ts': t, 'value': str((values or {}).get(datetime.fromtimestamp(t, timezone.utc).date().isoformat(), 0))}
            for t in required]


def install(monkeypatch, tmp_path, data, rows=None, fail=False):
    calls = []
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a, **k: data.copy())
    def no_warmup(*a, **k):
        raise AssertionError('ETF template has no indicator warmup')
    monkeypatch.setattr(p, '_fetch_template_warmup', no_warmup)
    def chunk(**kwargs):
        calls.append(kwargs)
        if fail:
            raise ValueError('central metrics unavailable')
        assert kwargs['symbol'] in ('BTC', 'ETH')
        assert (kwargs['exchange'], kwargs['metric'], kwargs['interval']) == ('AGGREGATED', 'etf_flow', '1d')
        return [row for row in (rows if rows is not None else rows_for(data))
                if kwargs['start_at'] <= row['ts'] <= kwargs['end_at']]
    monkeypatch.setattr(p, '_fetch_artifact_metric_chunk', chunk)
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **k: None)
    return calls


def post(data, params=None, **extra):
    request = dict(run_id='c38', provider_tool_id=TOOL, provider_params=params or {}, symbol='BTCUSDT',
                   market='spot', timeframe='15m', start_at=int(data.index[0].timestamp()),
                   end_at=int(data.index[-1].timestamp()) + 900, initial_capital='10000', fee_bps='0', slippage_bps='0')
    request.update(extra)
    response = TestClient(p.app).post('/cutie/backtest', json={'backtest': request})
    assert response.status_code == 200
    return response.json()


def run(monkeypatch, tmp_path, data=None, values=None, params=None, **extra):
    data = frame() if data is None else data
    calls = install(monkeypatch, tmp_path, data, rows_for(data, values))
    body = post(data, params, **extra)
    assert body['result_status'] == 'success', body
    return body, calls


def test_entry_positive_negative_and_regular_session_exit(monkeypatch, tmp_path):
    # Monday uses Friday; equality qualifies. Same-day huge flow is never a signal.
    body, calls = run(monkeypatch, tmp_path, values={'2026-03-06': 300000000, '2026-03-09': 900000000})
    assert len(body['trades']) == 1
    trade = body['trades'][0]
    assert (trade['opened_at'], trade['closed_at'], trade['entry_price'], trade['exit_price'], trade['pnl']) == (
        ts('2026-03-09 13:30'), ts('2026-03-09 20:00'), '100', '100', '0')
    report = body['raw_report']['etf_flow_follow']
    assert report['fills'][0]['reason'] == 'us_session_close'
    assert report['fills'][0]['flow_ts'] == ts('2026-03-06')
    assert report['series']['unit'] == 'usd'
    assert calls[0]['start_at'] == ts('2026-03-06')
    assert '做空镜像未开放' in body['assumptions']['etf_flow_follow']
    # Frozen result.v2 keys: exit reason belongs only to raw_report.
    assert set(body['metrics']) == {'total_return', 'max_drawdown', 'trade_count'}
    assert set(trade) == {'seq', 'side', 'opened_at', 'closed_at', 'qty', 'entry_price', 'exit_price', 'pnl', 'fee', 'slippage'}
    for flow in (299999999, -300000000):
        negative, _ = run(monkeypatch, tmp_path, values={'2026-03-06': flow, '2026-03-09': 900000000})
        assert negative['trades'] == []
        assert not any(d['action'] == 'entry' for d in negative['raw_report']['etf_flow_follow']['decisions'])


def test_stop_frozen_at_fill_native_touch_and_no_reentry(monkeypatch, tmp_path):
    data = frame()
    data.loc[pd.Timestamp('2026-03-09 13:30')] = [120, 121, 117.6, 120, 1]
    data.loc[pd.Timestamp('2026-03-09 13:45')] = [90, 91, 89, 90, 1]
    body, _ = run(monkeypatch, tmp_path, data, {'2026-03-06': 400000000})
    assert len(body['trades']) == 1
    trade = body['trades'][0]
    assert (trade['opened_at'], trade['closed_at'], trade['entry_price'], trade['exit_price']) == (
        ts('2026-03-09 13:30'), ts('2026-03-09 13:30'), '120', '117.6')
    assert body['raw_report']['etf_flow_follow']['fills'][0]['reason'] == 'stop_loss'
    # Wide stop is 108 at the 120 fill, so low 117.6 does not trigger it.
    data.loc[pd.Timestamp('2026-03-09 13:45'):] = [120, 121, 119, 120, 1]
    wide, _ = run(monkeypatch, tmp_path, data, {'2026-03-06': 400000000}, {'stop_loss_pct': 10})
    assert wide['trades'][0]['closed_at'] == ts('2026-03-09 20:00')


def test_calendar_dst_half_days_holidays_and_no_overnight(monkeypatch, tmp_path):
    # Independent expected UTC times, including half days in every covered year.
    cases = [('2026-03-06', '2026-03-05', '14:30', '21:00'),
             ('2026-03-09', '2026-03-06', '13:30', '20:00'),
             ('2024-07-03', '2024-07-02', '13:30', '17:00'),
             ('2025-11-28', '2025-11-26', '14:30', '18:00'),
             ('2026-12-24', '2026-12-23', '14:30', '18:00'),
             ('2026-07-02', '2026-07-01', '13:30', '20:00')]
    for day, previous, opened, closed in cases:
        data = frame(day, (pd.Timestamp(day) + pd.Timedelta(days=1)).isoformat())
        body, _ = run(monkeypatch, tmp_path, data, {previous: 400000000})
        assert [(t['opened_at'], t['closed_at']) for t in body['trades']] == [(ts(day + ' ' + opened), ts(day + ' ' + closed))]
        assert body['raw_report']['etf_flow_follow']['fills'][0]['reason'] == 'us_session_close'
    # Mourning holiday + weekend: Friday uses Wednesday, Monday uses Friday.
    data = frame('2025-01-08', '2025-01-14')
    body, _ = run(monkeypatch, tmp_path, data, {'2025-01-07': 400000000, '2025-01-08': 400000000, '2025-01-10': 400000000})
    assert [pd.Timestamp(t['opened_at'], unit='s').date().isoformat() for t in body['trades']] == [
        '2025-01-08', '2025-01-10', '2025-01-13']
    assert all(datetime.fromtimestamp(t['opened_at'], etf.ZONE).date() == datetime.fromtimestamp(t['closed_at'], etf.ZONE).date()
               for t in body['trades'])
    assert [sum(etf.is_trading_day(d.date()) for d in pd.date_range(f'{y}-01-01', f'{y}-12-31'))
            for y in (2024, 2025, 2026)] == [252, 250, 251]


@pytest.mark.parametrize('day', [
    '2027-01-01', '2027-01-18', '2027-02-15', '2027-03-26', '2027-05-31',
    '2027-06-18', '2027-07-05', '2027-09-06', '2027-11-25', '2027-12-24',
])
def test_calendar_2027_holidays(day):
    assert not etf.is_trading_day(date.fromisoformat(day))
    assert etf.session(date.fromisoformat(day)) is None


@pytest.mark.parametrize('day,previous,opened,closed', [
    ('2027-11-26', '2027-11-24', '14:30', '18:00'),
    ('2027-07-02', '2027-07-01', '13:30', '20:00'),
    ('2027-12-31', '2027-12-30', '14:30', '21:00'),
])
def test_calendar_2027_session_closes(monkeypatch, tmp_path, day, previous, opened, closed):
    data = frame(day, (pd.Timestamp(day) + pd.Timedelta(days=1)).isoformat())
    body, _ = run(monkeypatch, tmp_path, data, {previous: 400000000})
    assert [(t['opened_at'], t['closed_at']) for t in body['trades']] == [
        (ts(day + ' ' + opened), ts(day + ' ' + closed))]
    report = body['raw_report']['etf_flow_follow']
    assert report['fills'][0]['reason'] == 'us_session_close'
    assert report['fills'][0]['flow_ts'] == ts(previous)
    assert report['calendar']['last_date'] == etf.CALENDAR_LAST.isoformat() == '2030-12-31'


def test_calendar_2027_trading_day_count():
    days = [d.date() for d in pd.date_range('2027-01-01', '2027-12-31')]
    # 365 days - 104 weekend days - 10 weekday holidays = 251 sessions (half day counts as one).
    assert sum(d.weekday() < 5 for d in days) == 261
    assert sum(etf.is_trading_day(d) for d in days) == 261 - 10 == 251


def test_calendar_2027_first_session_uses_previous_year_flow():
    assert etf.session(date(2027, 1, 4))['flow_ts'] == ts('2026-12-31')


# NYSE official tables 2024-2027 (the hand-copied calendar before S5-7 rule generation).
OFFICIAL_HOLIDAYS = {
    2024: ['2024-01-01', '2024-01-15', '2024-02-19', '2024-03-29', '2024-05-27',
           '2024-06-19', '2024-07-04', '2024-09-02', '2024-11-28', '2024-12-25'],
    2025: ['2025-01-01', '2025-01-09', '2025-01-20', '2025-02-17', '2025-04-18',
           '2025-05-26', '2025-06-19', '2025-07-04', '2025-09-01', '2025-11-27', '2025-12-25'],
    2026: ['2026-01-01', '2026-01-19', '2026-02-16', '2026-04-03', '2026-05-25',
           '2026-06-19', '2026-07-03', '2026-09-07', '2026-11-26', '2026-12-25'],
    2027: ['2027-01-01', '2027-01-18', '2027-02-15', '2027-03-26', '2027-05-31',
           '2027-06-18', '2027-07-05', '2027-09-06', '2027-11-25', '2027-12-24'],
}
OFFICIAL_HALF_DAYS = {
    2024: ['2024-07-03', '2024-11-29', '2024-12-24'],
    2025: ['2025-07-03', '2025-11-28', '2025-12-24'],
    2026: ['2026-11-27', '2026-12-24'],
    2027: ['2027-11-26'],
}


@pytest.mark.parametrize('year', sorted(OFFICIAL_HOLIDAYS))
def test_rule_calendar_matches_official_tables_day_for_day(year):
    assert sorted(d.isoformat() for d in etf.HOLIDAYS if d.year == year) == OFFICIAL_HOLIDAYS[year]
    assert sorted(d.isoformat() for d in etf.HALF_DAYS if d.year == year) == OFFICIAL_HALF_DAYS[year]


def test_rule_calendar_known_shifts_beyond_official_tables():
    # Jan 1 2028 is a Saturday: NYSE does not close Fri 2027-12-31; Jan 1 2033 Saturday likewise.
    assert etf.is_trading_day(date(2027, 12, 31)) and date(2028, 1, 1) not in etf.HOLIDAYS
    # Jan 1 2029 Monday; Juneteenth 2028 Monday; Christmas 2028 Monday; Good Friday 2028-04-14.
    for day in ('2029-01-01', '2028-06-19', '2028-12-25', '2028-04-14', '2029-03-30', '2030-04-19'):
        assert not etf.is_trading_day(date.fromisoformat(day))


def test_calendar_covers_today_plus_400_days():
    # Guard (S5-7, Owner 10-27): extend CALENDAR_LAST before backtests near today start failing closed.
    assert etf.CALENDAR_LAST >= date.today() + timedelta(days=400)


def test_available_time_boundary_rejects_future_row(monkeypatch, tmp_path):
    config = etf.EtfFlowConfig.parse({})
    event = etf.session(date(2026, 3, 9))
    available = ts('2026-03-07')
    config.bind([dict(ts=ts('2026-03-06'), available_at=available, value='400000000', revision='fixture')], symbol='BTC')
    # One second before D+1 midnight remains unreadable; equality is readable. +15m mutation must fail.
    assert config.readable_flow(event, available - 1) is None
    assert config.readable_flow(event, available)['value'] == '400000000'
    body, _ = run(monkeypatch, tmp_path, values={'2026-03-06': 0, '2026-03-09': 10000000000})
    assert body['trades'] == []
    assert not any(d['action'] == 'entry' for d in body['raw_report']['etf_flow_follow']['decisions'])


def test_series_gap_and_malformed_fail_whole_run(monkeypatch, tmp_path):
    data = frame('2026-03-09', '2026-03-11')
    rows = rows_for(data, {'2026-03-06': 400000000})
    for dropped in ('2026-03-06', '2026-03-09'):
        install(monkeypatch, tmp_path, data, [r for r in rows if r['ts'] != ts(dropped)])
        body = post(data)
        assert (body['result_status'], body['error_type'], body['limitations']['reason']) == ('failed', 'TIME_DATA_GAP', 'etf_flow_data_gap')
        assert body.get('trades', []) == []
        assert body['limitations']['required_first_date'] == '2026-03-06'
        assert body['limitations']['required_last_date'] == '2026-03-09'
    for broken in ([], rows + [rows[0]], [{**rows[0], 'value': 'NaN'}, *rows[1:]],
                   [{**rows[0], 'ts': rows[0]['ts'] + 1}, *rows[1:]]):
        install(monkeypatch, tmp_path, data, broken)
        assert post(data)['limitations']['reason'] == 'etf_flow_data_gap'


def test_history_unavailable_has_server_dates_and_no_raw_exception(monkeypatch, tmp_path):
    data = frame('2026-03-09', '2026-03-11')
    install(monkeypatch, tmp_path, data, rows_for(data), fail=True)
    body = post(data)
    assert (body['result_status'], body['error_type'], body['limitations']['reason']) == (
        'failed', 'TIME_DATA_GAP', 'etf_flow_history_unavailable')
    assert body['limitations']['required_first_date'] == '2026-03-06'
    assert body['limitations']['required_last_date'] == '2026-03-09'
    assert 'central metrics unavailable' not in str(body['limitations'])
    assert 'central metrics unavailable' not in body['error_message']


def test_intraday_end_requires_previous_flow_only(monkeypatch, tmp_path):
    data = frame('2026-03-09', '2026-03-09 15:00')
    # Mirror stops at Friday P; Monday T's row does not exist and must never be required.
    calls = install(monkeypatch, tmp_path, data, [{'ts': ts('2026-03-06'), 'value': '400000000'}])
    body = post(data)
    assert body['result_status'] == 'success', body
    assert len(body['trades']) == 1 and body['trades'][0]['opened_at'] == ts('2026-03-09 13:30')
    assert body['raw_report']['etf_flow_follow']['series']['required_dates'] == ['2026-03-06']
    assert (calls[0]['start_at'], calls[0]['end_at']) == (ts('2026-03-06'), ts('2026-03-07') - 1)
    # The worker-2 ruling includes a session opening exactly at end_at in the dependency set.
    config = etf.EtfFlowConfig.parse({})
    assert config.plan(ts('2026-03-09'), ts('2026-03-09 13:30') - 1) == ()
    assert config.plan(ts('2026-03-09'), ts('2026-03-09 13:30')) == (ts('2026-03-06'),)


def test_non_nyse_extra_rows_are_ignored_not_used(monkeypatch, tmp_path):
    data = frame('2026-03-09', '2026-03-11')
    # Extra weekend points (one malformed value) must not override the prior NYSE day's zero flow.
    rows = [{'ts': ts('2026-03-06'), 'value': '0'}, {'ts': ts('2026-03-09'), 'value': '0'},
            {'ts': ts('2026-03-07'), 'value': '10000000000'}, {'ts': ts('2026-03-08'), 'value': 'NaN'}]
    install(monkeypatch, tmp_path, data, rows)
    body = post(data)
    assert body['result_status'] == 'success', body
    assert body['trades'] == []
    series = body['raw_report']['etf_flow_follow']['series']
    assert series['required_dates'] == ['2026-03-06', '2026-03-09']
    assert series['count'] == 2 and series['ignored_rows_count'] == 2


def test_session_open_at_matches_plan_in_summer_and_winter():
    for day, expected in ((date(2026, 3, 6), ts('2026-03-06 14:30')),
                          (date(2026, 3, 9), ts('2026-03-09 13:30'))):
        config = etf.EtfFlowConfig.parse({})
        config.plan(etf.day_ts(day), etf.day_ts(day) + 86400)
        event = etf.session(day)
        assert event['open_at'] == expected
        assert list(config.sessions) == [expected]
        assert config.sessions[expected]['open_at'] == expected


def test_calendar_coverage_fails_before_fetch(monkeypatch, tmp_path):
    def forbidden(*a, **k):
        raise AssertionError('outside calendar must fail before fetch')
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, '_fetch_ohlcv', forbidden)
    monkeypatch.setattr(p, '_fetch_artifact_metric_chunk', forbidden)
    for start, end in [('2023-12-31', '2024-01-02'), ('2030-12-31', '2031-01-02'), ('2031-01-01', '2031-01-02'), ('2031-01-03', '2031-01-04'), ('2024-01-02', '2024-01-03')]:
        body = post(frame(start, end))
        assert (body['error_type'], body['limitations']['reason']) == ('INSUFFICIENT_DATA', 'etf_calendar_unavailable')


def test_schema_defaults_ranges_and_invalid_params(monkeypatch, tmp_path):
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    tools = {t['tool_id']: t for t in TestClient(p.app).get('/catalog').json()['tools']}
    tool = tools[TOOL]
    assert (tool['name'], p.TOOL_SPECS[TOOL]['strategy_family'], tool['markets'], tool['timeframes']) == ('Local Spot ETF Flow Follow', 'event_driven', ['spot'], ['15m'])
    props = tool['param_schema']['properties']
    assert props['flow_threshold_usd'] == dict(type='number', default=300000000, minimum=1000000, maximum=10000000000)
    assert props['stop_loss_pct'] == dict(type='number', default=2, minimum=0.1, maximum=50)
    assert set(p._TIME_PARAM_SCHEMA_PROPERTIES) <= props.keys()
    build = p.TOOL_SPECS[TOOL]['build']
    built = build({})
    assert (built['executed_name'], built['etf_flow_config'].values, built['strategy']._time_config) == (
        'ETF Flow Follow', {'flow_threshold_usd': 300000000, 'stop_loss_pct': 2}, None)
    for params in ({'flow_threshold_usd': 999999}, {'flow_threshold_usd': 10000000001}, {'flow_threshold_usd': True},
                   {'flow_threshold_usd': '300000000'}, {'stop_loss_pct': 0.09}, {'stop_loss_pct': 50.1},
                   {'stop_loss_pct': False}, {'stop_loss_pct': '2'}, {'stop_loss_pct': float('nan')},
                   {'direction': 'short'}, {'take_profit_pct': 5}, {'max_holding_bars': 1}):
        try:
            build(params)
        except ValueError as exc:
            assert str(exc).startswith('INVALID_PARAMS:')
        else:
            raise AssertionError(params)
    for params in ({'flow_threshold_usd': 1000000, 'stop_loss_pct': 0.1}, {'flow_threshold_usd': 10000000000, 'stop_loss_pct': 50}):
        assert build(params)['etf_flow_config'].values == params


def test_symbols_market_timeframe_and_candle_gaps(monkeypatch, tmp_path):
    data = frame()
    calls = install(monkeypatch, tmp_path, data)
    for symbol in ('SOLUSDT', 'BTCUSD', 'BTCEUR', 'BTCUSDT:USDT', 'BTCBTC', 'ETH'):
        assert post(data, symbol=symbol)['error_type'] == 'INVALID_PARAMS'
    assert post(data, market='futures')['error_type'] == 'INVALID_PARAMS'
    for timeframe in ('1h', '1d', '30m'):
        assert post(data, timeframe=timeframe)['error_type'] == 'TIMEFRAME_UNSUPPORTED'
    assert calls == []
    for symbol in ('BTCUSDT', 'BTC/USDC', 'BTCBUSD', 'ETHUSDT', 'ETH/USDC', 'ETHBUSD'):
        body = post(data, symbol=symbol)
        assert body['result_status'] == 'success', body
        assert calls[-1]['symbol'] == ('ETH' if symbol.startswith('ETH') else 'BTC')
    install(monkeypatch, tmp_path, data.drop(pd.Timestamp('2026-03-09 19:45')))
    assert post(data)['error_type'] == 'TIME_DATA_GAP'
