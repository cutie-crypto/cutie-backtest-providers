"""P-LOW1 provider low-severity closure.

1. ORB / Asia / calendar same-timeframe filter: a non-empty warmup prefix must be gap-free and end one bar
   before the main range, else INSUFFICIENT_DATA/filter_history_insufficient; an empty prefix passes.
2. calendar event rejected by sizing ends `skipped/sizing_rejected`; sizing off keeps calendar_events.
3. futures-only templates reject spot with a text that names no template family, before any fetch.
4. US open sizing assumption names the nearest-of-window-and-user-stop rule; other labels unchanged.
5. calendar_stop_enabled=false + risk sizing is rejected before any fetch (same shape as red streak).
"""
import json
from pathlib import Path
import sys

import pandas as pd
import pytest
from backtesting import Backtest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent / 'fixtures'))
import _10b2d_cases as c  # noqa: E402
import capture_7p3b2_off_c72c4a1 as off2  # noqa: E402
import cutie_backtesting_provider as p  # noqa: E402

RISK = dict(position_size_risk_pct=1, position_size_qty_step=0.001)


def sizing(body):
    return body['raw_report']['position_sizing']


def _forbid_fetch(monkeypatch):
    calls = []

    def fetch(*a, **k):
        calls.append(a)
        raise AssertionError('fetched before validation')

    monkeypatch.setattr(p, '_fetch_ohlcv', fetch)
    monkeypatch.setattr(p, '_fetch_template_warmup', fetch)
    return calls


def _post_without_fetch(monkeypatch, tmp_path, name, params, market='futures', timeframe='1h'):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', tmp_path)
    calls = _forbid_fetch(monkeypatch)
    request = dict(run_id='plow1', provider_tool_id='local.backtesting_py.' + name, provider_params=params,
                   symbol='BTCUSDT', market=market, timeframe=timeframe, start_at=1772323200,
                   end_at=1772323200 + 86400 * 3, initial_capital='10000', fee_bps='0', slippage_bps='0')
    body = TestClient(p.app).post('/cutie/backtest', json={'backtest': request}).json()
    return body, calls


# --- 5. calendar stop disabled + risk sizing: rejected at validation ---------------------------------

def test_calendar_stop_disabled_risk_sizing_rejected_before_fetch(monkeypatch, tmp_path):
    params = {**c.SETUP['calendar_schedule'][2], **RISK, 'calendar_stop_enabled': False}
    body, calls = _post_without_fetch(monkeypatch, tmp_path, 'calendar_schedule', params)
    assert body['result_status'] == 'failed' and body['error_type'] == 'INVALID_PARAMS'
    assert sizing(body) == {'rejections': [{'reason': 'missing_initial_stop'}]}
    assert calls == []


def test_calendar_stop_enabled_risk_sizing_passes_validation(monkeypatch, tmp_path):
    body = c.post(monkeypatch, tmp_path, 'calendar_schedule', RISK)
    assert body['result_status'] == 'success', body.get('error_message')
    assert len(sizing(body)['fills']) == 1


def test_calendar_stop_disabled_pct_sizing_still_runs(monkeypatch, tmp_path):
    body = c.post(monkeypatch, tmp_path, 'calendar_schedule',
                  dict(position_size_pct=50, position_size_qty_step=0.001, calendar_stop_enabled=False))
    assert body['result_status'] == 'success', body.get('error_message')
    assert sizing(body)['rejections'] == []
    assert len(body['trades']) == 1


# --- 4. US open sizing assumption label names the nearest-of rule ------------------------------------

US_OPEN_LABEL = 'nearest_of_window_and_user_stop_actual_fill_distance'


