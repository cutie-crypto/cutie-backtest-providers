"""Five compile-valid mutants; guaranteed backup restore and cmp."""
from pathlib import Path
import subprocess, shutil, sys
f=Path('backtesting-py/strategy_calendar_templates.py');backup=Path('/tmp/f4/strategy_calendar_templates.backup.py');shutil.copy2(f,backup);original=f.read_text()
mutations=[
 ('sunday_lookahead', 'sunday_price = self._closed_prices.get(sunday_open)', 'sunday_price = Decimal(str(self._future_closes[len(self.data)])) if len(self.data)<len(self._future_closes) else None'),
 ('fixed_friday_utc', 'friday_close = self._calendar._session_for_day(sunday-timedelta(days=2)).end_utc', 'friday_close = self._calendar._session_for_day(sunday-timedelta(days=2)).end_utc.replace(hour=21)'),
 ('strict_threshold', "side = 'short' if move >= threshold else 'long' if move <= -threshold else None", "side = 'short' if move > threshold else 'long' if move < -threshold else None"),
 ('late_expiry', 'flatten_weekdays=4)', 'flatten_weekdays=8)'),
 ('neighbor_fallback', 'friday_price = self._closed_prices.get(friday_close)', 'friday_price = self._closed_prices.get(friday_close, self._closed_prices.get(friday_close-self._period))'),
]
results=[]
try:
 for name,old,new in mutations:
  assert original.count(old)==1
  changed=original.replace(old,new)
  if name=='sunday_lookahead': changed=changed.replace('            self._closed_prices = {}','            self._future_closes = list(self.data.Close)\n            self._closed_prices = {}')
  compile(changed,str(f),'exec'); f.write_text(changed)
  with open('/tmp/f4/mutation_'+name+'.log','w') as out:
   code=subprocess.run([sys.executable,'-m','pytest','backtesting-py/tests/test_f4_cme_gap.py','-q','-p','no:cacheprovider'],stdout=out,stderr=subprocess.STDOUT).returncode
  text=Path('/tmp/f4/mutation_'+name+'.log').read_text();assert code==1 and ' failed' in text,(name,code)
  results.append(f'{name}: EXIT={code} compile=OK killed');shutil.copy2(backup,f)
finally:
 shutil.copy2(backup,f)
 assert subprocess.run(['cmp',str(backup),str(f)]).returncode==0
Path('/tmp/f4/mutations.txt').write_text('\n'.join(results)+'\nRESTORE cmp=0\n')
print('\n'.join(results));print('RESTORE cmp=0')
