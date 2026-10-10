"""SHORT-T2-1: fail-closed public leverage and direct-engine sizing proof."""
from __future__ import annotations

import sys
import json
from pathlib import Path

import pandas as pd
import pytest
from backtesting import Backtest, Strategy
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as provider
from test_risk_overlay_compatibility import enumerate_mixin_cases, frame, required_params


MIXINS = [name for name in enumerate_mixin_cases() if not name.endswith('_short')]
LEDGERS = ['rsi_scale_in_out', 'grid', 'dca']


def request(params, market='spot', name='ema_cross'):
    return dict(backtest=dict(run_id='leverage_params',
        provider_tool_id='local.backtesting_py.' + name, provider_params=params,
        symbol='BTCUSDT', market=market, timeframe='1h', start_at=1767225600,
        end_at=1768737600, initial_capital='10000', fee_bps='0', slippage_bps='0'))


@pytest.mark.parametrize('name', MIXINS)
def test_schema_only_runtime_single_position_mixins(name):
    props = provider.TOOL_SPECS['local.backtesting_py.' + name]['param_schema_properties']
    assert props['leverage'] == dict(type='integer', default=1, minimum=1, maximum=20)
    for value in (1, 20):
        cls = provider.TOOL_SPECS['local.backtesting_py.' + name]['build']({**required_params(name), 'leverage': value})['strategy']
        assert cls._risk.get('leverage', 1) == value
    assert provider._parse_single_leverage({}) == 1
    assert provider._parse_fixed_risk_params({'leverage': 1}) == {}


@pytest.mark.parametrize('value', [0, 21, True, 2.0, '2', float('nan'), float('inf'), None, -1])
@pytest.mark.parametrize('name', MIXINS)
def test_invalid_leverage_rejected_before_fetch(monkeypatch, name, value):
    def no_fetch(*args, **kwargs):
        pytest.fail('invalid leverage reached market data')
    monkeypatch.setattr(provider, '_fetch_ohlcv', no_fetch)
    body = TestClient(provider.app).post('/cutie/backtest',
        content=json.dumps(request({**required_params(name), 'leverage': value}, name=name)),
        headers={'Content-Type': 'application/json'}).json()
    assert body['error_type'] == 'INVALID_PARAMS', body
    with pytest.raises(ValueError, match='INVALID_PARAMS:leverage'):
        provider.TOOL_SPECS['local.backtesting_py.' + name]['build']({**required_params(name), 'leverage': value})


@pytest.mark.parametrize('name', LEDGERS)
@pytest.mark.parametrize('value', [1, 20, 0, True, 2.0, '2', None])
def test_ledgers_reject_any_leverage(monkeypatch, name, value):
    spec = provider.TOOL_SPECS['local.backtesting_py.' + name]
    assert 'leverage' not in spec['param_schema_properties']
    monkeypatch.setattr(provider, '_fetch_ohlcv', lambda *a, **k: pytest.fail('ledger fetched data'))
    body = TestClient(provider.app).post('/cutie/backtest', json=request({'leverage': value}, name=name)).json()
    assert body['error_type'] == 'INVALID_PARAMS', body
    assert body['error_message'] == 'scale-in/out template does not support leverage'


def test_basket_leverage_schema_unchanged():
    baskets = [spec for spec in provider.TOOL_SPECS.values() if spec.get('runner') == 'kernel_v3']
    assert baskets
    for spec in baskets:
        assert spec['param_schema_properties']['leverage'] == dict(type='integer', minimum=1, maximum=3)
    declared = {name.removeprefix('local.backtesting_py.') for name, spec in provider.TOOL_SPECS.items()
                if 'leverage' in spec['param_schema_properties'] and spec.get('runner') != 'kernel_v3'}
    assert declared == set(MIXINS)