@pytest.mark.parametrize('extra,stop', [
    # user 100 * 0.985 = 98.5 is nearer than the window low 98 (hand arithmetic, fill open 100)
    (dict(stop_loss_pct=1.5), '98.5'),
    # no user stop: only the window low 98 frozen at the signal
    ({}, '98.0'),
])
def test_us_open_sizing_label_and_stop(extra, stop, monkeypatch, tmp_path):
    from decimal import Decimal
    body = c.post(monkeypatch, tmp_path, 'us_open_momentum', {**RISK, **extra})
    assert body['result_status'] == 'success', body.get('error_message')
    assert body['assumptions']['position_sizing']['initial_stop'] == US_OPEN_LABEL
    assert Decimal(sizing(body)['fills'][0]['initial_stop']) == Decimal(stop)


@pytest.mark.parametrize('name,label', [
    ('opening_range_breakout', 'template_frozen_signal_stop_actual_fill_distance'),
    ('asia_range_breakout', 'template_frozen_signal_stop_actual_fill_distance'),
    ('calendar_schedule', 'template_frozen_signal_stop_actual_fill_distance'),
    ('red_streak_rsi', 'template_frozen_signal_stop_actual_fill_distance'),
    ('cme_weekend_gap', 'shared_frozen_actual_fill_risk_state'),
])
def test_other_sizing_labels_unchanged(name, label, monkeypatch, tmp_path):
    body = c.post(monkeypatch, tmp_path, name, RISK)
    assert body['result_status'] == 'success', body.get('error_message')
    assert body['assumptions']['position_sizing']['initial_stop'] == label


# --- 2. calendar event rejected by sizing leaves `submitted` -----------------------------------------

def two_day_calendar(second_day_price=40.):
    import test_f2_calendar as f2
    data = f2.frame(count=48)  # events 01-01 02:00 (signal bar 1, fill bar 2) and 01-02 02:00 (bars 25, 26)
    data.iloc[24:] = [second_day_price, second_day_price * 1.01, second_day_price * 0.99, second_day_price, 1.]
    return data


def test_calendar_sizing_rejected_event_marked_skipped_then_next_fills(monkeypatch, tmp_path):
    # notional 50, step 1: day 1 at 100 -> floor(0.5) = 0 < step (quantity_below_step);
    # day 2 at 40 -> floor(1.25) = 1 unit fills.
    body = c.post(monkeypatch, tmp_path, 'calendar_schedule',
                  dict(position_size_notional=50, position_size_qty_step=1), data=two_day_calendar())
    assert body['result_status'] == 'success', body.get('error_message')
    first, second = body['raw_report']['calendar_events']
    assert (first['status'], first['reason']) == ('skipped', 'sizing_rejected')
    assert second['status'] == 'filled'
    assert [r['reason'] for r in sizing(body)['rejections']] == ['quantity_below_step']
    assert [t['qty'] for t in body['trades']] == ['1']


def test_calendar_sizing_off_events_match_base_golden(monkeypatch, tmp_path):
    import json
    for label, (params, side) in c.GOLDEN_VARIANTS['calendar_schedule'].items():
        golden = json.loads((c.GOLDEN_DIR / f'calendar_schedule.{label}.json').read_text())
        body = c.post(monkeypatch, tmp_path, 'calendar_schedule', params, side=side)
        assert c.canonical(body['raw_report']['calendar_events']) == c.canonical(
            golden['raw_report']['calendar_events']), label


def test_calendar_sizing_off_two_day_events_hand_expected(monkeypatch, tmp_path):
    body = c.post(monkeypatch, tmp_path, 'calendar_schedule', {}, data=two_day_calendar())
    assert [(e['status'], e['reason']) for e in body['raw_report']['calendar_events']] == [
        ('filled', None), ('filled', None)]


# --- 3. futures-only templates: spot rejection text names no template family ------------------------

FUTURES_ONLY = sorted(t.removeprefix('local.backtesting_py.') for t, s in p.TOOL_SPECS.items()
                      if s.get('markets') == ['futures'])
# The bearish divergences hit their own earlier spot gate (cutie_backtesting_provider.py, "bearish
# divergence requires futures market") before the generic markets gate; that text is unchanged.
EARLIER_GATE = {'macd_bearish_divergence': 'bearish divergence requires futures market',
                'rsi_bearish_divergence': 'bearish divergence requires futures market'}


