"""P-EVENT0 golden extension: partial take-profit scenarios (one entered event, several result.v2 rows).

Already pinned trade-by-trade elsewhere (not repeated here), all in test_event0_event_window.py:
off-grid ts -> containing bar (golden E3), bars_before > 0 (golden bars_before=1, boundary 48),
overlap -> skipped_in_position (golden E2), out of range / before first decision
(test_out_of_range_events_are_skipped), futures short (test_short_mirror), time layer ->
blocked_by_time_or_filter (test_time_layer_closed_gate_is_recorded), stop_loss_pct first
(test_stop_exit_precedes_window_end).

Frames are the hand frames of test_event0_event_window.py: frame() bar i = O 100+i H 101+i L 99+i
C 100.5+i; falling() bar i = O 73-i H 74-i L 72-i C 73.5-i. Every price below is hand arithmetic.
Capture: python3 tests/test_event0_window_scenarios.py (runs twice, asserts identical bytes, prints sha256).
"""
import hashlib
import json
from pathlib import Path

import pytest

from test_event0_event_window import falling, frame, legs, one, post, events_of

GOLDEN = Path(__file__).parent / 'fixtures' / 'event0_window_scenarios_golden.json'
GOLDEN_SHA256 = '404cd9350b01e5554a0d32e4447c1a0db49b8299cdb6d8866b29c39879c372df'
LADDER = dict(risk_layer_enabled=True, stop_loss_pct=2, tp1_r=0.5, tp1_close_pct=50, tp2_r=10, tp2_close_pct=50)
SCENARIOS = {
    # Long: entry bar 4 open 104, stop 101.92, R 2.08; tp1 105.04 touched by bar 5 high 106 -> half
    # at bar 6 open 106; tp2 124.8 never reached -> window (held 4,5,6) closes the rest at bar 7 open 107.
    'partial_tp_long': (dict(**one('2026-01-01T05:00:00Z', bars_before=1, bars_after=3), **LADDER), 'frame'),
    # Futures short: entry bar 4 open 69, stop 70.38, R 1.38; tp1 68.31 touched by the entry bar's own low 68
    # -> half at bar 5 open 68; tp2 55.2 never reached -> window closes the rest at bar 7 open 66.
    'partial_tp_short_futures': (dict(**one('2026-01-01T05:00:00Z', bars_before=1, bars_after=3), **LADDER,
                                      direction='short'), 'falling'),
}
EXPECTED = {
    'partial_tp_long': ([(4, '104', 6, '106', 'long'), (4, '104', 7, '107', 'long')],
                        [('2026-01-01T06:00:00+00:00', 106.0, 'take_profit_levels'),
                         ('2026-01-01T07:00:00+00:00', 107.0, 'window_end')]),
    'partial_tp_short_futures': ([(4, '69', 5, '68', 'short'), (4, '69', 7, '66', 'short')],
                                 [('2026-01-01T05:00:00+00:00', 68.0, 'take_profit_levels'),
                                  ('2026-01-01T07:00:00+00:00', 66.0, 'window_end')]),
}


def run(monkeypatch, tmp_path, name):
    params, data = SCENARIOS[name]
    body = post(monkeypatch, tmp_path, params, data=frame() if data == 'frame' else falling(), market='futures')
    assert body['result_status'] == 'success', body.get('error_message')
    return body


def capture(monkeypatch, tmp_path):
    out = {}
    for name in sorted(SCENARIOS):
        body = dict(run(monkeypatch, tmp_path, name))
        body.pop('report_path', None)
        out[name] = body
    return json.dumps(out, sort_keys=True, separators=(',', ':'), ensure_ascii=False) + '\n'


@pytest.mark.parametrize('name', sorted(SCENARIOS))
def test_scenario_hand_computed(monkeypatch, tmp_path, name):
    body = run(monkeypatch, tmp_path, name)
    expected_legs, expected_exits = EXPECTED[name]
    assert legs(body) == expected_legs
    (record,) = events_of(body)
    assert (record['status'], record['entry_utc'], record['entry_price']) == (
        'entered', '2026-01-01T04:00:00+00:00', float(expected_legs[0][1]))
    assert [(e['time'], e['price'], e['reason']) for e in record['exits']] == expected_exits
    assert (record['exit_utc'], record['exit_price'], record['exit_reason']) == expected_exits[-1]
    assert len(record['exits']) == len(body['trades'])


def test_golden_bytes_and_sha(monkeypatch, tmp_path):
    text = GOLDEN.read_text()
    assert hashlib.sha256(text.encode()).hexdigest() == GOLDEN_SHA256
    assert capture(monkeypatch, tmp_path) == text


if __name__ == '__main__':
    import tempfile
    mp = pytest.MonkeyPatch()
    with tempfile.TemporaryDirectory() as tmp:
        first, second = capture(mp, Path(tmp)), capture(mp, Path(tmp))
    mp.undo()
    assert first == second, 'two captures differ'
    GOLDEN.write_text(first)
    print('sha256', hashlib.sha256(first.encode()).hexdigest())
