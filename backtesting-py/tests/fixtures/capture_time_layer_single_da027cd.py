"""Capture Keltner only in detached da027cd, which predates the time layer.

Copy this file and capture_time_layer_11e8cfb.py into the baseline's fixtures
folder, then run this file there. Tests only read the resulting frozen fixture.
"""
from pathlib import Path
import json
import subprocess

import capture_time_layer_11e8cfb as capture


def main():
    root = Path(__file__).resolve().parents[3]
    sha = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    assert sha == 'da027cdd98c02b6c9735c2e2f4752345423a3f46', sha
    assert Path(capture.provider.__file__).resolve() == root / 'backtesting-py/cutie_backtesting_provider.py'
    assert all('time_layer_enabled' not in spec['param_schema_properties']
               for spec in capture.provider.TOOL_SPECS.values())
    name = 'keltner_breakout'
    tool = capture.provider.TOOL_SPECS['local.backtesting_py.' + name]
    single = {name: dict(min_bars=tool['build']({})['min_bars'])}
    for warm in (False, True):
        body = capture.response(name, {}, warm)
        assert 'time_layer' not in body['assumptions']
        assert 'time_layer' not in body['raw_report']
        single[name][str(int(warm))] = dict(
            warmup_bars=body['assumptions']['indicator_warmup_bars'],
            assumptions_sha256=capture.digest(body['assumptions']),
            raw_report_sha256=capture.digest(body['raw_report']))
    Path(__file__).with_name('time_layer_single_da027cd.json').write_text(
        json.dumps(dict(baseline_sha=sha, single=single), indent=2) + '\n')
    print(f'CAPTURED single=1 baseline_sha={sha}')


if __name__ == '__main__':
    main()