def test_futures_only_set_is_the_known_five():
    assert FUTURES_ONLY == ['chan_3sell', 'double_top', 'head_shoulders', 'macd_bearish_divergence',
                            'rsi_bearish_divergence']


@pytest.mark.parametrize('name', FUTURES_ONLY)
def test_futures_only_template_spot_rejected_before_fetch(name, monkeypatch, tmp_path):
    body, calls = _post_without_fetch(monkeypatch, tmp_path, name, {}, market='spot')
    assert body['result_status'] == 'failed' and body['error_type'] == 'INVALID_PARAMS'
    assert body['error_message'] == EARLIER_GATE.get(name, 'this template requires futures market')
    assert 'top pattern' not in body['error_message']
    assert calls == []


# --- 1. F1/F2 same-timeframe filter warmup must be contiguous and adjacent ----------------------------

EMA10 = dict(filter_layer_enabled=True, filter_ema_enabled=True, filter_ema_period=10)
SCHEDULE = dict(time_entry_at='02:00', time_max_holding_minutes=120)
# tool -> (params, frame, timeframe, market); every main frame has >= 10 bars, so the count check passes.
F12 = {
    'opening_range_breakout': ({}, off2.range_frame, '15m', 'futures'),
    'asia_range_breakout': ({}, off2.asia_frame, '15m', 'futures'),
    'calendar_schedule': (SCHEDULE, off2.calendar_frame, '1h', 'spot'),
}


def flat(close, count, last_open, freq):
    index = pd.date_range(end=last_open, periods=count, freq=freq)
    return pd.DataFrame(dict(Open=[close] * count, High=[close + 1] * count, Low=[close - 1] * count,
                             Close=[close] * count, Volume=[1.] * count), index=index)


def run_f12(monkeypatch, tmp_path, tool, params, data, timeframe, market, prefix, source=None):
    calls = []

    def warmup(*args):
        calls.append(args[5])
        return prefix.copy()

    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda ex, mk, sym, tf, since, until:
                        (data if tf == timeframe else source).copy())
    monkeypatch.setattr(p, '_fetch_template_warmup', warmup)
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **kw: None)
    step = pd.Timedelta(timeframe)
    request = dict(run_id='plow1', provider_tool_id='local.backtesting_py.' + tool, provider_params=params,
                   symbol='BTCUSDT', market=market, timeframe=timeframe, start_at=int(data.index[0].timestamp()),
                   end_at=int((data.index[-1] + step).timestamp()), initial_capital='10000', fee_bps='0',
                   slippage_bps='0')
    return TestClient(p.app).post('/cutie/backtest', json={'backtest': request}).json(), calls


def prefix_for(kind, data, timeframe):
    step = pd.Timedelta(timeframe)
    if kind == 'empty':
        return data.iloc[:0]
    if kind == 'contiguous':
        return flat(50., 12, data.index[0] - step, step)
    if kind == 'inner_gap':  # 12 bars ending right before the main range, bar 5 missing: 11 >= 10 left
        full = flat(50., 12, data.index[0] - step, step)
        return full.drop(full.index[5])
    assert kind == 'detached'  # 12 contiguous bars, but one bar missing between prefix and main range
    return flat(50., 12, data.index[0] - 2 * step, step)


@pytest.mark.parametrize('tool', sorted(F12))
@pytest.mark.parametrize('kind', ['inner_gap', 'detached'])
def test_f12_filter_warmup_with_gap_fails_insufficient(tool, kind, monkeypatch, tmp_path):
    params, make, timeframe, market = F12[tool]
    data = make()
    body, calls = run_f12(monkeypatch, tmp_path, tool, {**params, **EMA10}, data, timeframe, market,
                          prefix_for(kind, data, timeframe))
    assert calls == [10]
    assert body['result_status'] == 'failed' and body['error_type'] == 'INSUFFICIENT_DATA', body
    assert 'filter_history_insufficient' in json.dumps(body)
    assert body['error_message'] == 'Entry filter indicator history has gaps'


