"""Turtle seed 4d29dfa capture; --record-psf-p2c replays scalar and HTTP goldens."""
from __future__ import annotations
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
import cutie_backtesting_provider as provider
from backtesting import Backtest
from fastapi.testclient import TestClient

BASELINE_SHA = '4d29dfa91c63b737e22f5be4b3d2d05f105b476d'
FIXTURE = Path(__file__).with_name('turtle_time_off_4d29dfa.json')
# case -> (golden input, market, extra params merged over the golden params)
CASES = {
    'long': ('long', 'spot', {}),
    'short': ('short', 'futures', {}),
    'both': ('both', 'futures', {}),
    'long_risk_layer': ('long', 'futures', {'risk_layer_enabled': True, 'max_holding_bars': 3,
                                            'take_profit_pct': 5}),
}


def golden(direction):
    return json.loads(Path(__file__).with_name(f'turtle_{direction}_golden.json').read_text())


def frame(fx):
    return pd.DataFrame({k: fx[k.lower() + 's'] for k in ('Open', 'High', 'Low', 'Close')},
                        index=pd.date_range('2026-01-01', periods=len(fx['closes']), freq='h')).assign(Volume=1)


def response(case, extra=None):
    direction, market, case_extra = CASES[case]
    fx = golden(direction)
    data = frame(fx)
    params = {**fx['params'], **case_extra, **(extra or {})}
    body = {'backtest': dict(run_id='turtle_time_off', provider_tool_id='local.backtesting_py.turtle',
        provider_params=params, symbol='BTCUSDT', market=market, timeframe='1h',
        start_at=int(data.index[0].timestamp()), end_at=int(data.index[-1].timestamp()) + 3600,
        initial_capital='100000', fee_bps='10', slippage_bps='5')}
    with tempfile.TemporaryDirectory(prefix='turtle-time-') as reports, \
         patch.object(provider, 'AUTH_TOKEN', ''), \
         patch.object(provider, 'REPORTS_DIR', Path(reports)), \
         patch.object(provider, '_fetch_ohlcv', lambda *a: data.copy()), \
         patch.object(provider, '_fetch_template_warmup', lambda *a: data.iloc[:0].copy()), \
         patch.object(Backtest, 'plot', return_value=None):
        res = TestClient(provider.app).post('/cutie/backtest', json=body)
        assert res.status_code == 200, res.text
        return res.text


def record_psf_p2c():
    """Explicitly rebaseline turtle only, using the existing independent accounts."""
    import hashlib
    import re
    from test_turtle_template import hand, directional_hand
    from _exit_kinds_strip import without_exit_kinds_text
    for direction in ('long', 'short', 'both'):
        path = FIXTURE.with_name(f'turtle_{direction}_golden.json')
        fx = json.loads(path.read_text())
        fx['expected'] = hand(fx) if direction == 'long' else directional_hand(fx)
        path.write_text(json.dumps(fx, indent=2) + '\n')
    captured = {}
    for case in CASES:
        text = response(case)
        assert text == response(case), f'{case} response is not deterministic'
        assert json.loads(text)['result_status'] == 'success'
        captured[case] = re.sub(r',"n":"[^\"]*","stop":"[^\"]*"', '', without_exit_kinds_text(text))
    # baseline_sha identifies the original market seed, not the native response.
    payload = dict(baseline_sha=BASELINE_SHA, responses=captured,
                   protective_fill_contract='PSF-P2c: same-bar trigger price; gaps at open',
                   capture_source_sha256=hashlib.sha256(Path(provider.__file__).read_bytes()).hexdigest())
    FIXTURE.write_text(json.dumps(payload, indent=2) + '\n')
    print('RECALCULATED turtle_scalar=3 turtle_http=4')


if __name__ == '__main__':
    if sys.argv[1:] == ['--record-psf-p2c']:
        record_psf_p2c()
        raise SystemExit(0)
    sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    assert sha == BASELINE_SHA, sha
    captured = {}
    for case in CASES:
        text = response(case)
        body = json.loads(text)
        assert body['result_status'] == 'success' and body['trades'], body
        assert text == response(case), f'{case} response is not deterministic'
        captured[case] = text
    FIXTURE.write_text(json.dumps(dict(baseline_sha=sha, responses=captured), indent=2) + '\n')
    print(f'CAPTURED turtle_off={len(captured)}')
