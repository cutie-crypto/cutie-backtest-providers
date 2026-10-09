"""Capture only from detached c78def6, before merging 3a. Never modifies a baseline.

Usage: python3 capture_risk_overlay_c78def6.py /tmp/cx4-main-c78def6
Uses the existing compatibility helper's exact frame, warmup split and RISK_CASES.
"""
from __future__ import annotations
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

PARAMS = {
    'stoch_oversold_cross': dict(stoch_period=14, stoch_smooth=3, stoch_d=3, oversold=30, overbought=70),
    'bollinger_squeeze_breakout': dict(bb_period=20, bb_std=1, bandwidth_lookback=50, squeeze_pct=40),
    'adx_di_cross': dict(adx_period=7, adx_threshold=15, adx_exit=10),
    'macd_above_zero': dict(fast=5, slow=15, signal=3),
    'ema_triple_alignment': dict(ema_short=5, ema_mid=20, ema_long=60),
    'bias_reversion': dict(ema_period=10, bias_entry_pct=1),
    'ema_pullback_short': dict(ema_fast=5, ema_slow=30, pullback_tolerance_pct=1, direction='short'),
}


def main():
    baseline = Path(sys.argv[1]).resolve()
    sha = subprocess.check_output(['git', '-C', str(baseline), 'rev-parse', 'HEAD'], text=True).strip()
    assert sha == 'c78def6e45472d68416db9b534299599c433d463', sha
    sys.path.insert(0, str(baseline / 'backtesting-py'))
    import cutie_backtesting_provider as provider
    assert Path(provider.__file__).resolve() == baseline / 'backtesting-py/cutie_backtesting_provider.py'
    assert 'risk_layer_enabled' not in provider._FIXED_RISK_PARAM_SCHEMA_PROPERTIES
    helper_path = Path(__file__).resolve().parents[1] / 'test_risk_overlay_compatibility.py'
    spec = importlib.util.spec_from_file_location('capture_compat', helper_path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    cases = {}
    for name, params in PARAMS.items():
        tool = 'ema_pullback' if name == 'ema_pullback_short' else name
        helper.PARAMS[tool] = params
        for risk in helper.RISK_CASES:
            for warm in (False, True):
                value = helper.fingerprint(tool, risk, warm)
                assert value['trade_count'] > 0, (name, risk, warm)
                cases[f'{name}/{risk}/{int(warm)}'] = value
    output = Path(__file__).with_name('risk_overlay_c78def6.json')
    output.write_text(json.dumps(dict(baseline_sha=sha, params=PARAMS, cases=cases), indent=2) + '\n')
    print('CAPTURED=' + str(len(cases)))


if __name__ == '__main__':
    main()
