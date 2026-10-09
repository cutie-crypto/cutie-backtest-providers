from pathlib import Path
import subprocess, shutil, sys
f=Path('backtesting-py/strategy_calendar_templates.py'); backup=Path('/tmp/f3/strategy_calendar_templates.backup.py');shutil.copy2(f,backup);original=f.read_text()
mutations=[
 ('fixed_utc', "end = bounds.start_utc + timedelta(minutes=window)", "bounds = type(bounds)(bounds.start_utc.replace(hour=13, minute=30), bounds.end_utc)\n            end = bounds.start_utc + timedelta(minutes=window)"),
 ('first_close', 'self.data.Open[indices[0]]', 'self.data.Close[indices[0]]'),
 ('repeat_day', 'if day is None or day in self._attempted:', 'if day is None or False:'),
 ('wrong_stop', "min(self.data.Low[indices]) if side == 'long' else max(self.data.High[indices])", "max(self.data.High[indices]) if side == 'long' else min(self.data.Low[indices])"),
 ('late_flatten', "flatten_at='16:00'", "flatten_at='16:15'"),
]
results=[]
try:
 for name,old,new in mutations:
  assert original.count(old)==1
  changed=original.replace(old,new)
  if name=='repeat_day': changed=changed.replace('if decision != end:', 'if decision < end:')
  compile(changed,str(f),'exec'); f.write_text(changed)
  with open('/tmp/f3/mutation_'+name+'.log','w') as out:
   code=subprocess.run([sys.executable,'-m','pytest','backtesting-py/tests/test_f3_us_open.py','-q','-p','no:cacheprovider'],stdout=out,stderr=subprocess.STDOUT).returncode
  text=Path('/tmp/f3/mutation_'+name+'.log').read_text(); assert code==1 and ' failed' in text,(name,code)
  results.append(f'{name}: EXIT={code} compile=OK killed');shutil.copy2(backup,f)
finally:
 shutil.copy2(backup,f)
 assert subprocess.run(['cmp',str(backup),str(f)]).returncode==0
Path('/tmp/f3/mutations.txt').write_text('\n'.join(results)+'\nRESTORE cmp=0\n')
print('\n'.join(results));print('RESTORE cmp=0')
