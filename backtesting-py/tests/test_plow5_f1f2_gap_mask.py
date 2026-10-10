"""P-LOW5-F1F2: a main-range gap within CENTRAL_GAP_TOLERANCE_RATIO no longer fails F1 (ORB / Asia range
breakout) or F2 (calendar schedule); beyond it the run fails TIME_DATA_GAP with details (the VWAP shape).

- F1 masks every whole local range cycle (time_timezone + range_start decide it; F1 resets daily only,
  strategy_range_breakout.py RangeConfig -> PeriodDefinition(reset_period='day')) whose range window
  through the flatten fill holds a missing candle: no range frozen, no entry.
- F2 skips events on a local calendar day holding a missing candle, and events whose holding window
  (signal bar .. latest exit fill) touches a missing candle, so holding counts never span a gap.

Gold gap = the central-source 2026-08-31 04:00-12:00 UTC hole (9 x 1h, fixtures/plow5_vwap_0831_README.md).
"""
from __future__ import annotations

import json

import pandas as pd
import pytest
from backtesting import Backtest
from fastapi.testclient import TestClient

import cutie_backtesting_provider as p

GAP = pd.date_range('2026-08-31 04:00', '2026-08-31 12:00', freq='1h', tz='UTC')
DAYS = 5
ORB = {'range_minutes': 60, 'flatten_at': '23:00'}
ASIA = {'range_start': '00:00', 'range_end': '01:00', 'breakout_start': '01:00', 'flatten_at': '23:00'}
# Both local cycles = [16:00, 16:00) UTC, not the UTC day: a non-UTC timezone, or a UTC range_start != 00:00.
SHANGHAI = {**ORB, 'time_layer_enabled': True, 'time_timezone': 'Asia/Shanghai'}
UTC_1600 = {'range_start': '16:00', 'range_minutes': 60, 'flatten_at': '15:00'}


def f1_series(start, days=DAYS):
    """Each local cycle trades once: hour 0 is the range (99-101), hour 1 closes 102 above it, the entry
    fills at hour 2 open 102 and is flattened at 23:00 local (stop 99 / take 106 never reached)."""
    index = pd.date_range(start, periods=24 * days, freq='1h', tz='UTC')
    rows = [[100., 101., 99., 100.] if k % 24 == 0 else [100., 102., 100., 102.] if k % 24 == 1 else [102.] * 4
            for k in range(len(index))]
    frame = pd.DataFrame(rows, columns=['Open', 'High', 'Low', 'Close'], index=index)
    frame['Volume'] = 1.0
    return frame


def post(monkeypatch, tmp_path, data, tool, params, start, end):
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a: data.copy())
    monkeypatch.setattr(p, '_fetch_template_warmup', lambda *a: data.iloc[:0])
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **kw: None)
    request = dict(run_id='plow5f', provider_tool_id='local.backtesting_py.' + tool, provider_params=params,
                   symbol='BTCUSDT', market='spot', timeframe='1h', start_at=int(pd.Timestamp(start).timestamp()),
                   end_at=int(pd.Timestamp(end).timestamp()), initial_capital='10000', fee_bps='0',
                   slippage_bps='0')
    return TestClient(p.app).post('/cutie/backtest', json={'backtest': request}).json()


def f1_post(monkeypatch, tmp_path, data, tool='opening_range_breakout', params=ORB, start='2026-08-29 00:00Z'):
    end = pd.Timestamp(start) + pd.Timedelta(days=DAYS)
    return post(monkeypatch, tmp_path, data, tool, params, start, end)


def opened(body):
    return [pd.Timestamp(t['opened_at'], unit='s', tz='UTC') for t in body['trades']]


def kept(body, lo, hi):
    # seq renumbers after the dropped trade; every other field of the other trades is byte-identical.
    return json.dumps([{k: v for k, v in t.items() if k != 'seq'} for t, at in zip(body['trades'], opened(body))
                       if not lo <= at < hi], sort_keys=True)


# --- F1 ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('tool,params', [('opening_range_breakout', ORB), ('asia_range_breakout', ASIA)])
def test_f1_utc_cycle_masked_others_byte_identical(tool, params, monkeypatch, tmp_path):
    full = f1_post(monkeypatch, tmp_path, f1_series('2026-08-29'), tool, params)
    body = f1_post(monkeypatch, tmp_path, f1_series('2026-08-29').drop(GAP), tool, params)
    assert full['result_status'] == 'success' and len(full['trades']) == DAYS, full
    assert 'range_main_gaps' not in full['assumptions']
    assert body['result_status'] == 'success', body
    lo, hi = pd.Timestamp('2026-08-31', tz='UTC'), pd.Timestamp('2026-09-01', tz='UTC')
    assert not [t for t in opened(body) if lo <= t < hi] and len(body['trades']) == DAYS - 1
    assert kept(body, lo, hi) == kept(full, lo, hi)
    assert body['assumptions']['range_main_gaps'] == {
        'tolerance_ratio': p.CENTRAL_GAP_TOLERANCE_RATIO, 'gap_count': 1, 'missing_bars': 9,
        'segments': [{'after': '2026-08-31T03:00:00+00:00', 'before': '2026-08-31T13:00:00+00:00', 'missing_bars': 9}],
        'masked_cycles': [{'cycle_start_utc': '2026-08-31T00:00:00+00:00', 'local_date': '2026-08-31'}],
        'masked': 'whole_local_range_cycle_no_range_no_entry'}
    day = next(d for d in body['raw_report']['range_breakout_days'] if d['date'] == '2026-08-31')
    assert (day['available'], day['triggered'], day['masked_reason']) == (False, False, 'main_range_gap')
    assert all('masked_reason' not in d for d in full['raw_report']['range_breakout_days'])


