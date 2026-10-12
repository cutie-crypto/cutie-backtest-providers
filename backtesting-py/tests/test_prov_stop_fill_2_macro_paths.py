"""PSF-2 sibling schedules: H1 intrinsic overlap and H3 native delayed entry."""
import test_q18_macro_events as macro
from strategy_event_window import skip_native_exit_events


def test_breakout_retains_intrinsic_overlap(monkeypatch, tmp_path):
    data = macro.data_for(macro.H1)
    data.iloc[15] = [103, 104, 98, 103, 1]
    values = macro.params(macro.H1, events=[macro.event(12), macro.event(16)],
                          direction='long', max_hold_minutes=120)
    body = macro.post(monkeypatch, tmp_path, macro.H1, values, data)
    records = body['raw_report'][macro.H1]['events']
    assert records[1]['status'] == 'skipped_in_position'
    # H1 has no generic percent SL/TP; its intrinsic range exit keeps the legacy path.
    assert records[1]['reason'] == 'previous_event_still_open'
    assert [(t['opened_at'] - macro.T0, t['entry_price']) for t in body['trades']] == [(13 * 300, '103')]


def test_fomc_delayed_entry_on_native_stop_bar(monkeypatch, tmp_path):
    data = macro.frame()
    data[['Open', 'High', 'Low', 'Close']] = [100., 100.5, 99.5, 100.]
    data.iloc[11:13] = [100, 101, 94, 95, 1]
    data.iloc[14] = [100, 100.5, 96, 100, 1]
    data.iloc[59] = [100, 101, 94, 95, 1]
    values = macro.params(macro.H3, events=[macro.event(i) for i in (12, 13, 60)],
                          entry_delay_minutes=10, stop_loss_pct=3)
    body = macro.post(monkeypatch, tmp_path, macro.H3, values, data)
    records = body['raw_report'][macro.H3]['events']
    assert records[1]['status'] == 'skipped_in_position'
    assert records[1]['reason'] == 'native_exit_no_reentry'
    assert [(t['opened_at'] - macro.T0, t['entry_price']) for t in body['trades']] == [
        (14 * 300, '100'), (62 * 300, '100')]


def test_shared_helper_preserves_consumed_events():
    records = [dict(status=status) for status in ('entered', 'submitted', 'skipped_null')]
    pending = {}
    skip_native_exit_events([*records, pending, pending], 'native_exit_no_reentry')
    assert records == [dict(status=status) for status in ('entered', 'submitted', 'skipped_null')]
    assert pending == dict(status='skipped_in_position', reason='native_exit_no_reentry')
