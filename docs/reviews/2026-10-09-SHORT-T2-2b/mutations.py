from pathlib import Path
import hashlib, json, os, py_compile, shutil, subprocess
root=Path(__file__).resolve().parents[3]/'backtesting-py'
p=root/'cutie_backtesting_provider.py'
backup=Path('/tmp/t22b/provider.before-mutations.py')
shutil.copyfile(p,backup)
original=backup.read_text()
condition='''if not candidate["liquidation_gap"] and stop is not None and (
            stop > price if trade.is_long else stop < price
        ):'''
legacy='''        # Legacy stops are close-only; any intrabar liquidation precedes them.
        if self._risk.get("leverage", 1) > 1 and self._risk_isolated_exit():
            return True
'''
early='''        if sl_pct is None and tp_pct is None and not time_enabled:
            return False
'''
cases=[
 ('01_stop_first', original.replace(condition,'''if stop is not None and (
            Decimal(str(self.data.Low[-1])) <= stop if trade.is_long else Decimal(str(self.data.High[-1])) >= stop
        ):'''), 'test_same_bar_stop_beyond_liquidation'),
 ('02_equal_stop', original.replace('stop > price if trade.is_long else stop < price','stop >= price if trade.is_long else stop <= price'), 'test_same_bar_stop_equal_liquidation'),
 ('03_no_gap', original.replace('if not candidate["liquidation_gap"] and stop is not None and (','if stop is not None and ('), 'test_same_bar_gap_open_beyond_liquidation'),
 ('04_legacy_early_return', original.replace(legacy,'').replace(early,early+legacy), 'test_legacy_no_stops_still_liquidates'),
 ('05_percentage_units', original.replace('stop_loss_pct=params.get("stop_loss_pct")','stop_loss_pct=risk.get("stop_loss_pct")'), 'test_http_raw_stop_percentage_and_dynamic_count'),
 ('06_no_equity_correction', original.replace('broker._cash = float((capital + self._isolated_realized - open_cost) / scale)','pass  # mutation: retain native cash'), 'test_liquidation_reentry_uses_rewritten_equity'),
 ('07_spot_open', original.replace('''    if market == "spot" and leverage > 1:
        return "leverage above 1 requires futures market"
''',''), 'test_public_leverage_gate_futures_open_spot_closed'),
]
results=[]
env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1')
def run(name,select):
    for cache in (root/'__pycache__').glob('cutie_backtesting_provider.*.pyc'): cache.unlink()
    log=Path('/tmp/t22b')/(name+'.log')
    with log.open('w') as out:
        result=subprocess.run(['python3','-m','pytest','tests/test_isolated_liquidation_arbitration.py',
            'tests/test_isolated_liquidation_settlement.py','-q','-p','no:cacheprovider','-k',select],
            cwd=root,stdout=out,stderr=subprocess.STDOUT,env=env)
    lines=log.read_text().splitlines()
    verdict=[line for line in lines if ' passed' in line or ' failed' in line]
    failed=[line for line in lines if line.startswith('FAILED ')]
    return result.returncode,verdict,failed
try:
    for name,source,select in cases:
        assert source!=original,name
        p.write_text(source)
        py_compile.compile(str(p),doraise=True)
        red=run(name+'-red',select)
        assert red[0]==1 and red[2],(name,red)
        shutil.copyfile(backup,p)
        green=run(name+'-restored',select)
        assert green[0]==0,(name,green)
        cmp=subprocess.run(['cmp',str(backup),str(p)]).returncode
        assert cmp==0
        entry=dict(name=name,red_exit=red[0],red=red[1],failed=red[2],green_exit=green[0],green=green[1],cmp=cmp)
        results.append(entry)
        Path('/tmp/t22b/mutations.json').write_text(json.dumps(results,indent=2,ensure_ascii=False)+'\n')
        print(name,red[1],'=>',green[1],'cmp=0',flush=True)
finally:
    shutil.copyfile(backup,p)
    for cache in (root/'__pycache__').glob('cutie_backtesting_provider.*.pyc'): cache.unlink()
print('RESTORED_SHA256',hashlib.sha256(p.read_bytes()).hexdigest(),flush=True)