@pytest.mark.parametrize('params', [SHANGHAI, UTC_1600], ids=['asia_shanghai', 'utc_range_start_1600'])
def test_f1_local_cycle_not_utc_day_is_masked(params, monkeypatch, tmp_path):
    # Asia/Shanghai cycle of local 08-31 = [08-30 16:00, 08-31 16:00) UTC holds the whole gap; the UTC day
    # 08-31 would instead block local 09-01 (starts 08-31 16:00 UTC) and let local 08-31 hold across the gap.
    start = '2026-08-28 16:00Z'
    full = f1_post(monkeypatch, tmp_path, f1_series(start), params=params, start=start)
    body = f1_post(monkeypatch, tmp_path, f1_series(start).drop(GAP), params=params, start=start)
    assert full['result_status'] == 'success' and len(full['trades']) == DAYS, full
    assert body['result_status'] == 'success', body
    lo, hi = pd.Timestamp('2026-08-30 16:00', tz='UTC'), pd.Timestamp('2026-08-31 16:00', tz='UTC')
    assert not [t for t in opened(body) if lo <= t < hi]
    assert pd.Timestamp('2026-08-31 18:00', tz='UTC') in opened(body)  # local 09-01 02:00 still trades
    assert kept(body, lo, hi) == kept(full, lo, hi) and len(body['trades']) == DAYS - 1
    assert body['assumptions']['range_main_gaps']['masked_cycles'] == [
        {'cycle_start_utc': '2026-08-30T16:00:00+00:00',
         'local_date': '2026-08-31' if params is SHANGHAI else '2026-08-30'}]


def test_f1_flatten_fill_on_next_cycle_masks_previous(monkeypatch, tmp_path):
    # flatten_at == range_start: the flatten close fills at the next cycle's first open; a hole there would
    # fill the previous cycle's exit late, so that previous cycle is masked too.
    params = {'range_minutes': 60, 'flatten_at': '00:00'}
    hole = pd.DatetimeIndex([pd.Timestamp('2026-08-31 00:00', tz='UTC')])
    body = f1_post(monkeypatch, tmp_path, f1_series('2026-08-29').drop(hole), params=params)
    assert body['result_status'] == 'success', body
    assert [c['local_date'] for c in body['assumptions']['range_main_gaps']['masked_cycles']] == [
        '2026-08-30', '2026-08-31']
    assert not [t for t in opened(body) if pd.Timestamp('2026-08-30', tz='UTC') <= t < pd.Timestamp('2026-09-01', tz='UTC')]


def test_f1_time_layer_on_uses_gap_tolerant_context(monkeypatch, tmp_path):
    params = {**ORB, 'time_layer_enabled': True, 'time_session_start': '00:00', 'time_session_end': '23:00'}
    body = f1_post(monkeypatch, tmp_path, f1_series('2026-08-29').drop(GAP), params=params)
    assert body['result_status'] == 'success', body
    assert len(body['trades']) == DAYS - 1


def test_f1_beyond_tolerance_fails_with_details(monkeypatch, tmp_path):
    hole = pd.date_range('2026-08-31 00:00', periods=13, freq='1h', tz='UTC')  # 107 of 120 < 108
    body = f1_post(monkeypatch, tmp_path, f1_series('2026-08-29').drop(hole))
    assert (body['result_status'], body['error_type'], body['error_message']) == (
        'failed', 'TIME_DATA_GAP', 'TIME_DATA_GAP:range breakout main range gaps exceed tolerance'), body
    assert body['limitations'] == {'reason': 'time_data_gap', 'gap_count': 1, 'missing_bars': 13, 'segments': [
        {'after': '2026-08-30T23:00:00+00:00', 'before': '2026-08-31T13:00:00+00:00', 'missing_bars': 13}]}


def test_f1_leading_missing_still_incomplete(monkeypatch, tmp_path):
    data = f1_series('2026-08-29')
    body = f1_post(monkeypatch, tmp_path, data.iloc[1:])
    assert (body['error_type'], body['limitations']) == ('INSUFFICIENT_DATA', {'reason': 'time_history_incomplete'})


# --- F2 ------------------------------------------------------------------------------------------------

def f2_series(days=DAYS):
    index = pd.date_range('2026-08-29', periods=24 * days, freq='1h', tz='UTC')
    return pd.DataFrame(dict(Open=100., High=101., Low=99., Close=100., Volume=1.), index=index)


