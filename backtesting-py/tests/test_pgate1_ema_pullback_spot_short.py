"""P-GATE1: ema_pullback short/both on spot is rejected before fetch; futures short and spot long still pass the gate."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import cutie_backtesting_provider as provider

TOOL = 'local.backtesting_py.ema_pullback'


class _Fetched(Exception):
    pass


def _post(monkeypatch, market, params):
    calls = []

    def fetch(*a, **kw):
        calls.append(1)
        raise _Fetched()
    monkeypatch.setattr(provider, '_fetch_ohlcv', fetch)
    body = {'backtest': dict(run_id='pgate1', provider_tool_id=TOOL, provider_params=params,
                             symbol='BTCUSDT', market=market, timeframe='1h',
                             start_at=1_750_000_000, end_at=1_750_000_000 + 30 * 86400,
                             initial_capital='10000', fee_bps='0', slippage_bps='0')}
    resp = TestClient(provider.app, raise_server_exceptions=False).post('/cutie/backtest', json=body)
    return calls, resp


def test_spot_short_rejected_before_fetch(monkeypatch):
    calls, resp = _post(monkeypatch, 'spot', {'direction': 'short'})
    result = resp.json()
    assert calls == []
    assert result['error_type'] == 'INVALID_PARAMS'
    assert result['error_message'] == 'short/both direction requires futures market'


def test_spot_both_still_rejected_by_schema_enum_before_fetch(monkeypatch):
    calls, resp = _post(monkeypatch, 'spot', {'direction': 'both'})
    result = resp.json()
    assert calls == []
    assert result['error_type'] == 'INVALID_PARAMS'
    assert result['error_message'] == "direction must be one of ['long', 'short']"


@pytest.mark.parametrize('market,params', [('futures', {'direction': 'short'}), ('spot', {'direction': 'long'}), ('spot', {})])
def test_futures_short_and_spot_long_pass_gate_to_fetch(monkeypatch, market, params):
    calls, _ = _post(monkeypatch, market, params)
    assert calls == [1]
