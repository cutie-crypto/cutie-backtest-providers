"""P-LOW3: same-timeframe entry filter on -> main-range gaps are judged.

- gap share within CENTRAL_GAP_TOLERANCE_RATIO (missing <= 10%): the run goes on; the filter mask is
  re-warmed after each gap (signal bar before the gap + required_bars - 1 bars after it are blocked)
  and assumptions.entry_filter_main_gaps lists the segments;
- beyond it: INSUFFICIENT_DATA / filter_history_insufficient with gap_count / missing_bars /
  first_gap_after / first_gap_segment in limitations;
- filter off, filter_timeframe, other failures: byte-identical to 451b879 (fixtures/plow3_451b879.json).
Template table and request shape reuse P-LOW2a (`capture_plow2a_b48e65e`) and P-LOW1 (`test_plow1.F12`).
"""
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / 'fixtures'))
import capture_plow2a_b48e65e as cap  # noqa: E402
import capture_plow3_451b879 as base  # noqa: E402
import cutie_backtesting_provider as p  # noqa: E402
import test_plow1 as plow1  # noqa: E402

GOLDEN = json.loads((Path(__file__).parent / 'fixtures/plow3_451b879.json').read_text())
TOOLS = cap.tools()
# VWAP has its own grid gate before the filter guard (P-LOW5-VWAP: within tolerance it masks whole UTC days).
EARLIER_GRID_GATE = {'vwap_reversion'}


def iso(stamp):
    return pd.Timestamp(stamp).tz_localize('UTC').isoformat()


def run(monkeypatch, tmp_path, tool, data, warm, **extra):
    freq = cap.TIMEFRAME.get(tool, '1h')
    params = {**cap.params_for(tool), **cap.EMA10, **extra}
    return base.post(monkeypatch, tmp_path, tool, params, data, warm, timeframe={'1h': '1h', '30min': '30m'}[freq])


def test_template_table():
    # 43 at P-LOW3; SHORT-PAT-4 adds bearish_engulfing / shooting_star / evening_star => 46.
    assert len(TOOLS) == 46 and set(EARLIER_GRID_GATE) <= set(TOOLS)
    assert len(plow1.F12) == 3


# --- 2026-08-31 binance 1h outage: 04:00-12:00 UTC (9 bars) missing -------------------------------------

AUG31_GAP = pd.date_range('2026-08-31 04:00', '2026-08-31 12:00', freq='h')
EMA_PERIOD = 10  # filter_ema_period=10 -> required_bars = 10


def aug31_frame():
    """60-bar warmup + 240-bar main range (2026-08-25 .. 09-03). Oscillating with drift before 08-29 so
    ema_cross trades; strictly rising from 08-29 so close > EMA10 on every bar around the gap."""
    index = pd.date_range('2026-08-22 12:00', periods=300, freq='h')
    t = np.arange(len(index), dtype='float64')
    close = 100 + 0.2 * t + 4 * np.sin(t / 4)
    k0 = index.get_loc(pd.Timestamp('2026-08-29'))
    close[k0:] = close[k0 - 1] + 1 + np.arange(len(index) - k0)
    frame = pd.DataFrame(dict(Open=np.r_[close[0], close[:-1]], Close=close, Volume=1.), index=index)
    frame['High'] = frame[['Open', 'Close']].max(axis=1) + .5
    frame['Low'] = frame[['Open', 'Close']].min(axis=1) - .5
    return frame.iloc[:60], frame.iloc[60:]


def run_aug31(monkeypatch, tmp_path, gap):
    warm, data = aug31_frame()
    if gap:
        data = data.drop(AUG31_GAP)
    allowed = {}
    original = p._FilterLayerMixin._filter_init

    def spy(self):  # record what the signal-close adapter answers for every main-range bar
        original(self)
        allowed.update({pd.Timestamp(self.data.index[k]): self._filter_allow_entry(bar=k)
                        for k in range(len(self.data))})
    monkeypatch.setattr(p._FilterLayerMixin, '_filter_init', spy)
    body = base.post(monkeypatch, tmp_path, 'ema_cross',
                     {**cap.params_for('ema_cross'), **cap.EMA10, 'filter_ema_period': EMA_PERIOD}, data, warm)
    return body, allowed, data