@pytest.mark.parametrize('tool', sorted(F12))
@pytest.mark.parametrize('kind,bars', [('contiguous', 12), ('empty', 0)])
def test_f12_filter_warmup_contiguous_or_empty_runs(tool, kind, bars, monkeypatch, tmp_path):
    params, make, timeframe, market = F12[tool]
    data = make()
    assert len(data) >= 10
    body, calls = run_f12(monkeypatch, tmp_path, tool, {**params, **EMA10}, data, timeframe, market,
                          prefix_for(kind, data, timeframe))
    assert calls == [10]
    assert body['result_status'] == 'success', body
    assert body['assumptions']['indicator_warmup_bars'] == bars


def flat_day():
    return pd.DataFrame(dict(Open=[100.] * 24, High=[101.] * 24, Low=[99.] * 24, Close=[100.] * 24,
                             Volume=[1.] * 24), index=pd.date_range('2026-01-01', periods=24, freq='h'))


@pytest.mark.parametrize('entry_at,bar,status,reason', [
    # An HH:00 event is bar HH-1's close. EMA10 needs 10 bars: with no prefix, main bars 0..8 are blocked
    # whatever the price; bar 9 (10:00 event) is the first judgable bar, and close 102 > EMA of 100s+102.
    ('01:00', 0, 'skipped', 'entry_filter'),
    ('09:00', 8, 'skipped', 'entry_filter'),
    ('10:00', 9, 'filled', None),
])
def test_f12_empty_warmup_blocks_first_required_bars_minus_one(entry_at, bar, status, reason, monkeypatch,
                                                                tmp_path):
    data = flat_day()
    data.iloc[bar] = [100., 103., 99., 102., 1.]
    body, calls = run_f12(monkeypatch, tmp_path, 'calendar_schedule',
                          dict(time_entry_at=entry_at, time_max_holding_minutes=60, **EMA10),
                          data, '1h', 'spot', data.iloc[:0])
    assert calls == [10] and body['result_status'] == 'success', body
    first = body['raw_report']['calendar_events'][0]
    assert (first['status'], first['reason']) == (status, reason)
    assert bool(body['trades']) is (status == 'filled')


def test_f12_bar8_event_passes_with_contiguous_prefix(monkeypatch, tmp_path):
    # Contrast for the bar-8 row above: a contiguous 20-bar prefix at 50 makes bar 8 judgable and 102 passes.
    data = flat_day()
    data.iloc[8] = [100., 103., 99., 102., 1.]
    body, _ = run_f12(monkeypatch, tmp_path, 'calendar_schedule',
                      dict(time_entry_at='09:00', time_max_holding_minutes=60, **EMA10),
                      data, '1h', 'spot', flat(50., 20, data.index[0] - pd.Timedelta('1h'), 'h'))
    assert body['result_status'] == 'success', body
    assert body['raw_report']['calendar_events'][0]['status'] == 'filled'


GOLDEN2 = json.loads((Path(__file__).parent / 'fixtures/7p3b2_off_c72c4a1.json').read_text())


@pytest.mark.parametrize('case', [k for k in off2.cases() if k.split('/')[0] in F12])
def test_f12_off_state_byte_identical_to_golden(case):
    # snapshot() raises if F1/F2 fetch any indicator warmup while the filter is off.
    assert off2.snapshot(*off2.cases()[case]) == GOLDEN2['cases'][case]


def test_f12_filter_timeframe_skips_same_timeframe_warmup(monkeypatch, tmp_path):
    # Multi-timeframe filter: filter_warmup is 0, so the same-timeframe warmup (gapped here) is never fetched.
    data = off2.calendar_frame()
    source = flat(100., 60, pd.Timestamp('2026-01-02'), '4h')
    body, calls = run_f12(monkeypatch, tmp_path, 'calendar_schedule',
                          {**SCHEDULE, **EMA10, 'filter_timeframe': '4h'}, data, '1h', 'spot',
                          prefix_for('inner_gap', data, '1h'), source)
    assert calls == []
    assert body['result_status'] == 'success', body
    assert body['raw_report']['entry_filters']['timeframe'] == '4h'
