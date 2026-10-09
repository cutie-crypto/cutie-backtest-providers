"""Capture once at 5b8320a; tests never regenerate this immutable baseline."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cutie_backtesting_provider as provider
import test_risk_overlay_compatibility as compat
import capture_time_layer_11e8cfb as capture
from backtesting import Backtest
from canonical_json import canonical_json


def cases():
    result = {}
    for tool_id, spec in provider.TOOL_SPECS.items():
        if spec.get('runner') in ('kernel_v3', provider.SCALE_IN_OUT_RUNNER):
            continue
        name = tool_id.removeprefix('local.backtesting_py.')
        result[name] = (name, {})
        for direction in spec['param_schema_properties'].get('direction', {}).get('enum', []):
            result[name + '@' + direction] = (name, {'direction': direction})
    return result


def snapshot(name, params, warm):
    full = compat.frame()
    prefix, data = full.iloc[:60], full.iloc[60:].copy()
    built = provider.TOOL_SPECS['local.backtesting_py.' + name]['build'](params)
    cls = built['strategy']
    if warm:
        cls._warmup_bars = len(prefix)
        cls._warmup_cols = {c: prefix[c].to_numpy() for c in provider._WARMUP_COLUMNS}
    stats = Backtest(data, cls, cash=100000, commission=.001, exclusive_orders=True,
                     finalize_trades=True).run()
    def rows(frame):
        return [[compat_encode(v) for v in row] for row in frame.itertuples(index=False, name=None)]
    body = capture.response(name, params, warm)
    return dict(min_bars=built['min_bars'],
        engine_trades=capture.digest([list(stats['_trades'].columns), rows(stats['_trades'])]),
        engine_equity=capture.digest(rows(stats['_equity_curve'])),
        result_v2=hashlib.sha256(canonical_json({k: body[k] for k in capture.V2_KEYS}).encode()).hexdigest(),
        warmup_bars=body['assumptions']['indicator_warmup_bars'],
        assumptions=capture.digest(body['assumptions']), raw_report=capture.digest(body['raw_report']))


def compat_encode(value):
    import pandas as pd
    if value is pd.NaT:
        return 'NaT'
    if isinstance(value, float):
        return value.hex()
    if isinstance(value, (pd.Timestamp, pd.Timedelta)):
        return str(value)
    return value


if __name__ == '__main__':
    sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    assert sha == '5b8320a87116eaa0fd36c36ae277289bb502b8e8', sha
    values = {key + '/' + str(int(warm)): snapshot(name, params, warm)
              for key, (name, params) in cases().items() for warm in (False, True)}
    Path(__file__).with_name('entry_filters_5b8320a.json').write_text(
        json.dumps(dict(baseline_sha=sha, cases=values), indent=2) + '\n')
    print('CAPTURED', len(values))
