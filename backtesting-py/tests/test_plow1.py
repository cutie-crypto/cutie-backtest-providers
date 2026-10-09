"""P-LOW1 provider low-severity closure.

2. calendar event rejected by sizing ends `skipped/sizing_rejected`; sizing off keeps calendar_events.
3. futures-only templates reject spot with a text that names no template family, before any fetch.
4. US open sizing assumption names the nearest-of-window-and-user-stop rule; other labels unchanged.
5. calendar_stop_enabled=false + risk sizing is rejected before any fetch (same shape as red streak).
"""
import pytest

import _10b2d_cases as c
import cutie_backtesting_provider as p

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
