"""Append only the two new tools' disabled fingerprints; preserve existing cases."""
from pathlib import Path
import importlib.util
import json
import sys

root = Path(__file__).resolve().parents[3]
fixtures = root / 'backtesting-py/tests/fixtures'
sys.path[:0] = [str(root/'backtesting-py'), str(root/'backtesting-py/tests'), str(fixtures)]
import cutie_backtesting_provider as p
import capture_entry_filters_5b8320a as capture
import capture_time_layer_11e8cfb as time_capture

names = ('macd_bullish_divergence', 'rsi_bullish_divergence')
path = fixtures / 'entry_filters_5b8320a.json'
baseline = json.loads(path.read_text())
original = dict(baseline['cases'])
for key, (name, params) in capture.cases().items():
    if name not in names:
        continue
    for warm in (False, True):
        case = key+'/'+str(int(warm))
        assert case not in baseline['cases'], case
        baseline['cases'][case] = capture.snapshot(name, params, warm)
assert all(baseline['cases'][key] == value for key,value in original.items())
path.write_text(json.dumps(baseline, indent=2)+'\n')
single = {}
for name in names:
    single[name] = dict(min_bars=p.TOOL_SPECS['local.backtesting_py.'+name]['build']({})['min_bars'])
    for warm in (False, True):
        body = time_capture.response(name, {}, warm)
        single[name][str(int(warm))] = dict(warmup_bars=body['assumptions']['indicator_warmup_bars'],
            assumptions_sha256=time_capture.digest(body['assumptions']),
            raw_report_sha256=time_capture.digest(body['raw_report']))
(fixtures/'9t4_divergence_off.json').write_text(json.dumps(dict(base_sha='b42210b',single=single),indent=2)+'\n')
print('APPENDED_FILTER_CASES',len(baseline['cases'])-len(original),'TIME_TOOLS',len(single))
