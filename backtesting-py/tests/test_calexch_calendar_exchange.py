"""CALEXCH: calendar_schedule 与 opening_range_breakout 同形同默认地声明 exchange 键。

服务端给未选 runtime 的回测下发 exchange=binance；calendar 的 schema 原先没有这个键，
provider 只能按缺省交易所取公共 K 线，data_manifest.source 与服务端记录的 run 对不上。
"""
from pathlib import Path

import pandas as pd
import pytest
from backtesting import Backtest
from fastapi.testclient import TestClient

from test_f2_calendar import SCHEDULE, frame
from test_risk_layer import p

TOOL = 'local.backtesting_py.calendar_schedule'
ORB = 'local.backtesting_py.opening_range_breakout'


def run(monkeypatch, tmp_path, params, market, central):
    data = frame()
    data.iloc[1] = [100, 103, 99, 102, 1]
    data.iloc[2] = [104, 105, 100, 103, 1]
    calls = []

    def fake_fetch(exchange_id, fetch_market, symbol, timeframe, *rest):
        calls.append((exchange_id, fetch_market, symbol, timeframe))
        df = data.copy()
        df.attrs['cutie_central_market_data_used'] = central
        return df

    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', Path(tmp_path))
    monkeypatch.setattr(p, '_fetch_ohlcv', fake_fetch)
    monkeypatch.setattr(p, '_fetch_template_warmup', lambda *a: pytest.fail('calendar has no indicator warmup'))
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **k: None)
    request = dict(run_id='calexch', provider_tool_id=TOOL, provider_params=params,
                   symbol='BTCUSDT', market=market, timeframe='1h', start_at=int(data.index[0].timestamp()),
                   end_at=int(data.index[-1].timestamp()) + 3600,
                   initial_capital='10000', fee_bps='0', slippage_bps='0')
    body = TestClient(p.app).post('/cutie/backtest', json={'backtest': request}).json()
    return body, calls


def test_calendar_exchange_schema_matches_orb():
    calendar = p.TOOL_SPECS[TOOL]['param_schema_properties']
    orb = p.TOOL_SPECS[ORB]['param_schema_properties']
    assert calendar['exchange'] == orb['exchange'] == {'type': 'string', 'default': p.DEFAULT_EXCHANGE}


@pytest.mark.parametrize('market,source', [('spot', 'binance_us'), ('futures', 'binance_futures')])
def test_calendar_exchange_binance_reaches_fetch_and_manifest(monkeypatch, tmp_path, market, source):
    body, calls = run(monkeypatch, tmp_path, {**SCHEDULE, 'exchange': 'binance'}, market, central=True)
    assert body['result_status'] == 'success', body
    assert calls and all(call[0] == 'binance' for call in calls), calls
    assert body['data_manifest']['source'] == source
    assert body['data_manifest']['market'] == market


def test_calendar_without_exchange_keeps_default(monkeypatch, tmp_path):
    body, calls = run(monkeypatch, tmp_path, dict(SCHEDULE), 'spot', central=False)
    assert body['result_status'] == 'success', body
    assert calls and all(call[0] == p.DEFAULT_EXCHANGE for call in calls), calls
    assert body['data_manifest']['source'] == f'ccxt:{p.DEFAULT_EXCHANGE}'


def test_calendar_exchange_still_type_checked(monkeypatch, tmp_path):
    body, calls = run(monkeypatch, tmp_path, {**SCHEDULE, 'exchange': 1}, 'spot', central=True)
    assert body.get('result_status') != 'success'
    assert 'exchange must be a string' in str(body)
    assert not calls
