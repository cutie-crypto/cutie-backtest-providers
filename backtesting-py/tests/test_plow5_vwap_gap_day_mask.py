"""P-LOW5-VWAP: a main-range gap within CENTRAL_GAP_TOLERANCE_RATIO masks every UTC day holding a missing
candle (no entries, VWAP not accumulated, restarts next UTC day); beyond it the run fails with details.

Each UTC day of the synthetic 1h series trades exactly once without a mask: 00:00 close 100, 01:00 close 90
(VWAP 95, 90 <= 95 * 0.985) signals, entry fills 02:00 open 100, close 100 >= VWAP exits at 03:00.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd
import pytest
from backtesting import Backtest
from fastapi.testclient import TestClient

import cutie_backtesting_provider as p

TOOL = 'local.backtesting_py.vwap_reversion'
DAYS = 5
GAP = pd.date_range('2026-08-31 04:00', '2026-08-31 12:00', freq='1h', tz='UTC')  # the 9 central-source bars


def series(days=DAYS):
    index = pd.date_range('2026-08-29', periods=24 * days, freq='1h', tz='UTC')
    close = [90.0 if t.hour == 1 else 100.0 for t in index]
    return pd.DataFrame(dict(Open=close, High=close, Low=close, Close=close, Volume=1.0), index=index)


def post(monkeypatch, tmp_path, data, params=None, days=DAYS):
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a: data.copy())
    monkeypatch.setattr(p, '_fetch_template_warmup', lambda *a: data.iloc[:0])
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **kw: None)
    full = series(days)
    request = dict(run_id='plow5', provider_tool_id=TOOL, provider_params=params or {}, symbol='BTCUSDT',
                   market='spot', timeframe='1h', start_at=int(full.index[0].timestamp()),
                   end_at=int(full.index[-1].timestamp()) + 3600, initial_capital='10000', fee_bps='0',
                   slippage_bps='0')
    return TestClient(p.app).post('/cutie/backtest', json={'backtest': request}).json()


def day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()


def test_contiguous_baseline_trades_every_day(monkeypatch, tmp_path):
    body = post(monkeypatch, tmp_path, series())
    assert body['result_status'] == 'success', body
    assert sorted({day(t['opened_at']) for t in body['trades']}) == [
        '2026-08-29', '2026-08-30', '2026-08-31', '2026-09-01', '2026-09-02']
    assert 'vwap_main_gaps' not in body['assumptions']


def test_gap_day_masked_other_days_byte_identical(monkeypatch, tmp_path):
    full = post(monkeypatch, tmp_path, series())
    body = post(monkeypatch, tmp_path, series().drop(GAP))
    assert body['result_status'] == 'success', body
    assert '2026-08-31' not in {day(t['opened_at']) for t in body['trades']}
    assert body['assumptions']['vwap_main_gaps'] == {
        'tolerance_ratio': p.CENTRAL_GAP_TOLERANCE_RATIO, 'gap_count': 1, 'missing_bars': 9,
        'segments': [{'after': '2026-08-31T03:00:00+00:00', 'before': '2026-08-31T13:00:00+00:00', 'missing_bars': 9}],
        'masked_utc_days': ['2026-08-31'], 'masked': 'whole_utc_day_no_entry_vwap_restarts_next_utc_day'}

    def kept(b):
        # seq renumbers by one after the dropped trade; every other field is byte-identical.
        return json.dumps([{k: v for k, v in t.items() if k != 'seq'} for t in b['trades']
                           if day(t['opened_at']) != '2026-08-31'], sort_keys=True)
    assert kept(body) == kept(full) and len(body['trades']) == DAYS - 1


def test_pre_gap_bars_of_masked_day_do_not_feed_vwap():
    # 08-31 01:00 would signal (90 vs VWAP 95) if the pre-gap bars accumulated; the day is masked whole.
    data = series().drop(GAP)
    data.index = data.index.tz_localize(None)
    cls = p.TOOL_SPECS[TOOL]['build']({})['strategy']
    cls._f5_step_ms = 3600000
    cls._f5_gap_days = (datetime(2026, 8, 31).date(),)
    stats = Backtest(data, cls, cash=100000, exclusive_orders=True).run()
    vwap = pd.Series(stats['_strategy']._f5_vwap, index=data.index)
    assert vwap['2026-08-31'].isna().all()
    # Next day restarts from its own first bar: 00:00 hlc3 100, then (100 + 90) / 2 at 01:00.
    assert list(vwap['2026-09-01'].iloc[:2]) == [100.0, 95.0]
    assert list(vwap['2026-08-30'].iloc[:2]) == [100.0, 95.0]


def test_gap_across_midnight_masks_both_days(monkeypatch, tmp_path):
    gap = pd.date_range('2026-08-30 22:00', '2026-08-31 01:00', freq='1h', tz='UTC')
    body = post(monkeypatch, tmp_path, series().drop(gap))
    assert body['result_status'] == 'success', body
    assert body['assumptions']['vwap_main_gaps']['masked_utc_days'] == ['2026-08-30', '2026-08-31']
    assert not {'2026-08-30', '2026-08-31'} & {day(t['opened_at']) for t in body['trades']}


def test_time_layer_on_uses_gap_tolerant_context(monkeypatch, tmp_path):
    params = {'time_layer_enabled': True, 'time_session_start': '00:00', 'time_session_end': '06:00'}
    body = post(monkeypatch, tmp_path, series().drop(GAP), params)
    assert body['result_status'] == 'success', body
    assert sorted({day(t['opened_at']) for t in body['trades']}) == ['2026-08-29', '2026-08-30', '2026-09-01', '2026-09-02']


def test_beyond_tolerance_fails_with_details(monkeypatch, tmp_path):
    gap = pd.date_range('2026-08-31 00:00', periods=13, freq='1h', tz='UTC')  # 107 of 120 < 108
    body = post(monkeypatch, tmp_path, series().drop(gap))
    assert (body['result_status'], body['error_type'], body['error_message']) == (
        'failed', 'TIME_DATA_GAP', 'TIME_DATA_GAP:VWAP main range gaps exceed tolerance'), body
    assert body['limitations'] == {'reason': 'time_data_gap', 'gap_count': 1, 'missing_bars': 13, 'segments': [
        {'after': '2026-08-30T23:00:00+00:00', 'before': '2026-08-31T13:00:00+00:00', 'missing_bars': 13}]}


def test_tolerance_boundary_twelve_missing_runs(monkeypatch, tmp_path):
    gap = pd.date_range('2026-08-31 00:00', periods=12, freq='1h', tz='UTC')  # 108 == 120 * 0.9
    body = post(monkeypatch, tmp_path, series().drop(gap))
    assert body['result_status'] == 'success', body
    assert body['assumptions']['vwap_main_gaps']['missing_bars'] == 12


@pytest.mark.parametrize('shift', [pd.Timedelta(minutes=30), pd.Timedelta(0)])
def test_off_grid_or_duplicate_open_still_fails(monkeypatch, tmp_path, shift):
    data = series()
    if shift:
        data.index = data.index.where(data.index != data.index[50], data.index[50] + shift)
    else:
        data = pd.concat([data.iloc[:51], data.iloc[50:]])
    body = post(monkeypatch, tmp_path, data)
    assert (body['error_type'], body['error_message']) == (
        'TIME_DATA_GAP', 'TIME_DATA_GAP:VWAP requires a complete UTC candle grid'), body


def test_whole_utc_day_missing_within_tolerance_runs(monkeypatch, tmp_path):
    # pi critical: a masked day with no candle at all must still split the VWAP runs (264 >= 288 * 0.9).
    whole = pd.date_range('2026-08-31', periods=24, freq='1h', tz='UTC')
    body = post(monkeypatch, tmp_path, series(12).drop(whole), days=12)
    assert body['result_status'] == 'success', body
    gaps = body['assumptions']['vwap_main_gaps']
    assert (gaps['missing_bars'], gaps['masked_utc_days']) == (24, ['2026-08-31'])
    opened = {day(t['opened_at']) for t in body['trades']}
    assert '2026-08-31' not in opened and {'2026-08-30', '2026-09-01'} <= opened
