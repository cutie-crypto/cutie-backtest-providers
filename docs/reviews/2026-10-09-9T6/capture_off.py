"""Append only the new tool's compatibility fingerprints, never recognition goldens."""
from pathlib import Path
import json
import sys
ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT/'backtesting-py'),str(ROOT/'backtesting-py/tests'),str(ROOT/'backtesting-py/tests/fixtures')]
import capture_entry_filters_5b8320a as filters
import capture_time_layer_11e8cfb as time
import cutie_backtesting_provider as p
fixtures=ROOT/'backtesting-py/tests/fixtures'
path=fixtures/'entry_filters_5b8320a.json'
original=json.loads(path.read_text())
old=dict(original['cases'])
for case,(name,params) in filters.cases().items():
    if name!='chan_3buy': continue
    for warm in (False,True):
        key=case+'/'+str(int(warm))
        assert key not in original['cases'],key
        original['cases'][key]=filters.snapshot(name,params,warm)
assert all(original['cases'][k]==v for k,v in old.items())
path.write_text(json.dumps(original,indent=2)+'\n')
case=dict(min_bars=p.TOOL_SPECS['local.backtesting_py.chan_3buy']['build']({})['min_bars'])
for warm in (False,True):
    body=time.response('chan_3buy',{},warm)
    case[str(int(warm))]=dict(warmup_bars=body['assumptions']['indicator_warmup_bars'],
        assumptions_sha256=time.digest(body['assumptions']),raw_report_sha256=time.digest(body['raw_report']))
(fixtures/'9t6_chan_off.json').write_text(json.dumps(dict(single=dict(chan_3buy=case)),indent=2)+'\n')
print('old filter cases preserved:',len(old),'appended:',len(original['cases'])-len(old))
