"""Immutable 999664e/c78def6 fingerprints: exact engine floats and canonical result.v2 bytes.

Fixtures were captured BEFORE provider changes. No test regenerates them.
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from backtesting import Backtest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as provider
from canonical_json import canonical_json

PARAMS = {
    'ema_cross': dict(ema_fast=5, ema_slow=20),
    'rsi_reversal': dict(rsi_period=5, oversold=40, overbought=60),
    'bollinger_reversal': dict(bb_period=10, bb_std=1),
    'bollinger_breakout': dict(bb_period=10, bb_std=1),
    'breakout': dict(lookback=5, exit_lookback=3),
    'volume_breakout': dict(lookback=5, volume_avg_period=5, volume_multiple=1, exit_ema=5),
    'macd': dict(fast=5, slow=15, signal=3),
    'cci_rsi': dict(cci_period=5, rsi_period=5, cci_oversold=-50, cci_overbought=50,
                    rsi_oversold=40, rsi_overbought=60),
    'ema_rsi_pullback': dict(ema_period=20, rsi_period=5, rsi_entry=50, rsi_exit=70),
    'roc': dict(roc_period=5, entry_threshold=1, exit_threshold=0),
    'supertrend': dict(atr_period=5, multiplier=1),
    'ema_trend_rsi': dict(ema_fast=5, ema_slow=20, rsi_period=5, rsi_entry_below=60, rsi_exit_above=70),
    'ema_pullback': dict(ema_fast=5, ema_slow=30, pullback_tolerance_pct=1),
}
RISK_CASES = {
    'absent': {},
    'fixed': dict(stop_loss_pct=3, take_profit_pct=5),
    'pct': dict(stop_loss_pct=3, take_profit_pct=5, position_size_pct=20),
    'notional': dict(stop_loss_pct=3, take_profit_pct=5, position_size_notional=500),
}
DISABLED = dict(risk_layer_enabled=False, atr_stop_multiplier=0, risk_atr_period=0, take_profit_r=0)
FIXTURE = Path(__file__).parent / 'fixtures' / 'risk_overlay_999664e.json'
MAIN_FIXTURE = FIXTURE.with_name('risk_overlay_c78def6.json')
PARAMS.update(json.loads(MAIN_FIXTURE.read_text())['params'])


def fixture_cases():
    original = json.loads(FIXTURE.read_text())['cases']
    added = json.loads(MAIN_FIXTURE.read_text())['cases']
    assert original.keys().isdisjoint(added)
    return {**original, **added}


def fixture_names():
    return {key.split('/')[0] for key in fixture_cases()}


def tool_name(name):
    return 'ema_pullback' if name == 'ema_pullback_short' else name


def enumerate_mixin_cases():
    """Discover actual default-built mixins; future tools need no old fingerprint."""
    cases = {}
    for tool_id, spec in provider.TOOL_SPECS.items():
        if spec.get('runner') in ('kernel_v3', 'scale_in_out_ledger'):
            continue
        cls = spec['build']({})['strategy']
        if isinstance(cls, type) and issubclass(cls, provider._FixedRiskMixin):
            name = tool_id.removeprefix('local.backtesting_py.')
            cases[name] = cls
            if name == 'ema_pullback':
                short_cls = spec['build']({'direction': 'short'})['strategy']
                assert issubclass(short_cls, provider._FixedRiskMixin)
                cases[name + '_short'] = short_cls
    return cases


def frame():
    closes = [100 + .03 * i + 13 * math.sin(i / 9) + 2 * math.sin(i / 2) for i in range(420)]
    return pd.DataFrame(dict(Open=[c + .15 * math.cos(i) for i, c in enumerate(closes)],
                             High=[c + 1.5 for c in closes], Low=[c - 1.5 for c in closes],
                             Close=closes, Volume=[100 + (i % 7) * 70 for i in range(420)]),
                        index=pd.date_range('2026-01-01', periods=420, freq='h'))


def fingerprint(name, risk_case, warm, disabled=False):
    data = frame()
    prefix, data = data.iloc[:60], data.iloc[60:].copy()
    params = {**PARAMS[name], **RISK_CASES[risk_case], **(DISABLED if disabled else {})}
    cls = provider.TOOL_SPECS['local.backtesting_py.' + tool_name(name)]['build'](params)['strategy']
    if warm:
        cls._warmup_bars = len(prefix)
        cls._warmup_cols = {c: prefix[c].to_numpy() for c in provider._WARMUP_COLUMNS}
    stats = Backtest(data, cls, cash=100000, commission=.001, exclusive_orders=True,
                     finalize_trades=True).run()
    trades = stats['_trades']

    def encode(value):
        if value is pd.NaT:
            return "NaT"
        if isinstance(value, float):
            return value.hex()  # exact IEEE value, including NaN, without decimal truncation
        if isinstance(value, (pd.Timestamp, pd.Timedelta)):
            return str(value)
        return value

    def digest(value):
        return hashlib.sha256(json.dumps(value, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()

    v2 = provider._build_result_v2(
        stats_trades=trades, equity_scale_dec=Decimal('.1'), fee_bps=Decimal(10),
        slippage_bps=Decimal(5), initial_capital=Decimal(10000),
        start_at=int(data.index[0].timestamp()), end_at=int(data.index[-1].timestamp()) + 3600,
        symbol='BTCUSDT', market='spot', timeframe='1h', exchange_id='binance', df=data)
    return dict(trade_count=len(trades),
                trades_sha256=digest([list(trades.columns), [[encode(v) for v in row]
                                                          for row in trades.itertuples(index=False, name=None)]]),
                equity_sha256=digest([[encode(v) for v in row]
                                      for row in stats['_equity_curve'].itertuples(index=False, name=None)]),
                result_v2_sha256=hashlib.sha256(canonical_json(v2).encode()).hexdigest())


@pytest.mark.parametrize('name', PARAMS)
@pytest.mark.parametrize('risk_case', RISK_CASES)
@pytest.mark.parametrize('warm', [False, True])
@pytest.mark.parametrize('disabled', [False, True])
def test_templates_match_immutable_baselines(name, risk_case, warm, disabled):
    expected = fixture_cases()[f'{name}/{risk_case}/{int(warm)}']
    assert expected['trade_count'] > 0, 'compatibility proof must exercise trades'
    assert fingerprint(name, risk_case, warm, disabled) == expected


def test_compatibility_covers_baseline_templates_among_runtime_mixins():
    actual = set(enumerate_mixin_cases())
    assert fixture_names() <= actual
    assert len(actual) >= 19
    assert set(PARAMS) == fixture_names()
