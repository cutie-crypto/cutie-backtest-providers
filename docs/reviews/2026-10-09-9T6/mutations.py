"""Compile-valid single-fault mutations; backup/restore/cmp with real test exits."""
from pathlib import Path
import hashlib
import json
import py_compile
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[3]
source=ROOT/'backtesting-py/strategy_chan.py'
logs=Path('/tmp/9t6')
backup=logs/'strategy_chan.before-mutations.py'
original=source.read_bytes()
backup.write_bytes(original)
text=original.decode()
cases=[
 ('no_inclusion', "        if included:\n", "        if False:\n", 'inclusion'),
 ('no_new_bi_gap', "        if point['merged_index'] - previous['merged_index'] < self.gap:", "        if False:", 'spacing or gap_checked'),
 ('wrong_zg_max', "                zg = min(b['high'] for b in triple)", "                zg = max(b['high'] for b in triple)", 'hand_counted_center'),
 ('non_strict_pullback', "                eligible = stroke['low'] > center['zg']", "                eligible = stroke['low'] >= center['zg']", 'pullback_strictly'),
 ('unconfirmed_bottom', "            self._signals = tuple(signals)", "            self._signals = tuple(signals[1:]) + (None,)", 'next_open_and_actual'),
 ('center_twice', "if eligible and not center['used']", "if eligible", 'same_center_has_only_one'),
]
results=[]
try:
 for name,before,after,selection in cases:
  assert text.count(before) >= 1,(name,'missing mutation target')
  source.write_text(text.replace(before,after))
  py_compile.compile(str(source),doraise=True)
  with (logs/(name+'.log')).open('w') as out:
   code=subprocess.run([sys.executable,'-m','pytest','backtesting-py/tests/test_9t6_chan_3buy.py','-q','-p','no:cacheprovider','-k',selection],cwd=ROOT,stdout=out,stderr=subprocess.STDOUT).returncode
  source.write_bytes(backup.read_bytes())
  cmp_code=subprocess.run(['cmp','-s',str(backup),str(source)]).returncode
  with (logs/(name+'-restored.log')).open('w') as out:
   restored=subprocess.run([sys.executable,'-m','pytest','backtesting-py/tests/test_9t6_chan_3buy.py','-q','-p','no:cacheprovider'],cwd=ROOT,stdout=out,stderr=subprocess.STDOUT).returncode
  results.append(dict(name=name,mutated_exit=code,restored_exit=restored,cmp_exit=cmp_code,
      restored_sha256=hashlib.sha256(source.read_bytes()).hexdigest()))
  print(results[-1],flush=True)
  assert code==1 and restored==0 and cmp_code==0,results[-1]
finally:
 source.write_bytes(backup.read_bytes())
 (Path(__file__).parent/'mutations.json').write_text(json.dumps(results,indent=2)+'\n')
