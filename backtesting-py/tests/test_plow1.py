"""P-LOW1 provider low-severity closure.

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
