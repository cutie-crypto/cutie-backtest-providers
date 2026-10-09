"""Explicit six-mutation gate; never collected by pytest. Restores and cmp-checks each mutation.

Run from backtesting-py: python3 tests/mutate_turtle_3b.py /tmp/tt3
"""
from pathlib import Path
import os
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'cutie_backtesting_provider.py'
LOGS = Path(sys.argv[1] if len(sys.argv)>1 else '/tmp/tt3')
LOGS.mkdir(parents=True,exist_ok=True)
BACKUP = LOGS / 'provider-before-mutations.py'
shutil.copy2(SOURCE,BACKUP)
original = BACKUP.read_text()


def replace_once(source,old,new):
    assert source.count(old)==1,(old,source.count(old))
    return source.replace(old,new,1)


def reset_clock(s):
    return replace_once(s,'if self._group_units == 0:\n                    self._group_entry_bar = self.trades[0].entry_bar',
        'if True:\n                    self._group_entry_bar = self.trades[-1].entry_bar')


def last_fill_take(s):
    return replace_once(s,'self._group_take = vwap * (1 + self._group_side * pct)',
        'self._group_take = Decimal(str(self.trades[-1].entry_price)) * (1 + self._group_side * pct)')


def take_before_expiry(s):
    start = s.index('                if due.due:',s.index('class _TurtleGroupMixin:'))
    middle = s.index('                if self._group_take',start)
    end = s.index('            if (price < self.exit_low',middle)
    return s[:start]+s[middle:end]+s[start:middle]+s[end:]


def allow_trailing(s):
    s = replace_once(s,'set(_FIXED_RISK_PARAM_SCHEMA_PROPERTIES) - set(_TURTLE_RISK_KEYS)',
        'set(_FIXED_RISK_PARAM_SCHEMA_PROPERTIES) - (set(_TURTLE_RISK_KEYS) | {"trailing_stop_pct"})')
    return replace_once(s,'**{key: dict(_FIXED_RISK_PARAM_SCHEMA_PROPERTIES[key]) for key in _TURTLE_RISK_KEYS},',
        '**{key: dict(_FIXED_RISK_PARAM_SCHEMA_PROPERTIES[key]) for key in (*_TURTLE_RISK_KEYS, "trailing_stop_pct")},')


def reject_after_fetch(s):
    s = replace_once(s,'if schema_err:  # F2:',
        'if schema_err and tool_spec.get("runner") != TURTLE_RUNNER:  # F2:')
    s = replace_once(s,'    risk = _parse_turtle_risk_params(params)\n    properties',
        '    risk = {key:params[key] for key in _TURTLE_RISK_KEYS if key in params}\n    properties')
    start = s.index('def _build_turtle(')
    pos = s.index('    error = _validate_params_against_schema(params, properties)',start)
    s = s[:pos]+s[pos:].replace('    error = _validate_params_against_schema(params, properties)',
        '    error = _validate_params_against_schema({k:v for k,v in params.items() if k in properties}, properties)',1)
    return replace_once(s,'        df = _fetch_ohlcv(exchange_id, market, symbol, timeframe, start_at, end_at)',
        '        df = _fetch_ohlcv(exchange_id, market, symbol, timeframe, start_at, end_at)\n'
        '        if tool_spec.get("runner") == TURTLE_RUNNER:\n'
        '            _parse_turtle_risk_params(params)')


def allow_reentry(s):
    return replace_once(s,'return True  # No new group on the exit-fill bar either.',
        'return False  # Mutation: exit fill permits a new group.')


CASES = [
    ('reset_clock',reset_clock,'test_add_does_not_reset_group_expiry_and_due_prevents_add'),
    ('last_fill_take',last_fill_take,'test_take_profit_scalar_vwap_and_next_open'),
    ('take_before_expiry',take_before_expiry,'test_exit_arbitration'),
    ('allow_trailing',allow_trailing,'test_conflicting_key_rejected_before_fetch_even_default[trailing_stop_pct]'),
    ('reject_after_fetch',reject_after_fetch,'test_conflicting_key_rejected_before_fetch_even_default'),
    ('allow_reentry',allow_reentry,'test_expiry_trigger_and_fill_bars_block_reentry'),
]


def run(label,selector):
    path = LOGS/(label+'.log')
    # Distinct cache directories prevent same-second, same-size source edits reusing bytecode.
    env = dict(os.environ,PYTHONPYCACHEPREFIX=str(LOGS/('cache-'+label)))
    with path.open('w') as output:
        result = subprocess.run([sys.executable,'-m','pytest','tests/test_turtle_risk_3b.py::'+selector,
            '-q','-p','no:cacheprovider'],cwd=ROOT,env=env,stdout=output,stderr=subprocess.STDOUT)
    return result.returncode,path.read_text()


for label,mutator,selector in CASES:
    try:
        mutant = mutator(original)
        compile(mutant,str(SOURCE),'exec')
        SOURCE.write_text(mutant)
        red,output = run('mutation-'+label,selector)
        failures = re.findall(r'^FAILED (.+)$',output,re.M)
        assert red==1 and failures,(label,red,output[-4000:])
    finally:
        shutil.copyfile(BACKUP,SOURCE)
    green,output = run('restored-'+label,selector)
    assert green==0,(label,output[-4000:])
    subprocess.run(['cmp',str(SOURCE),str(BACKUP)],check=True)
    summary = re.findall(r'^.*\b\d+ passed.*$',output,re.M)[-1]
    print(f'{label}: RED EXIT={red} {failures[0]}; GREEN EXIT={green} {summary}; cmp=0',flush=True)
assert SOURCE.read_bytes()==BACKUP.read_bytes()
