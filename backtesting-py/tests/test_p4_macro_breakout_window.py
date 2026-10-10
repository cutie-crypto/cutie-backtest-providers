"""P4: macro_release_breakout breakout_window_bars + data_manifest regression pin for the three macro templates.

Frame: the Q18 5-minute candles (flat 100, High 101, Low 99). The H1 event sits on the grid at bar 12, so the
event candle is bar 12 and the pre-release range is [99, 101]. Candles after the event candle are counted
1, 2, ..., so with window N the last valid breakout close is bar 12 + N and the entry fills at the open of
bar 12 + N + 1. A breakout that first closes at bar 12 + N + 1 is too late: the event expires.
"""
from pathlib import Path
import sys

import pandas as pd
import pytest
from backtesting import Backtest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p
from test_q18_macro_events import H1, H2, H3, KINDS, STEP, T0, data_for, event, frame, legs, params, post, report

EVENT_BAR = 12


def breakout_data(close_bar, n=96):
    """Flat candles, then every close from `close_bar` on is 102 (above the pre-release high 101)."""
    data = frame(n)
    data.iloc[close_bar:] = [102, 103, 101.5, 102, 1]
    return data


def run(monkeypatch, tmp_path, close_bar, **extra):
    return post(monkeypatch, tmp_path, H1, params(H1, **extra), breakout_data(close_bar))


def event_record(body):
    (record,) = report(body, H1)['events']
    return record


@pytest.mark.parametrize('window', [1, 3, 12])
def test_breakout_closing_on_the_last_window_candle_enters(monkeypatch, tmp_path, window):
    body = run(monkeypatch, tmp_path, EVENT_BAR + window, breakout_window_bars=window)
    assert [leg[0] for leg in legs(body)] == [EVENT_BAR + window + 1]
    assert legs(body)[0][4] == 'long'
    assert event_record(body)['status'] == 'entered'


@pytest.mark.parametrize('window,close_bar', [(3, EVENT_BAR), (3, EVENT_BAR + 1), (3, EVENT_BAR + 2), (1, EVENT_BAR)])
def test_breakout_earlier_inside_the_window_enters(monkeypatch, tmp_path, window, close_bar):
    body = run(monkeypatch, tmp_path, close_bar, breakout_window_bars=window)
    assert [leg[0] for leg in legs(body)] == [close_bar + 1]


@pytest.mark.parametrize('window', [1, 3, 12])
def test_breakout_one_candle_after_the_window_expires(monkeypatch, tmp_path, window):
    body = run(monkeypatch, tmp_path, EVENT_BAR + window + 1, breakout_window_bars=window)
    assert not body['trades']
    record = event_record(body)
    assert (record['status'], record['reason']) == ('skipped', 'breakout_window_expired')
    assert report(body, H1)['counts'] == {'skipped': 1}
    assert report(body, H1)['skipped_events'] == [dict(ts_utc=record['ts_utc'], reason='breakout_window_expired')]


def test_default_window_is_12_candles(monkeypatch, tmp_path):
    spec = p.TOOL_SPECS[f'local.backtesting_py.{H1}']['param_schema_properties']['breakout_window_bars']
    assert (spec['type'], spec['default'], spec['minimum'], spec['maximum']) == ('integer', 12, 1, 96)
    inside = run(monkeypatch, tmp_path, EVENT_BAR + 12)
    assert [leg[0] for leg in legs(inside)] == [EVENT_BAR + 13]
    assert inside['assumptions'][H1]['breakout_window_bars'] == 12
    late = run(monkeypatch, tmp_path, EVENT_BAR + 13)
    assert not late['trades'] and event_record(late)['reason'] == 'breakout_window_expired'


def test_window_and_max_hold_are_independent(monkeypatch, tmp_path):
    body = run(monkeypatch, tmp_path, EVENT_BAR + 2, breakout_window_bars=2, max_hold_minutes=30)
    (leg,) = legs(body)
    # entry bar 15, held 30 minutes = 6 candles, exit on the following open: neither value feeds the other.
    assert (leg[0], leg[2]) == (EVENT_BAR + 3, EVENT_BAR + 3 + 6)


