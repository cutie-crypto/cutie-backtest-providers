"""10A: independent hand EMA constants and the frozen pre-change SMA bytes."""
import copy
import hashlib
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from canonical_json import canonical_json  # noqa: E402
from strategy_kernel import (  # noqa: E402
    _condition_hit_v3, _ema_series_v3, build_frames_v3, compile_strategy_v3,
)
from strategy_spec_v3_builder import build_strategy_spec_v3  # noqa: E402
from test_strategy_kernel_basket_golden import (  # noqa: E402
    FIXTURE, _install_fetch, _klines_by_symbol, _post, _request_body,
)
import cutie_backtesting_provider as provider  # noqa: E402

# N=3: seed (7+6+5)/3=6; alpha=1/2. N=7: seed=4; alpha=1/4.
RATIOS = ['7', '6', '5', '4', '3', '2', '1', '10', '1', '1']
FAST = [None, None, '6', '5', '4', '3', '2', '6', '3.5', '2.25']
SLOW = [None, None, None, None, None, None, '4', '5.5', '4.375', '3.53125']
ENTRY = [7]
EXIT = [8]
ENVELOPE = {'timeframe': '4h', 'fee_bps': '0', 'slippage_bps': '0'}


def _params():
    p = copy.deepcopy(FIXTURE['request']['provider_params'])
    p.update(fast_window=3, slow_window=7, ma_type='ema')
    return p


def _rows(ratios):
    return {leg: [dict(open_time=i * 14400, open=x, high=x, low=x, close=x, volume='1')
                  for i, x in enumerate(ratios if leg == 'a' else ['1'] * len(ratios))]
            for leg in ('a', 'b')}


def test_hand_ema_n3_eight_bars():
    got = _ema_series_v3([(Decimal(x), None) for x in RATIOS[:8]], 3)
    assert [None if x is None else str(x.normalize()) for x, _ in got] == FAST[:8]
    assert _ema_series_v3([(Decimal('7'), None)], 3) == [(None, 'insufficient_history')]


def test_compiled_ema_crosses_and_future_causality():
    spec = build_strategy_spec_v3('basket_ratio_sma_cross', _params(), ENVELOPE)
    plan = compile_strategy_v3(spec)
    frames = build_frames_v3(_rows(RATIOS), plan).frames
    assert [f.values['ratio_ema_fast'] for f in frames] == FAST
    assert [f.values['ratio_ema_slow'] for f in frames] == SLOW
    assert [i for i in range(len(frames)) if _condition_hit_v3(spec['entry']['condition'], frames, i, plan.feature_types)] == ENTRY
    assert [i for i in range(len(frames)) if _condition_hit_v3(spec['exit']['signal_exit'], frames, i, plan.feature_types)] == EXIT
    mutated = build_frames_v3(_rows(RATIOS[:8] + ['999', '888']), plan).frames
    assert [f.values for f in mutated[:8]] == [f.values for f in frames[:8]]
    assert provider._basket_warmup_bars(spec) == 7  # prefix excludes the current decision bar


@pytest.mark.parametrize('mode', [None, 'sma'])
def test_sma_prod_golden_bytes_unchanged(monkeypatch, mode):
    _install_fetch(monkeypatch, _klines_by_symbol())
    request = copy.deepcopy(_request_body())
    if mode is not None:
        request['backtest']['provider_params']['ma_type'] = mode
    body = _post(request)
    assert body['result_status'] == 'success', body
    assert body['trades'] == FIXTURE['expected']['trades']
    assert body['strategy_spec_hash'] == FIXTURE['expected']['strategy_spec_v3_hash']
    # Captured from b42210b before editing, using the production golden's K-lines.
    frozen = {k: body[k] for k in ('strategy_spec_json', 'trades', 'equity_curve', 'metrics', 'assumptions')}
    assert hashlib.sha256(canonical_json(frozen).encode()).hexdigest() == '9216904f3a047f8d5617dd543c45bd082967153b01bdd773a90bd082ceceb62e'


@pytest.mark.parametrize('mode', ['EMA', 'wma', '', 1, None, [], {}])
def test_invalid_ma_rejected_before_fetch(monkeypatch, mode):
    def forbidden(*args, **kwargs):
        pytest.fail('invalid params reached data fetch')
    monkeypatch.setattr(provider, '_fetch_ohlcv_raw', forbidden)
    request = copy.deepcopy(_request_body())
    request['backtest']['provider_params']['ma_type'] = mode
    assert _post(request)['error_type'] == 'INVALID_PARAMS'


def test_ema_http_assumptions(monkeypatch):
    _install_fetch(monkeypatch, _klines_by_symbol())
    request = copy.deepcopy(_request_body())
    request['backtest']['provider_params']['ma_type'] = 'ema'
    body = _post(request)
    assert body['result_status'] == 'success', body
    assert body['assumptions']['ema']['adjust'] is False
    assert 'SMA of the first N' in body['assumptions']['ema']['seed']
    assert 'ratio_ema_fast' in body['strategy_spec_json']


def test_ema_gaps_restart_seed():
    source = [(Decimal(x), None) for x in ['7', '6', '5']] + [(None, 'missing_source')]
    source += [(Decimal(x), None) for x in ['4', '3', '2', '1']]
    assert _ema_series_v3(source, 3) == [
        (None, 'insufficient_history'), (None, 'insufficient_history'), (Decimal(6), None),
        (None, 'missing_source'), (None, 'insufficient_history'), (None, 'insufficient_history'),
        (Decimal(3), None), (Decimal(2), None),
    ]


def test_hand_non_dyadic_alpha_ignores_global_precision():
    from decimal import localcontext
    with localcontext() as ctx:
        ctx.prec = 6
        got = _ema_series_v3([(Decimal(x), None) for x in RATIOS[:8]], 5)
    expected = [
        None, None, None, None, Decimal('5'), Decimal('4'), Decimal('3'),
        Decimal('5.333333333333333333333333333333333'),
    ]
    # Ideal hand values; rounded alpha=1/3 introduces at most a few decimal128 ULPs.
    for (actual, _), ideal in zip(got, expected):
        assert actual is None if ideal is None else abs(actual - ideal) <= Decimal('1e-32')