def f2_post(monkeypatch, tmp_path, data, params):
    return post(monkeypatch, tmp_path, data, 'calendar_schedule', params, '2026-08-29 00:00Z',
                pd.Timestamp('2026-08-29 00:00Z') + pd.Timedelta(days=DAYS))


def events(body):
    return {e['event_utc'][:10]: (e['status'], e['reason']) for e in body['raw_report']['calendar_events']}


def test_f2_holding_window_touching_gap_masked(monkeypatch, tmp_path):
    # 22:00 entry + 360 min: the 08-30 entry would exit-fill at 08-31 04:00, the first missing candle.
    params = {'time_entry_at': '22:00', 'time_max_holding_minutes': 360}
    full = f2_post(monkeypatch, tmp_path, f2_series(), params)
    body = f2_post(monkeypatch, tmp_path, f2_series().drop(GAP), params)
    assert full['result_status'] == 'success' and len(full['trades']) == DAYS, full
    assert 'calendar_main_gaps' not in full['assumptions']
    assert body['result_status'] == 'success', body
    got = events(body)
    assert got['2026-08-30'] == ('skipped', 'main_range_gap_holding_window')
    assert got['2026-08-31'] == ('skipped', 'main_range_gap_day')
    assert {got[d][0] for d in ('2026-08-29', '2026-09-01', '2026-09-02')} == {'filled'}
    lo, hi = pd.Timestamp('2026-08-30 12:00', tz='UTC'), pd.Timestamp('2026-09-01', tz='UTC')
    assert kept(body, lo, hi) == kept(full, lo, hi) and len(body['trades']) == DAYS - 2
    assert body['assumptions']['calendar_main_gaps'] == {
        'tolerance_ratio': p.CENTRAL_GAP_TOLERANCE_RATIO, 'gap_count': 1, 'missing_bars': 9,
        'segments': [{'after': '2026-08-31T03:00:00+00:00', 'before': '2026-08-31T13:00:00+00:00', 'missing_bars': 9}],
        'masked_local_days': ['2026-08-31'],
        'masked_events': [{'event_utc': '2026-08-30T22:00:00+00:00', 'reason': 'main_range_gap_holding_window'},
                          {'event_utc': '2026-08-31T22:00:00+00:00', 'reason': 'main_range_gap_day'}],
        'masked': 'gap_local_day_no_entry_and_entries_whose_holding_window_touches_gap'}


def test_f2_holding_window_clear_of_gap_still_trades(monkeypatch, tmp_path):
    # 22:00 + 300 min exit-fills 08-31 03:00, the last candle before the hole: the 08-30 entry runs.
    params = {'time_entry_at': '22:00', 'time_max_holding_minutes': 300}
    full = f2_post(monkeypatch, tmp_path, f2_series(), params)
    body = f2_post(monkeypatch, tmp_path, f2_series().drop(GAP), params)
    assert body['result_status'] == 'success', body
    got = events(body)
    assert got['2026-08-30'][0] == 'filled' and got['2026-08-31'] == ('skipped', 'main_range_gap_day')
    lo, hi = pd.Timestamp('2026-08-31 12:00', tz='UTC'), pd.Timestamp('2026-09-01', tz='UTC')
    assert kept(body, lo, hi) == kept(full, lo, hi) and len(body['trades']) == DAYS - 1
    assert [e['reason'] for e in body['assumptions']['calendar_main_gaps']['masked_events']] == ['main_range_gap_day']


def test_f2_flatten_window_touching_gap_masked(monkeypatch, tmp_path):
    # Flatten at 05:00 local: the 08-30 22:00 entry would hold until the 08-31 05:00 cutoff, inside the hole.
    params = {'time_entry_at': '22:00', 'time_flatten_at': '05:00'}
    body = f2_post(monkeypatch, tmp_path, f2_series().drop(GAP), params)
    assert body['result_status'] == 'success', body
    assert events(body)['2026-08-30'] == ('skipped', 'main_range_gap_holding_window')


def test_f2_beyond_tolerance_fails_with_details(monkeypatch, tmp_path):
    hole = pd.date_range('2026-08-31 00:00', periods=13, freq='1h', tz='UTC')
    body = f2_post(monkeypatch, tmp_path, f2_series().drop(hole), {'time_entry_at': '22:00', 'time_max_holding_minutes': 60})
    assert (body['result_status'], body['error_type'], body['error_message']) == (
        'failed', 'TIME_DATA_GAP', 'TIME_DATA_GAP:calendar main range gaps exceed tolerance'), body
    assert body['limitations'] == {'reason': 'time_data_gap', 'gap_count': 1, 'missing_bars': 13, 'segments': [
        {'after': '2026-08-30T23:00:00+00:00', 'before': '2026-08-31T13:00:00+00:00', 'missing_bars': 13}]}


def test_f2_off_grid_open_still_fails(monkeypatch, tmp_path):
    data = f2_series()
    data.index = data.index.where(data.index != data.index[50], data.index[50] + pd.Timedelta(minutes=30))
    body = f2_post(monkeypatch, tmp_path, data, {'time_entry_at': '22:00', 'time_max_holding_minutes': 60})
    assert body['error_type'] == 'TIME_DATA_GAP', body