def test_aug31_gap_within_tolerance_rewarms_mask(monkeypatch, tmp_path):
    body, allowed, data = run_aug31(monkeypatch, tmp_path, gap=True)
    assert len(data) == 231 and len(data) >= (len(data) + 9) * p.CENTRAL_GAP_TOLERANCE_RATIO
    assert body['result_status'] == 'success', body
    assert body['assumptions']['entry_filter_main_gaps'] == {
        'tolerance_ratio': 0.9, 'missing_bars': 9,
        'segments': [{'after': '2026-08-31T03:00:00+00:00', 'before': '2026-08-31T13:00:00+00:00',
                      'missing_bars': 9}],
        'masked': 'signal_bar_before_gap_plus_required_bars_minus_1_after'}
    before = pd.Timestamp('2026-08-31 03:00')
    after = list(pd.date_range('2026-08-31 13:00', periods=EMA_PERIOD - 1, freq='h'))  # 13:00 .. 21:00
    first_open = pd.Timestamp('2026-08-31 22:00')  # 13:00 + (required_bars - 1)h
    assert [allowed[t] for t in [before] + after] == [False] * EMA_PERIOD
    assert allowed[first_open] is True and allowed[pd.Timestamp('2026-08-31 02:00')] is True


def test_aug31_same_bars_pass_without_gap_and_pre_gap_trades_identical(monkeypatch, tmp_path):
    gapped, _, _ = run_aug31(monkeypatch, tmp_path, gap=True)
    body, allowed, _ = run_aug31(monkeypatch, tmp_path, gap=False)
    assert body['result_status'] == 'success' and 'entry_filter_main_gaps' not in body['assumptions']
    # Every bar the gapped run blocks would have passed: the block comes from the re-warm, not the price.
    blocked = [pd.Timestamp('2026-08-31 03:00')] + list(pd.date_range('2026-08-31 13:00', periods=9, freq='h'))
    assert all(allowed[t] for t in blocked)
    # Outside the gap: trades closed at or before the last pre-gap bar open, serialized with sorted keys,
    # are byte-identical between the gapped and the contiguous run of the same series.
    cut = int(pd.Timestamp('2026-08-31 03:00', tz='UTC').timestamp())

    def pre(b):
        return json.dumps([t for t in b['trades'] if t['closed_at'] <= cut], sort_keys=True)
    assert json.loads(pre(body)) and pre(gapped) == pre(body)


# --- every filter-capable template --------------------------------------------------------------------

@pytest.mark.parametrize('tool', TOOLS)
def test_one_missing_bar_within_tolerance_runs(tool, monkeypatch, tmp_path):
    warm, data = base.gapped(cap.TIMEFRAME.get(tool, '1h'))
    body = run(monkeypatch, tmp_path, tool, data, warm)
    assert body['result_status'] == 'success', body
    if tool in EARLIER_GRID_GATE:
        assert body['assumptions']['vwap_main_gaps']['missing_bars'] == 1
    gaps = body['assumptions']['entry_filter_main_gaps']
    assert gaps['missing_bars'] == 1 and len(gaps['segments']) == 1


@pytest.mark.parametrize('tool', TOOLS)
def test_beyond_tolerance_fails_with_details(tool, monkeypatch, tmp_path):
    freq = cap.TIMEFRAME.get(tool, '1h')
    warm, data = base.gapped(freq, drop=range(100, 140))  # 320 of 360 bars < 324
    body = run(monkeypatch, tmp_path, tool, data, warm)
    _, full = cap.series(freq)
    if tool in EARLIER_GRID_GATE:
        assert (body['error_type'], body['limitations']) == ('TIME_DATA_GAP', {
            'reason': 'time_data_gap', 'gap_count': 1, 'missing_bars': 40, 'segments': [{
                'after': iso(full.index[99]), 'before': iso(full.index[140]), 'missing_bars': 40}]}), body
        return
    assert (body['result_status'], body['error_type'], body['error_message']) == (
        'failed', 'INSUFFICIENT_DATA', 'Entry filter indicator history has gaps'), body
    assert body['limitations'] == {'reason': 'filter_history_insufficient', 'gap_count': 1, 'missing_bars': 40,
                                   'first_gap_after': iso(full.index[99]), 'first_gap_segment': 'main'}


