"""Run compile-valid calendar mutations and restore source via copy/cmp; repository cwd required."""
from pathlib import Path
import hashlib
import json
import py_compile
import shutil
import subprocess
import sys

Path('/tmp/f2').mkdir(parents=True, exist_ok=True)
source = Path('backtesting-py/strategy_calendar_schedule.py')
backup = Path('/tmp/f2/strategy_calendar_schedule.py.bak')
shutil.copyfile(source, backup)
original = backup.read_text()
mutations = [
    ('M1-current-bar-fill', '                self._pending_entry = (self.orders[-1], record, stop)',
     '                self._pending_entry = (self.orders[-1], record, stop)\n                self._broker._process_orders()', 'causal_entry_holding'),
    ('M2-clamp-monthday', '(self.monthday and day.day != self.monthday)',
     "(self.monthday and day.day != min(self.monthday, __import__('calendar').monthrange(day.year, day.month)[1]))", 'monthday_31'),
    ('M3-holding-new-entry', '            if self.position:\n                self._risk_check_exit()',
     '            if self.position:\n                if due:\n                    self._submit_entry(event, float(self.data.Close[-1]), has_next=True)\n                self._risk_check_exit()', 'no_add_or_reverse'),
    ('M4-UTC-flatten', 'bar_open=self.data.index[-1], context=self._calendar_clock)',
     "bar_open=self.data.index[-1], context=__import__('dataclasses').replace(self._calendar_clock, zone=ZoneInfo('UTC')))", 'weekly_friday_to_monday'),
    ('M5-ignore-gap-stop', '                    if stop is not None and ((order.is_long',
     '                    if False and stop is not None and ((order.is_long', 'gap_wrong_side'),
    ('M6-lost-first-event', '                if self._broker._i == 1:',
     '                if False and self._broker._i == 1:', 'first_closed_bar'),
    ('M7-time-before-stop', '            closed = self._risk_layer_check_exit()',
     "            if self._holding_expiry().due:\n                self._risk_state = __import__('dataclasses').replace(self._risk_state, initial_stop=None)\n            closed = self._risk_layer_check_exit()", 'stop_then_time'),
]
results = []
try:
    for name, old, new, test in mutations:
        assert original.count(old) == 1, (name, original.count(old))
        source.write_text(original.replace(old, new))
        py_compile.compile(str(source), doraise=True)
        with Path('/tmp/f2/'+name+'.log').open('w') as log:
            run = subprocess.run([sys.executable, '-m', 'pytest', 'backtesting-py/tests/test_f2_calendar.py',
                '-k', test, '-q', '-p', 'no:cacheprovider'], stdout=log, stderr=subprocess.STDOUT)
            log.write('\nEXIT='+str(run.returncode)+'\n')
        shutil.copyfile(backup, source)
        cmp = subprocess.run(['cmp', str(source), str(backup)], capture_output=True)
        results.append(dict(name=name, compile='pass', test=test, exit=run.returncode, restore_cmp_exit=cmp.returncode))
        print(json.dumps(results[-1]), flush=True)
        assert run.returncode == 1 and cmp.returncode == 0
finally:
    shutil.copyfile(backup, source)
    Path('/tmp/f2/mutations.json').write_text(json.dumps(dict(results=results,
        restored_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        backup_sha256=hashlib.sha256(backup.read_bytes()).hexdigest()), indent=2)+'\n')
