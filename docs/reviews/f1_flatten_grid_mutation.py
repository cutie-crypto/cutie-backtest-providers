"""Remove only the flatten check, require assertion failures, restore and compare."""
from pathlib import Path
import json
import shutil
import subprocess
import sys
root = Path.cwd()
logs = Path('/tmp/f1fix')
logs.mkdir(exist_ok=True)
source = root/'backtesting-py/strategy_range_breakout.py'
backup = logs/'range.backup'
shutil.copyfile(source, backup)
old = "            if (entry_window.end_utc-first) % step:\n                raise ValueError('INVALID_PARAMS:flatten endpoint cuts through a candle')"
assert source.read_text().count(old) == 1
try:
    source.write_text(source.read_text().replace(old, ''))
    subprocess.run([sys.executable, '-m', 'py_compile', str(source)], check=True)
    with (logs/'mutation.log').open('w') as output:
        rc = subprocess.run([sys.executable, '-m', 'pytest', 'backtesting-py/tests/test_f1_range_breakout.py',
            '-k', 'flatten_grid_pre_fetch', '-q', '-p', 'no:cacheprovider'], stdout=output, stderr=subprocess.STDOUT).returncode
finally:
    shutil.copyfile(backup, source)
    cmp = subprocess.run(['cmp', str(backup), str(source)]).returncode
report = dict(pytest_exit=rc, cmp_exit=cmp)
(logs/'mutation.json').write_text(json.dumps(report, indent=2))
print(report)
assert rc == 1 and cmp == 0
assert '2 failed, 2 passed' in (logs/'mutation.log').read_text()
