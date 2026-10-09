"""四条定点变异：cp备份/还原、cmp逐字节确认、还原后全聚焦转绿。"""
from pathlib import Path
import hashlib
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
ENGINE = ROOT / 'backtesting-py'
OUT = Path('/tmp/10d')
OUT.mkdir(parents=True, exist_ok=True)
MUTATIONS = [
    ('decision_close', 'portfolio_runner.py', 'OpenPrices(bar.ts, bar.open_prices)',
     'OpenPrices(bar.ts, bars[i-1].close_prices)', 'test_three_period_handwritten_golden_next_raw_open'),
    ('buy_first', 'portfolio_ledger.py', 'for side in ("sell", "buy"):',
     'for side in ("buy", "sell"):', 'test_sell_before_buy_uses_same_period_proceeds'),
    ('ceil_qty', 'portfolio_ledger.py', 'units = (an * ud) // (ad * un)',
     'units = (an * ud + ad * un - 1) // (ad * un)', 'test_floor_remainder_and_minimum_reasons'),
    ('tolerant_nav', 'portfolio_ledger.py', 'if nav != Decimal(point["equity"]) or replay_nav != Fraction(point["equity"]):',
     'if abs(nav - Decimal(point["equity"])) > Decimal("0.0000001") or abs(replay_nav - Fraction(point["equity"])) > Fraction("0.0000001"):',
     'test_reconciliation_rejects_one_smallest_unit'),
]


def main():
    for name, filename, original, mutant, test in MUTATIONS:
        path = ENGINE / filename
        backup = OUT / (filename + '.backup')
        source = path.read_text()
        assert source.count(original) == 1, (name, 'mutation anchor changed')
        subprocess.run(['cp', str(path), str(backup)], check=True)
        try:
            path.write_text(source.replace(original, mutant))
            with (OUT / (name + '.log')).open('w') as log:
                result = subprocess.run([sys.executable, '-m', 'pytest', 'tests/test_portfolio_10d.py::'+test,
                                         '-q', '-p', 'no:cacheprovider'], cwd=ENGINE, stdout=log, stderr=subprocess.STDOUT)
            content = (OUT / (name + '.log')).read_text()
            print(f'{name}: EXIT={result.returncode}; ' + content.splitlines()[-1], flush=True)
            assert result.returncode == 1 and '1 failed' in content, 'mutation was not detected by an assertion'
        finally:
            subprocess.run(['cp', str(backup), str(path)], check=True)
            subprocess.run(['cmp', str(backup), str(path)], check=True)
            print(f'{name}: RESTORE_CMP=0 SHA256={hashlib.sha256(path.read_bytes()).hexdigest()}', flush=True)
    with (OUT / 'restored.log').open('w') as log:
        result = subprocess.run([sys.executable, '-m', 'pytest', 'tests/test_portfolio_10d.py', '-q', '-p', 'no:cacheprovider'],
                                cwd=ENGINE, stdout=log, stderr=subprocess.STDOUT)
    print(f'RESTORED EXIT={result.returncode}; ' + (OUT / 'restored.log').read_text().splitlines()[-1])
    return result.returncode


if __name__ == '__main__':
    sys.exit(main())
