"""Reproducible F5 mutants; each compiles, goes red, and restores byte-for-byte."""
from pathlib import Path
import hashlib
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
P = ROOT / 'backtesting-py/cutie_backtesting_provider.py'
S = ROOT / 'backtesting-py/strategy_time_series.py'
TEST = 'backtesting-py/tests/test_f5_vwap_reversion.py'
LOG = Path('/tmp/f5')
MUTANTS = [
    ('previous_bar_vwap', P, 'self._f5_vwap = np.asarray(values[-len(main_index):])',
     'self._f5_vwap = np.roll(np.asarray(values[-len(main_index):]), 1)', 'test_vwap_includes_current_bar_changes_entry'),
    ('no_daily_reset', S, '        if cycle != active:\n            active = cycle\n            numerator = denominator = 0.0',
     '        if cycle != active:\n            active = cycle', 'test_two_day_hand_vwap_and_next_open_fills'),
    ('high_touch_exit', P, 'if self.data.Close[-1] >= value:', 'if self.data.High[-1] >= value:',
     'test_high_touch_does_not_close_before_reversion'),
    ('daily_flatten_late', P, 'entry_utc=trade.entry_time, bar_open=self.data.index[-1], context=self._f5_clock)',
     'entry_utc=trade.entry_time + timedelta(days=1), bar_open=self.data.index[-1], context=self._f5_clock)',
     'test_two_day_hand_vwap_and_next_open_fills'),
    ('daily_timeframe_allowed', P, 'if vwap_step_ms >= 86400000 or 86400000 % vwap_step_ms:',
     'if vwap_step_ms > 86400000 or 86400000 % vwap_step_ms:', 'test_non_intraday_before_fetch'),
    ('negative_deviation_allowed', P,
     '"vwap_deviation_pct": {"type": "number", "default": 1.5, "minimum": 0.5, "maximum": 5}',
     '"vwap_deviation_pct": {"type": "number", "default": 1.5, "minimum": -5, "maximum": 5}',
     'test_invalid_parameters_before_fetch'),
    ('close_source_ignored', P, 'backtest_start=main_index[0], price_source=price_source)',
     'backtest_start=main_index[0], price_source="hlc3")', 'test_close_price_source_independent_hand_values'),
    ('entry_equality_rejected', P, 'if self.data.Close[-1] > value * (1 - deviation / 100):',
     'if self.data.Close[-1] >= value * (1 - deviation / 100):', 'test_entry_threshold_is_inclusive'),
    ('gap_stop_allowed', P, 'if stop is not None and Decimal(str(self.data.Open[-1])) <= stop:',
     'if False and stop is not None and Decimal(str(self.data.Open[-1])) <= stop:',
     'test_gap_cancelled_before_entry_and_disclosed'),
    ('partial_day_accepted', P, 'if not complete_prefix:', 'if False and not complete_prefix:',
     'test_partial_opening_day_fail_closed_and_next_day_works'),
]


def main():
    LOG.mkdir(exist_ok=True)
    originals={path:path.read_bytes() for path in (P,S)}
    backups={path:LOG/(path.name+'.f5-original') for path in originals}
    for path, value in originals.items():
        backups[path].write_bytes(value)
    rows=[]
    for name,path,before,after,target in MUTANTS:
        original=originals[path]
        source=original.decode()
        assert source.count(before)==1,(name,source.count(before))
        try:
            path.write_text(source.replace(before,after))
            compile_result=subprocess.run([sys.executable,'-m','py_compile',str(path)],cwd=ROOT,
                capture_output=True,text=True)
            assert compile_result.returncode==0,compile_result.stderr
            with (LOG/(name+'.log')).open('w') as output:
                result=subprocess.run([sys.executable,'-m','pytest',TEST,'-q','-p','no:cacheprovider','-k',target],
                    cwd=ROOT,stdout=output,stderr=subprocess.STDOUT)
            assert result.returncode==1,(name,result.returncode)
            rows.append(f'{name}: compile EXIT=0; pytest EXIT={result.returncode}; red test={target}')
        finally:
            path.write_bytes(original)
            restored=subprocess.run(['cmp',str(path),str(backups[path])],capture_output=True)
            assert restored.returncode==0,(name,'restore failed')
            print(f'{name}: cmp EXIT=0',flush=True)
    for path,value in originals.items():
        assert path.read_bytes()==value
        rows.append(f'{path.name}: restored SHA256={hashlib.sha256(value).hexdigest()}')
    (LOG/'mutations-summary.txt').write_text('\n'.join(rows)+'\n')
    print('\n'.join(rows))


if __name__=='__main__':
    main()
