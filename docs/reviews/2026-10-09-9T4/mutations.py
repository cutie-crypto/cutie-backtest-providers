"""Compile-valid negative controls; always restore bytes, run green and cmp."""
from pathlib import Path
import hashlib
import json
import os
import py_compile
import shutil
import subprocess

root=Path(__file__).resolve().parents[3]
p=root/'backtesting-py/strategy_divergence.py'
logs=Path('/tmp/9t4')
backup=logs/'strategy_divergence.before-mutations.py'
shutil.copyfile(p,backup)
original=backup.read_text()
cases=[
 ('01_unconfirmed_swing','point = swings.lows[i]',
  'point = swings.lows[min(i+n, len(close)-1)]', 'unconfirmed_second_low or hand_low_bar'),
 ('02_confirmation_indicator','a, b = values[left.index], values[point.index]',
  'a, b = values[left.confirmed_at], values[point.confirmed_at]', 'samples_low_bar or hand_low_bar'),
 ('03_reversed_price','point.price < left.price and b > a',
  'point.price > left.price and b > a', 'price_direction_strict or hand_low_bar'),
 ('04_rsi_threshold',"and (kind != 'rsi' or a < first_below)",
  'and True', 'rsi_threshold_strict'),
 ('05_close_invalidation','if close[i] < armed.stop:',
  'if False:', 'close_invalidation'),
 ('06_signal_price_r','self._targets[tag] = trade.entry_price + 2 * (trade.entry_price - tag.stop)',
  'self._targets[tag] = float(self.data.Close[tag.signal_bar]) + 2 * (float(self.data.Close[tag.signal_bar]) - tag.stop)',
  '2r_uses_actual_fill'),
 ('07_new_low_invalidation','if armed is not None:',
  'if False:', 'new_confirmed_low_invalidates'),
]
results=[]
def run(name,selector):
    for cache in (p.parent/'__pycache__').glob('strategy_divergence.*.pyc'):cache.unlink()
    log=logs/(name+'.log')
    with log.open('w') as out:
        completed=subprocess.run(['python3','-m','pytest','backtesting-py/tests/test_9t4_divergence.py',
            '-q','-p','no:cacheprovider','-k',selector],cwd=root,stdout=out,stderr=subprocess.STDOUT,
            env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
    lines=log.read_text().splitlines()
    return completed.returncode,[line for line in lines if line.startswith('FAILED ')],[line for line in lines if ' passed' in line or ' failed' in line]
try:
    for name,before,after,selector in cases:
        assert original.count(before)==1,(name,original.count(before))
        p.write_text(original.replace(before,after))
        py_compile.compile(str(p),doraise=True)
        red=run(name+'-red',selector)
        assert red[0]==1 and red[1],(name,red)
        shutil.copyfile(backup,p)
        green=run(name+'-restored',selector)
        assert green[0]==0,(name,green)
        cmp_exit=subprocess.run(['cmp',str(backup),str(p)]).returncode
        assert cmp_exit==0
        results.append(dict(name=name,compile='passed',red_exit=red[0],failures=red[1],red=red[2],
                            restored_exit=green[0],restored=green[2],cmp_exit=cmp_exit))
        (logs/'mutations.json').write_text(json.dumps(results,indent=2,ensure_ascii=False)+'\n')
        print(name,'RED',red[2],'GREEN',green[2],'CMP=0',flush=True)
finally:
    shutil.copyfile(backup,p)
    for cache in (p.parent/'__pycache__').glob('strategy_divergence.*.pyc'):cache.unlink()
print('RESTORED_SHA256',hashlib.sha256(p.read_bytes()).hexdigest(),flush=True)