@pytest.mark.parametrize('market,message', [
    ('spot', 'leverage above 1 requires futures market'),
    ('futures', None),
])
@pytest.mark.parametrize('leverage', [2, 20])
def test_market_gate_before_fetch(monkeypatch, market, message, leverage):
    if market == 'futures':
        calls = []
        def empty_fetch(*args, **kwargs):
            calls.append(True)
            return pd.DataFrame()
        monkeypatch.setattr(provider, '_fetch_ohlcv', empty_fetch)
        body = TestClient(provider.app).post('/cutie/backtest', json=request({'leverage': leverage}, market)).json()
        assert calls == [True]
        assert body['error_type'] == 'INSUFFICIENT_DATA', body
        assert provider._single_leverage_rejection(leverage, market) is None
        return
    monkeypatch.setattr(provider, '_fetch_ohlcv', lambda *a, **k: pytest.fail('rejected leverage fetched data'))
    body = TestClient(provider.app).post('/cutie/backtest', json=request({'leverage': leverage}, market)).json()
    assert body['error_type'] == 'INVALID_PARAMS', body
    assert body['error_message'] == message


@pytest.mark.parametrize('params', [{}, {'leverage': 1}])
def test_spot_one_runs_without_margin_kwarg(monkeypatch, tmp_path, params):
    calls = []
    def record(*args, **kwargs):
        calls.append(kwargs.copy())
        return Backtest(*args, **kwargs)
    monkeypatch.setattr('backtesting.Backtest', record)
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **k: None)
    monkeypatch.setattr(provider, '_fetch_ohlcv', lambda *a, **k: frame())
    monkeypatch.setattr(provider, '_fetch_template_warmup', lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(provider, 'REPORTS_DIR', tmp_path)
    body = TestClient(provider.app).post('/cutie/backtest', json=request(params)).json()
    assert body['result_status'] == 'success', body
    assert len(calls) == 1 and 'margin' not in calls[0]
    assert not ({'leverage', 'isolated_risk', 'liquidation_gap'} & body['raw_report'].keys())


def sized_trade(params, side='long'):
    risk = provider._parse_fixed_risk_params(params)
    class SingleEntry(provider._FixedRiskMixin, Strategy):
        _risk = risk
        _initial_capital = 10000.0
        def init(self):
            self._risk_init()
        def next(self):
            if len(self.data) == 2:
                self._risk_buy() if side == 'long' else self._risk_sell()
    data = pd.DataFrame(dict(Open=[100.] * 6, High=[101.] * 6, Low=[99.] * 6,
                             Close=[100.] * 6, Volume=[1.] * 6),
                        index=pd.date_range('2026-01-01', periods=6, freq='h'))
    stats = Backtest(data, SingleEntry, cash=100000, commission=0, finalize_trades=True,
                     **provider._leverage_backtest_kwargs(provider._parse_single_leverage(params))).run()
    assert len(stats['_trades']) == 1
    return stats['_trades'].iloc[0], stats['_strategy']


@pytest.mark.parametrize('side', ['long', 'short'])
def test_pct_margin_budget_scales_quantity_once(side):
    one, _ = sized_trade(dict(leverage=1, position_size_pct=20), side)
    five, _ = sized_trade(dict(leverage=5, position_size_pct=20), side)
    assert abs(one.Size) == 200
    assert abs(five.Size) == 1000
    assert five.Size / one.Size == 5


@pytest.mark.parametrize('side', ['long', 'short'])
def test_notional_stays_fixed_across_leverage(side):
    one, _ = sized_trade(dict(leverage=1, position_size_notional=500), side)
    five, _ = sized_trade(dict(leverage=5, position_size_notional=500), side)
    # Engine capital is scaled 10x; fold back to the user's $10,000 account.
    assert abs(one.Size * one.EntryPrice) / 10 == 500
    assert abs(five.Size * five.EntryPrice) / 10 == 500


@pytest.mark.parametrize('side', ['long', 'short'])
def test_unconfigured_size_keeps_library_sentinel(side):
    absent, absent_strategy = sized_trade({}, side)
    one, one_strategy = sized_trade({'leverage': 1}, side)
    assert float(absent.Size).hex() == float(one.Size).hex()
    assert absent_strategy._risk_entry_size() is None
    assert one_strategy._risk_entry_size() is None
