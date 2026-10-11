"""Capture the Wave-B isolated-margin OFF-state golden once, at each tool's own feature head.

Usage (never on the integration head):
  python3 capture_isolated_off_waveb.py part <backtesting-py dir of source worktree> <out.json> <tool> [<tool> ...]
  python3 capture_isolated_off_waveb.py merge <out.json> <part.json> [<part.json> ...]
Tests only read the merged fixture isolated_off_waveb_<sources>.json.
"""
import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import patch


# opening_range_breakout's default flatten 23:45 cuts through the 1h candle grid used by the OFF-state test.
TOOL_PARAMS = {'opening_range_breakout': {'flatten_at': '23:00'}}
# Range tools need the history to begin on a UTC day boundary (frame row 72 = 2026-01-04 00:00); others keep row 60.
DATA_OFFSET = {'opening_range_breakout': 72, 'asia_range_breakout': 72}


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def part(root, out, names):
    root = Path(root).resolve()
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / 'tests'))
    import tempfile
    import cutie_backtesting_provider as p
    from _exit_kinds_strip import without_exit_kinds
    import test_risk_overlay_compatibility as compat
    from backtesting import Backtest
    from canonical_json import canonical_json
    from fastapi.testclient import TestClient
    from test_leverage_params import request
    assert Path(p.__file__).resolve().parent == root, p.__file__
    full = compat.frame()
    step = 3600
    v2 = ('schema_version', 'trades', 'equity_curve', 'metrics', 'data_manifest')
    markets = {'futures': {}, 'futures_leverage_one': {'leverage': 1}, 'spot': {}}
    cases = {}
    for name in names:
        cases[name] = {}
        data = full.iloc[DATA_OFFSET.get(name, 60):].copy()
        for label, extra in markets.items():
            market = 'spot' if label == 'spot' else 'futures'
            tmp = Path(tempfile.mkdtemp())
            with patch.object(p, '_fetch_ohlcv', lambda *a, **k: data.copy()), \
                 patch.object(p, '_fetch_template_warmup', lambda *a, **k: __import__('pandas').DataFrame()), \
                 patch.object(p, 'REPORTS_DIR', tmp), patch.object(Backtest, 'plot', lambda *a, **k: None):
                req = request({**TOOL_PARAMS.get(name, {}), **extra}, market=market, name=name)
                req['backtest'].update(start_at=int(data.index[0].timestamp()),
                                       end_at=int(data.index[-1].timestamp()) + step)
                resp = TestClient(p.app).post('/cutie/backtest', json=req).json()
                resp = without_exit_kinds(resp)
            assert resp['result_status'] == 'success', (name, label, resp)
            assert 'isolated_margin' not in resp['assumptions'] and 'isolated_risk' not in resp['raw_report']
            cases[name][label] = dict(
                v2=digest(canonical_json({k: resp[k] for k in v2})),
                assumptions=digest(json.dumps(resp['assumptions'], sort_keys=True, separators=(',', ':'))),
                raw_report=digest(json.dumps(resp['raw_report'], sort_keys=True, separators=(',', ':'))))
        assert cases[name]['futures'] == cases[name]['futures_leverage_one'], name
        cases[name] = {'futures': cases[name]['futures'], 'spot': cases[name]['spot']}
    import subprocess
    sha = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    Path(out).write_text(json.dumps(dict(source_sha=sha, tool_params={n: TOOL_PARAMS[n] for n in cases if n in TOOL_PARAMS}, data_offset={n: DATA_OFFSET[n] for n in cases if n in DATA_OFFSET}, cases=cases), indent=2, sort_keys=True) + '\n')
    print('PART', sha[:7], len(cases))


def merge(out, parts):
    loaded = [json.loads(Path(x).read_text()) for x in parts]
    cases, sources, tool_params, offsets = {}, [], {}, {}
    for item in loaded:
        assert not set(cases) & set(item['cases']), 'duplicate tool'
        cases.update(item['cases'])
        tool_params.update(item['tool_params'])
        offsets.update(item['data_offset'])
        sources.append(item['source_sha'])
    Path(out).write_text(json.dumps(dict(source_shas=sources, tool_params=tool_params, data_offset=offsets, cases=cases), indent=2, sort_keys=True) + '\n')
    print('MERGED', len(cases))


if __name__ == '__main__':
    if sys.argv[1] == 'part':
        part(sys.argv[2], sys.argv[3], sys.argv[4:])
    else:
        merge(sys.argv[2], sys.argv[3:])
