"""Compile-valid red mutants; backups, finally restore, external cmp per mutation."""
from pathlib import Path
import hashlib
import json
import py_compile
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]
PROVIDER=ROOT/'backtesting-py/cutie_backtesting_provider.py'
SWING=ROOT/'backtesting-py/strategy_swing_points.py'
TEST='backtesting-py/tests/test_9t5_fibonacci.py'
LOG=Path('/tmp/9t5')
MUTANTS=[
    ('unconfirmed_high',SWING,'highs[confirmation] = SwingPoint(j, confirmation, high[j])',
        'highs[j] = SwingPoint(j, j, high[j])','test_high_must_be_confirmed_before_entry'),
    ('level_from_low',PROVIDER,'price = H - Decimal(str(level)) * (H - L)',
        'price = L + Decimal(str(level)) * (H - L)','test_high_must_be_confirmed_before_entry'),
    ('bullish_disabled',PROVIDER,'and self.data.Close[-1] > self.data.Open[-1]):',
        'and True):','test_bullish_close_required'),
    ('tolerance_reversed',PROVIDER,'<= price * (1 + tolerance)\n                    and Decimal(str(self.data.Close[-1])) >= price * (1 - tolerance)',
        '<= price * (1 - tolerance)\n                    and Decimal(str(self.data.Close[-1])) >= price * (1 + tolerance)',
        'test_tolerance_both_bounds'),
    ('786_wrong_stop',PROVIDER,'if level < 0.786 else L','if level <= 0.786 else L',
        'test_hand_calculated_levels_and_786_stop'),
    ('repeat_pair_entry',PROVIDER,'wave["used"] = True','wave["used"] = False','test_same_pair_cannot_enter_twice'),
    ('gap_target_guard_missing',PROVIDER,'if state.take_price is not None and opening >= state.take_price else None)',
        'if False else None)','test_gap_cancels_before_real_broker_fill'),
    ('float_gain_threshold',PROVIDER,'Decimal(str(hi.price)) / Decimal(str(low.price)) - 1 >= minimum',
        'hi.price / low.price - 1 >= float(minimum)','test_minimum_gain'),
    ('default_close_only',PROVIDER,'high=self.data.High[-1] if intrinsic else self.data.Close[-1]',
        'high=self.data.Close[-1]','test_same_pair_cannot_enter_twice'),
]


def main():
    LOG.mkdir(exist_ok=True)
    originals={path:path.read_bytes() for path in (PROVIDER,SWING)}
    backups={path:LOG/(path.name+'.9t5-original') for path in originals}
    for path,data in originals.items(): backups[path].write_bytes(data)
    results=[]
    try:
        for name,path,old,new,selector in MUTANTS:
            source=originals[path].decode()
            assert source.count(old)==1,(name,source.count(old))
            try:
                path.write_text(source.replace(old,new))
                py_compile.compile(str(path),doraise=True)
                with (LOG/('mutant-'+name+'.log')).open('w') as output:
                    result=subprocess.run([sys.executable,'-m','pytest',TEST,'-k',selector,'-q','-p','no:cacheprovider'],
                        cwd=ROOT,stdout=output,stderr=subprocess.STDOUT)
                    output.write(f'\nEXIT={result.returncode}\n')
                log=(LOG/('mutant-'+name+'.log')).read_text()
                assert result.returncode==1 and 'FAILED '+TEST+'::'+selector in log,(name,result.returncode)
                results.append(dict(name=name,compiled=True,exit=result.returncode,selector=selector))
            finally:
                path.write_bytes(originals[path])
                subprocess.run(['cmp',str(path),str(backups[path])],check=True)
                py_compile.compile(str(path),doraise=True)
            print(name,'compile=0 red=1 restored_cmp=0',flush=True)
    finally:
        for path,data in originals.items():
            path.write_bytes(data)
            subprocess.run(['cmp',str(path),str(backups[path])],check=True)
            py_compile.compile(str(path),doraise=True)
    for row in results: row['restored_cmp']=0
    (LOG/'mutations.json').write_text(json.dumps(dict(results=results,
        sha256={path.name:hashlib.sha256(data).hexdigest() for path,data in originals.items()}),indent=2)+'\n')
    print(f'9T5 MUTATIONS {len(results)}/{len(MUTANTS)} killed')


if __name__=='__main__': main()
