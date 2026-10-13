"""Capture from existing snapshot/response/fingerprint helpers; never rewrite financial hashes."""
import hashlib,json,sys
from pathlib import Path
sys.path[:0]=['backtesting-py','backtesting-py/tests','backtesting-py/tests/fixtures']
import cutie_backtesting_provider as p
import test_entry_filters as entry
import test_time_layer_compatibility as time
original=p._parse_fixed_risk_params
p._parse_fixed_risk_params=lambda params,**kw:original({'risk_layer_enabled':False,**params},**kw)
fixture=json.loads(Path('backtesting-py/tests/fixtures/warmup_b3_fixture_cases.json').read_text())
allowed=fixture['cases']
response=time.capture.response

def checked_response(name, params, warm=True, *, complete=False):
    body=response(name,params,warm,complete=complete)
    inert={**p._TIME_PARAM_SCHEMA_PROPERTIES,**p.FILTER_PARAM_SCHEMA_PROPERTIES}
    core={k:v for k,v in params.items() if k not in inert}
    [expected]=[r for r in fixture['financial_fingerprints']
                if (r['name'],r['params'],r['warm'])==(name,core,warm)]
    value={k:body[k] for k in ('trades','equity_curve','metrics','data_manifest')}
    digest=hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
    assert digest==expected['financial_sha256'],(name,params,warm,'financial fields changed; stop')
    projected=dict(assumptions={k:v for k,v in body['assumptions'].items()
        if k not in ('indicator_warmup_bars','warmup_manifest','warmup_manifest_hash','ema_warmup')},
        raw_report={k:v for k,v in body['raw_report'].items() if k!='ema_warmup'})
    report_hash=hashlib.sha256(json.dumps(projected,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
    assert report_hash==expected['report_sha256'],(name,params,warm,'non-warmup report changed; stop')
    return body

time.capture.response=checked_response
entry.capture.capture.response=checked_response
out=dict(baseline_sha='2840fa9',filters={},single={},ledger={});audit=[]
for case,(name,params) in entry.CASES.items():
 for warm in (False,True):
  if dict(name=name,params=params,warm=warm) not in allowed:continue
  actual=entry.capture.snapshot(name,params,warm,complete=True);expected=entry.BASELINE['cases'][case+'/'+str(int(warm))]
  # Every original engine and result.v2 hash must remain frozen.
  changed=[k for k in actual.keys()|expected.keys() if k not in ('warmup_bars','assumptions','raw_report') and actual.get(k)!=expected.get(k)]
  audit.append(dict(case='filters/'+case+'/'+str(int(warm)),changed=changed))
  assert not changed,(case,warm,changed)
  out['filters'][case+'/'+str(int(warm))]={k:actual[k] for k in ('warmup_bars','assumptions','raw_report')}
for name in time.LEGACY_MIXINS:
 params={'direction':'short'} if name.endswith('_short') else {}
 tool=time.compat.tool_name(name)
 for warm in (False,True):
  if dict(name=tool,params=params,warm=warm) not in allowed:continue
  body=time.capture.response(tool,{**params,**time.DEFAULTS},warm,complete=True)
  assert p.TOOL_SPECS['local.backtesting_py.'+tool]['build']({**params,**time.DEFAULTS})['min_bars']==time.BASELINE['single'][name]['min_bars']
  audit.append(dict(case='single/'+name+'/'+str(int(warm)),changed=[],financial_and_report_hashes_checked=True))
  out['single'].setdefault(name,{})[str(int(warm))]=dict(
    warmup_bars=body['assumptions']['indicator_warmup_bars'],
    assumptions_sha256=time.capture.digest(body['assumptions']),
    raw_report_sha256=time.capture.digest(body['raw_report']))
for name,params in time.capture.LEDGER_PARAMS.items():
 if dict(name=name,params=params,warm=True) not in allowed:continue
 actual=time.capture.ledger_fingerprint(name,complete=True);expected=time.BASELINE['ledger'][name]
 changed=[k for k in actual.keys()|expected.keys() if k not in ('warmup_bars','assumptions_sha256','raw_report_sha256') and actual.get(k)!=expected.get(k)]
 audit.append(dict(case='ledger/'+name,changed=changed))
 assert not changed,(name,changed)
 out['ledger'][name]={k:actual[k] for k in ('warmup_bars','assumptions_sha256','raw_report_sha256')}
Path(sys.argv[1] if len(sys.argv)>1 else '/tmp/warmup-b3/disclosure-audit.json').write_text(json.dumps(audit,indent=2))
# Only captured metadata; original engine/trade/equity/result hashes stay in place.
Path('backtesting-py/tests/fixtures/warmup_b3_disclosure.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
print('CAPTURED filter='+str(len(out['filters'])),'single='+str(sum(map(len,out['single'].values()))),'ledger='+str(len(out['ledger'])))
print('STOPPED',[(r['case'],r['changed']) for r in audit if r['changed']])
