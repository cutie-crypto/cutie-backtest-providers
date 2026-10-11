"""PIN7-F: the higher-timeframe filter tolerates missing candles without filling them.

Present candles must sit on the grid, unique and increasing; missing ones are tolerated while
present/expected >= CENTRAL_GAP_TOLERANCE_RATIO and no run exceeds _htf_max_gap_bars. A decision whose
latest closed candle is missing reuses the previous closed candle (no look-ahead, no masking).
Gap failures carry _gap_details-shaped details; a gap-free run is byte-identical to fe3f762.
"""
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest
from backtesting import Backtest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p
import test_entry_filters as single
import test_entry_filters_mtf as mtf
from strategy_entry_filters import (FilterConfig, FilterHistoryError, HigherTimeframeContext, _htf_max_gap_bars,
                                    entry_mask)

# raw_report sha256 of the gap-free http_run below, captured on provider main fe3f762.
FE3F762_RAW_REPORT_SHA256 = '4887f087cb8bf89f15852c9f5a77ce5723278e43008fc16b798d002b927305c2'
HTF_SOURCE = [100]*10 + [110, 90, 100, 110, 90, 120, 80, 110, 90]*9  # 4h from 2026-01-01


def http_run(monkeypatch, tmp_path, drop=(), shift=None):
    # Same shape as test_entry_filters_mtf.http_run: 4h expected grid is source bars 8..41 (34 bars).
    data = single.frame([100]*10 + [90, 110, 120, 80, 70, 130, 90, 100]*64)
    data.index = pd.date_range('2026-01-02 16:00', periods=len(data), freq='15min')
    source = mtf.coarse_frame(HTF_SOURCE)
    if shift is not None:
        index = source.index.tolist()
        index[shift] = index[shift] + pd.Timedelta(milliseconds=1)
        source.index = pd.DatetimeIndex(index)
    source = source.drop(source.index[list(drop)])
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda e, m, s, tf, since, until: (source if tf == '4h' else data).copy())
    monkeypatch.setattr(p, '_fetch_template_warmup', lambda *args: single.frame([100]*10))
    monkeypatch.setattr(Backtest, 'plot', lambda *args, **kwargs: None)
    values = {**single.params('ema'), 'ema_fast': 2, 'ema_slow': 3, 'filter_timeframe': '4h', 'position_size_pct': 10}
    if 'direction' in p.TOOL_SPECS['local.backtesting_py.ema_cross']['param_schema_properties']:
        values['direction'] = 'long'
    body = single.capture.capture.request_body('ema_cross', values)
    start = int(data.index[0].timestamp())
    body['backtest'].update(timeframe='15m', start_at=start, end_at=start + len(data)*900)
    return TestClient(p.app).post('/cutie/backtest', json=body).json()


def assert_gap_failure(result, missing_bars, gap_count, first_gap_after):
    assert result['error_type'] == 'INSUFFICIENT_DATA', result
    limitations = result['limitations']
    assert limitations['reason'] == 'filter_history_insufficient'
    assert {k: limitations[k] for k in ('gap_count', 'missing_bars', 'first_gap_after', 'first_gap_segment')} == {
        'gap_count': gap_count, 'missing_bars': missing_bars, 'first_gap_after': first_gap_after,
        'first_gap_segment': 'higher_timeframe'}


def build(source, opens, timeframe='4h', ratio=p.CENTRAL_GAP_TOLERANCE_RATIO):
    config = FilterConfig.parse({**single.params('ema'), 'filter_timeframe': timeframe})
    return HigherTimeframeContext.build(config, '1h', opens, lambda *args: source.copy(), p._supertrend_arrays,
                                        p._timeframe_grid_offset_ms(timeframe), gap_tolerance_ratio=ratio)


# 1 ------------------------------------------------------------------------------------------------
def test_two_missing_4h_candles_succeed_and_record_assumptions(monkeypatch, tmp_path):
    result = http_run(monkeypatch, tmp_path, drop=(12, 13))
    assert result['result_status'] == 'success', result
    assert result['assumptions']['entry_filter_htf_gaps'] == {
        'tolerance_ratio': 0.9, 'missing_bars': 2,
        'segments': [{'after': '2026-01-02T20:00:00+00:00', 'before': '2026-01-03T08:00:00+00:00',
                      'missing_bars': 2}],
        'max_gap_bars': 24, 'timeframe': '4h'}
    assert 'entry_filter_htf_gaps' not in result['raw_report']['entry_filters']


