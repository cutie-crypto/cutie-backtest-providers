"""One-time 9T5 off-state capture. Existing filter rows must remain unchanged."""
from pathlib import Path
import json
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
import capture_entry_filters_5b8320a as filters
import capture_time_layer_11e8cfb as clock


def main():
    name='fibonacci_retracement'
    path=Path(__file__).parent/'entry_filters_5b8320a.json'
    baseline=json.loads(path.read_text())
    existing=dict(baseline['cases'])
    for case, params in ((name,{}),(name+'@long',dict(direction='long'))):
        for warm in (False,True):
            key=case+'/'+str(int(warm))
            assert key not in baseline['cases'], 'capture once; do not overwrite goldens'
            baseline['cases'][key]=filters.snapshot(name,params,warm)
    assert all(baseline['cases'][k]==v for k,v in existing.items())
    path.write_text(json.dumps(baseline,indent=2)+'\n')
    single={name:dict(min_bars=clock.provider.TOOL_SPECS['local.backtesting_py.'+name]['build']({})['min_bars'])}
    for warm in (False,True):
        body=clock.response(name,{},warm)
        assert 'time_layer' not in body['assumptions'] and 'time_layer' not in body['raw_report']
        single[name][str(int(warm))]=dict(warmup_bars=body['assumptions']['indicator_warmup_bars'],
            assumptions_sha256=clock.digest(body['assumptions']),raw_report_sha256=clock.digest(body['raw_report']))
    Path(__file__).with_name('time_layer_single_9t5_b42210b.json').write_text(json.dumps(
        dict(base_sha='b42210ba1f47a2dcd98c9e2abbf9a0754088a5e5',single=single),indent=2)+'\n')
    capture_isolated()
    print('9T5 CAPTURED filter=4 time=2; existing filter rows preserved')


def capture_isolated():
    from unittest.mock import patch
    import hashlib
    import tempfile
    from fastapi.testclient import TestClient
    from backtesting import Backtest
    from canonical_json import canonical_json
    import test_leverage_params as leverage
    cases={}
    data=clock.compat.frame().iloc[60:].copy()
    with tempfile.TemporaryDirectory(prefix='9t5-off-capture-') as reports, \
         patch.object(clock.provider,'_fetch_ohlcv',lambda *a,**k:data.copy()), \
         patch.object(clock.provider,'_fetch_template_warmup',lambda *a,**k:data.iloc[:0].copy()), \
         patch.object(clock.provider,'REPORTS_DIR',Path(reports)), \
         patch.object(Backtest,'plot',return_value=None):
        for market in ('futures','spot'):
            req=leverage.request({},market=market,name='fibonacci_retracement')
            req['backtest'].update(start_at=int(data.index[0].timestamp()),end_at=int(data.index[-1].timestamp())+3600)
            body=TestClient(clock.provider.app).post('/cutie/backtest',json=req).json()
            assert body['result_status']=='success',body
            digest=lambda value:hashlib.sha256(value.encode()).hexdigest()
            cases[market]=dict(v2=digest(canonical_json({k:body[k] for k in clock.V2_KEYS})),
                **{k:digest(json.dumps(body[k],sort_keys=True,separators=(',',':'))) for k in ('assumptions','raw_report')})
    path=Path(__file__).with_name('isolated_off_9t5_b42210b.json')
    assert not path.exists(),'capture once'
    path.write_text(json.dumps(dict(base_sha='b42210ba1f47a2dcd98c9e2abbf9a0754088a5e5',cases=cases),indent=2)+'\n')


if __name__=='__main__':
    main()