def test_data_ending_inside_the_window_keeps_the_data_end_reason(monkeypatch, tmp_path):
    body = post(monkeypatch, tmp_path, H1, params(H1, breakout_window_bars=96), frame())
    assert not body['trades'] and event_record(body)['reason'] == 'no_breakout_before_data_end'


def test_breakout_34_days_after_the_event_is_expired(monkeypatch, tmp_path):
    # The production replay: nothing broke out after the release, and the entry came about 34 days later.
    bars = 34 * 288 + 6
    data = frame(bars)
    data.iloc[34 * 288:] = [102, 103, 101.5, 102, 1]
    body = post(monkeypatch, tmp_path, H1, params(H1), data)
    assert not body['trades']
    assert event_record(body)['reason'] == 'breakout_window_expired'


@pytest.mark.parametrize('value', [0, 97, -1, 1.5, True, '12'])
def test_window_out_of_range_or_wrong_type_rejected(monkeypatch, tmp_path, value):
    body = post(monkeypatch, tmp_path, H1, params(H1, breakout_window_bars=value), invalid=True)
    assert body['error_type'] == 'INVALID_PARAMS' and 'breakout_window_bars' in body['error_message']


@pytest.mark.parametrize('value', [1, 96])
def test_window_bounds_are_accepted(monkeypatch, tmp_path, value):
    post(monkeypatch, tmp_path, H1, params(H1, breakout_window_bars=value), breakout_data(EVENT_BAR + 1))


@pytest.mark.parametrize('kind', [H2, H3])
def test_other_macro_templates_do_not_take_the_window(monkeypatch, tmp_path, kind):
    assert 'breakout_window_bars' not in p.TOOL_SPECS[f'local.backtesting_py.{kind}']['param_schema_properties']
    body = post(monkeypatch, tmp_path, kind, params(kind, breakout_window_bars=3), invalid=True)
    assert body['error_type'] == 'INVALID_PARAMS' and 'breakout_window_bars' in body['error_message']
    assert 'breakout_window' not in p.TOOL_SPECS[f'local.backtesting_py.{kind}']['description']


def reference_manifest(monkeypatch, tmp_path, data):
    """data_manifest of an existing, non-macro template (event_window) over the same candles."""
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a, **k: data.copy())
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **k: None)
    request = dict(run_id='p4ref', provider_tool_id='local.backtesting_py.event_window',
                   provider_params=dict(events=[event(EVENT_BAR)], bars_before=0, bars_after=3),
                   symbol='BTCUSDT', market='futures', timeframe='5m', start_at=T0,
                   end_at=int(data.index[-1].timestamp()) + STEP, initial_capital='10000', fee_bps='0', slippage_bps='0')
    body = TestClient(p.app).post('/cutie/backtest', json={'backtest': request}).json()
    assert body['result_status'] == 'success', body
    return body['data_manifest']


def drop_checksum(manifest):  # test-side mutation hook; the mutation run swaps this for the real removal
    return manifest


@pytest.mark.parametrize('kind', KINDS)
def test_macro_data_manifest_matches_an_existing_template(monkeypatch, tmp_path, kind):
    data = data_for(kind)
    manifest = drop_checksum(post(monkeypatch, tmp_path, kind, data=data)['data_manifest'])
    reference = reference_manifest(monkeypatch, tmp_path, data)
    assert list(manifest) == list(reference)
    assert set(manifest) == {'source', 'symbol', 'market', 'timeframe', 'start_at', 'end_at', 'kline_count',
                             'checksum_algo', 'checksum'}
    assert manifest == reference
    # The evaluation window is the 96 fetched candles; the checksum is the provider's canonical-rows sha256.
    assert manifest['kline_count'] == len(data) == 96
    assert manifest['checksum_algo'] == 'sha256'
    rows = p._kline_rows_to_canonical(data, manifest['start_at'], manifest['end_at'])
    assert manifest['checksum'] == p.canonical_json_sha256(rows)
    assert len(manifest['checksum']) == 64