def test_decision_after_gap_uses_last_closed_candle_before_gap():
    # Bar 10 (16:00-20:00) closes 110 => EMA2 bullish; bars 11 (90) and 12 (95) are missing; bar 13 (80) bearish.
    full = mtf.coarse_frame([100]*10 + [110, 90, 95, 80] + [100]*26)
    present = full.drop(full.index[[11, 12]])
    opens = pd.date_range('2026-01-02 08:00', periods=96, freq='h')
    gapped, strict = build(present, opens), build(full, opens)
    coarse = entry_mask(FilterConfig.parse({**single.params('ema'), 'filter_timeframe': '4h'}), present,
                        p._supertrend_arrays)
    bar10 = coarse[present.index.get_loc(pd.Timestamp('2026-01-02 16:00'))]
    # opens[15] decides at Jan 3 00:00 (latest closed = missing bar 11); opens[19] at 04:00 (missing bar 12).
    assert bar10 and gapped.mask[15] == gapped.mask[19] == bar10
    assert not strict.mask[15]
    assert not gapped.mask[23]  # 08:00 decision: bar 13 closed, bearish.
    assert gapped.gaps['missing_bars'] == 2 and strict.gaps is None


# 2 ------------------------------------------------------------------------------------------------
def test_all_4h_candles_missing_fails_with_details(monkeypatch, tmp_path):
    result = http_run(monkeypatch, tmp_path, drop=range(len(HTF_SOURCE)))
    assert_gap_failure(result, missing_bars=34, gap_count=1, first_gap_after=None)


# 3 / 6 ------------------------------------------------------------------------------------------
def test_coverage_below_tolerance_fails_with_details(monkeypatch, tmp_path):
    result = http_run(monkeypatch, tmp_path, drop=(12, 16, 20, 24))  # 30 / 34 < 0.9
    assert_gap_failure(result, missing_bars=4, gap_count=4, first_gap_after='2026-01-02T20:00:00+00:00')


def test_warmup_failure_payload_has_no_details(monkeypatch, tmp_path):
    result = http_run(monkeypatch, tmp_path, drop=(8,))  # first expected candle missing => :324 warmup
    assert result['error_type'] == 'INSUFFICIENT_DATA', result
    assert result['error_message'] == 'Higher-timeframe indicator warmup is insufficient'
    assert set(result['limitations']) & {'gap_count', 'missing_bars', 'first_gap_after', 'first_gap_segment'} == set()


# 4 ------------------------------------------------------------------------------------------------
def test_misaligned_candle_still_fails_without_details(monkeypatch, tmp_path):
    result = http_run(monkeypatch, tmp_path, shift=20)
    assert result['error_type'] == 'INSUFFICIENT_DATA', result
    assert result['error_message'] == 'Higher-timeframe history has missing or misaligned candles'
    assert 'missing_bars' not in result['limitations'] and 'gap_count' not in result['limitations']


def test_duplicate_candle_still_fails_without_details():
    source = mtf.coarse_frame([100]*40)
    source = pd.concat([source.iloc[:20], source.iloc[19:]])
    with pytest.raises(FilterHistoryError) as caught:
        build(source, pd.date_range('2026-01-02 08:00', periods=96, freq='h'))
    assert caught.value.details is None


# 5 ------------------------------------------------------------------------------------------------
def test_gap_free_run_is_byte_identical_to_fe3f762(monkeypatch, tmp_path):
    result = http_run(monkeypatch, tmp_path)
    assert result['result_status'] == 'success', result
    assert 'entry_filter_htf_gaps' not in result['assumptions']
    digest = hashlib.sha256(json.dumps(result['raw_report'], sort_keys=True, separators=(',', ':')).encode())
    assert digest.hexdigest() == FE3F762_RAW_REPORT_SHA256


# 7 ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize('timeframe,bars', [('1h', 24), ('4h', 24), ('12h', 14), ('1d', 7), ('1w', 7)])
def test_htf_max_gap_bars(timeframe, bars):
    assert _htf_max_gap_bars(p._timeframe_milliseconds(timeframe)) == bars


@pytest.mark.parametrize('run,ok', [(24, True), (25, False)])
def test_single_run_beyond_max_gap_bars_fails_with_details(run, ok):
    full = mtf.coarse_frame([100, 110]*160)  # 320 4h candles; 1h decisions cover ~300 of them
    opens = pd.date_range('2026-01-02 08:00', periods=1200, freq='h')
    present = full.drop(full.index[100:100 + run])
    if ok:
        assert build(present, opens).gaps['segments'][0]['missing_bars'] == run
        return
    with pytest.raises(FilterHistoryError, match='missing or misaligned') as caught:
        build(present, opens)
    assert caught.value.details == {'gap_count': 1, 'missing_bars': run, 'first_gap_segment': 'higher_timeframe',
                                    'first_gap_after': full.index[99].tz_localize('UTC').isoformat()}
