"""一次性读取 C1 分支真实 validator 及其纯 Python 依赖；不在 TokenBeep 落文件。"""
import importlib
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
BRANCH = 'feat/1009-10c1-v4-contract'


def main():
    repo = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT.parent.parent / 'TokenBeep'
    with tempfile.TemporaryDirectory(prefix='10d-c1-') as folder:
        target = Path(folder)
        for rel in ('services/portfolio_result_v4.py', 'services/strategy_spec_v3_builder.py', 'utils/canonical_json.py'):
            path = target / rel
            path.parent.mkdir(exist_ok=True)
            (path.parent / '__init__.py').touch()
            path.write_bytes(subprocess.check_output(['git', '-C', str(repo), 'show', BRANCH+':cutie-server/'+rel]))
        sys.path.insert(0, str(target))
        validator = importlib.import_module('services.portfolio_result_v4').validate_result_v4
        fixture = ROOT / 'backtesting-py/tests/fixtures/portfolio_10d_golden.json'
        result = validator(json.loads(fixture.read_text()))
        print(f'validate_result_v4(golden) = {result!r}')
        if result is not None:
            return 1
        # 同时校验生产runner生成的非null BTC.D 引用，避免只过手写文件。
        sys.path.insert(0, str(ROOT / 'backtesting-py/tests'))
        from test_portfolio_10d import golden_run, T0, DAY
        from canonical_json import canonical_json_sha256
        series = {'metric': 'btc_dominance', 'source': 'coingecko', 'unit': 'percent',
                  'points': [{'ts': T0+i*DAY, 'available_at': T0+i*DAY, 'value': '60'} for i in range(-20, 4)]}
        series['hash'] = canonical_json_sha256(series)
        result = validator(golden_run(metric_series=series).result)
        print(f'validate_result_v4(runner_with_btc_d) = {result!r}')
        if result is not None:
            return 1
        # 额外记录 C1 默认 28 位上下文与声明的 decimal128 上下文差异。
        probe = deepcopy(json.loads(fixture.read_text()))
        capital = '1234567890123456789012345678901234'
        probe['fills'] = []
        probe['snapshots'] = probe['snapshots'][:1]
        probe['snapshots'][0]['cash'] = capital
        probe['equity_curve'] = [{'ts': T0, 'equity': capital}]
        probe['btc_benchmark'] = [{'ts': T0, 'close_price': '100', 'equity': capital}]
        probe['metrics'] = {'total_return': '0', 'max_drawdown': '0', 'fill_count': '0'}
        for manifest in probe['input_manifests']['prices']:
            manifest['end_at'] = T0
            manifest['kline_count'] = 1
        print(f'C1 34-digit payload/default context = {validator(probe)!r}')
        from services.strategy_spec_v3_builder import decimal128
        with decimal128(exact=True):
            print(f'C1 34-digit payload/decimal128 context = {validator(probe)!r}')
        return 0


if __name__ == '__main__':
    sys.exit(main())
