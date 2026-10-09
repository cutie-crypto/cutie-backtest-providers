"""Five independently compiled mutations; exact backups restored after every run."""
from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import sys

root = Path.cwd()
logs = Path('/tmp/6e2')
logs.mkdir(exist_ok=True)
mutations = [
    ('signal_bar', 'backtesting-py/strategy_time_layer.py',
     '.contains_bar(decision_utc, timeframe)', '.contains_bar(decision_utc - self.period, timeframe)',
     'hand_calculated_fills'),
    ('gated_exit', 'backtesting-py/cutie_backtesting_provider.py',
     '    def _risk_check_exit(self) -> bool:\n',
     '    def _risk_check_exit(self) -> bool:\n        if not self._time_allow_entry():\n            return False\n',
     'whole_fill_bar_and_unrestricted_expiry and single'),
    ('ungated_adds', 'backtesting-py/scale_in_out_ledger.py',
     'if time_context is not None and pending[0] == BUY:',
     'if time_context is not None and pending[0] == BUY and not ledger.lots:',
     'ledger_adds_blocked'),
    ('unchecked_timezone', 'backtesting-py/strategy_time_layer.py',
     "        validate_calendar_timezone(values['time_calendar'], name)",
     "        pass  # mutation: missing timezone consistency check",
     'invalid_calendar_before_fetch and timezone'),
    ('turtle_schema', 'backtesting-py/cutie_backtesting_provider.py',
     'if _tool_spec.get("runner") in ("kernel_v3", TURTLE_RUNNER):',
     'if _tool_spec.get("runner") == "kernel_v3":',
     'calendar_schema_matches_consumers'),
    ('partial_candle', 'backtesting-py/strategy_time_layer.py',
     '.contains_bar(decision_utc, timeframe)', '.is_open(decision_utc)',
     'whole_fill_bar_and_unrestricted_expiry'),
]
records = []
for name, filename, old, new, selector in mutations:
    source = root/filename
    backup = logs/(name+'.backup')
    shutil.copyfile(source, backup)
    original = source.read_text()
    assert original.count(old) == 1, (name, original.count(old))
    try:
        source.write_text(original.replace(old, new))
        compile_rc = subprocess.run([sys.executable, '-m', 'py_compile', str(source)]).returncode
        assert compile_rc == 0
        with (logs/(name+'.log')).open('w') as output:
            rc = subprocess.run([sys.executable, '-m', 'pytest', 'backtesting-py/tests/test_time_layer_6e2.py',
                '-k', selector, '-q', '-p', 'no:cacheprovider'], stdout=output, stderr=subprocess.STDOUT).returncode
    finally:
        shutil.copyfile(backup, source)
        cmp_rc = subprocess.run(['cmp', str(backup), str(source)]).returncode
    text = (logs/(name+'.log')).read_text()
    restored_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    backup_sha = hashlib.sha256(backup.read_bytes()).hexdigest()
    row = dict(name=name, compile_exit=compile_rc, pytest_exit=rc, cmp_exit=cmp_rc,
               sha256=restored_sha, backup_sha256=backup_sha)
    records.append(row)
    print(json.dumps(row), flush=True)
    (logs/'mutations.json').write_text(json.dumps(records, indent=2)+'\n')
    assert rc == 1 and cmp_rc == 0 and restored_sha == backup_sha, row
    assert 'FAILED ' in text and 'ERROR collecting' not in text and 'ImportError' not in text, name