# F1 (ORB / Asia) fetch the main range strictly and F2 (calendar) builds its clock over it: a main-range gap
# fails there, before the filter guard, whether within the tolerance or not.
F12_SHAPE = {'opening_range_breakout': ('INSUFFICIENT_DATA', 'time_history_incomplete'),
             'asia_range_breakout': ('INSUFFICIENT_DATA', 'time_history_incomplete'),
             'calendar_schedule': ('TIME_DATA_GAP', 'time_data_gap')}


@pytest.mark.parametrize('tool', sorted(plow1.F12))
@pytest.mark.parametrize('share', ['one_bar', 'beyond_tolerance'])
def test_f12_main_gap_fails_before_guard(tool, share, monkeypatch, tmp_path):
    params, make, timeframe, market = plow1.F12[tool]
    data = make()
    n = len(data)
    gapped = data.drop(data.index[n // 2]) if share == 'one_bar' else data.drop(data.index[n // 4:n // 4 + n // 5])
    body, _ = plow1.run_f12(monkeypatch, tmp_path, tool, {**params, **cap.EMA10}, gapped, timeframe, market,
                            plow1.prefix_for('contiguous', data, timeframe))
    assert (body['result_status'], body['error_type'], body['limitations']) == (
        'failed', F12_SHAPE[tool][0], {'reason': F12_SHAPE[tool][1]}), body


# --- byte-identical to 451b879 ------------------------------------------------------------------------

@pytest.mark.parametrize('case', sorted(base.cases()))
def test_untouched_states_byte_identical_to_451b879(case, monkeypatch, tmp_path):
    # off/*: filter off + main gap (5 families); mtf/*: filter_timeframe + main gap; fail/*: other failures.
    assert base.snapshot(monkeypatch, tmp_path, case) == GOLDEN['cases'][case]


def test_golden_covers_required_states():
    keys = set(GOLDEN['cases'])
    assert len([k for k in keys if k.startswith('off/')]) >= 5 and 'mtf/ema_cross' in keys
    assert len([k for k in keys if k.startswith('fail/')]) == 3
    assert json.loads(GOLDEN['cases']['mtf/ema_cross'])['result_status'] == 'success'


# --- arithmetic and boundary --------------------------------------------------------------------------

def test_two_gaps_three_missing_beyond_tolerance_details(monkeypatch, tmp_path):
    warm, data = cap.series()
    window = data.iloc[:29]
    gapped = window.drop(window.index[[5, 15, 16]])  # 26 bars, expected 29: 26 < 26.1
    body = run(monkeypatch, tmp_path, 'inside_bar_breakout', gapped, warm)
    assert body['error_type'] == 'INSUFFICIENT_DATA', body
    assert body['limitations'] == {'reason': 'filter_history_insufficient', 'gap_count': 2, 'missing_bars': 3,
                                   'first_gap_after': iso(window.index[4]), 'first_gap_segment': 'main'}


def test_exactly_at_tolerance_does_not_fail(monkeypatch, tmp_path):
    warm, data = base.gapped(drop=range(100, 136))  # 324 == 360 * 0.9: strictly-less fails only
    assert len(data) == 360 * p.CENTRAL_GAP_TOLERANCE_RATIO
    body = run(monkeypatch, tmp_path, 'ema_cross', data, warm)
    assert body['result_status'] == 'success', body
    assert body['assumptions']['entry_filter_main_gaps']['missing_bars'] == 36


# --- warmup segment keeps "any gap fails", now with details ---------------------------------------------

def test_warmup_inner_gap_segment(monkeypatch, tmp_path):
    full, data = cap.series()
    body = run(monkeypatch, tmp_path, 'ema_cross', data, full.drop(full.index[-3]))
    assert body['limitations'] == {'reason': 'filter_history_insufficient', 'gap_count': 1, 'missing_bars': 1,
                                   'first_gap_after': iso(full.index[-4]), 'first_gap_segment': 'warmup'}, body


def test_warmup_boundary_segment(monkeypatch, tmp_path):
    full, data = cap.series()
    body = run(monkeypatch, tmp_path, 'ema_cross', data, full.iloc[:-1])
    assert body['limitations'] == {'reason': 'filter_history_insufficient', 'gap_count': 1, 'missing_bars': 1,
                                   'first_gap_after': iso(full.index[-2]),
                                   'first_gap_segment': 'warmup_boundary'}, body
