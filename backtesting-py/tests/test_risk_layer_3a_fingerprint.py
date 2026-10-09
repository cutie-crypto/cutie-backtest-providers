"""Frozen enabled-3a compatibility proof, captured at 11e8cfb before 3b edits."""
import json
from pathlib import Path
import pytest
import test_risk_overlay_compatibility as compat
from strategy_risk_overlay import risk_assumptions

CASES = {
    'atr': ('ema_cross', dict(risk_layer_enabled=True, atr_stop_multiplier=2, risk_atr_period=14)),
    'atr_r': ('ema_cross', dict(risk_layer_enabled=True, atr_stop_multiplier=2, risk_atr_period=14, take_profit_r=2)),
    'fixed_touch': ('ema_cross', dict(risk_layer_enabled=True, stop_loss_pct=3, take_profit_pct=5)),
    'short_atr': ('ema_pullback_short', dict(risk_layer_enabled=True, atr_stop_multiplier=2, risk_atr_period=14)),
}
FIXTURE = Path(__file__).parent / 'fixtures/risk_layer_3a_11e8cfb.json'

def fingerprint(case, warm):
    name, params = CASES[case]
    old = compat.RISK_CASES.copy()
    try:
        compat.RISK_CASES['_3a'] = params
        result = compat.fingerprint(name, '_3a', warm)
        result['assumptions'] = risk_assumptions(params)
        return result
    finally:
        compat.RISK_CASES.clear()
        compat.RISK_CASES.update(old)

@pytest.mark.parametrize('case', CASES)
@pytest.mark.parametrize('warm', [False, True])
def test_enabled_3a_bytes_unchanged(case, warm):
    expected = json.loads(FIXTURE.read_text())['cases'][f'{case}/{int(warm)}']
    assert expected['trade_count'] > 0
    assert fingerprint(case, warm) == expected

if __name__ == '__main__':
    import subprocess
    sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    assert sha == '11e8cfbfccb68776eeb755aa3ad1296bf4034bce'
    cases = {f'{case}/{int(warm)}': fingerprint(case, warm) for case in CASES for warm in (False, True)}
    assert all(c['trade_count'] > 0 for c in cases.values())
    FIXTURE.write_text(json.dumps(dict(baseline_sha=sha, cases=cases), indent=2) + '\n')
    print('CAPTURED=' + str(len(cases)))
