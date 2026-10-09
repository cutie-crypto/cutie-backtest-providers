"""Frozen input and capture helper; run once at 11e8cfb, never from tests."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as provider
import test_risk_overlay_compatibility as compat
from backtesting import Backtest
from canonical_json import canonical_json
from fastapi.testclient import TestClient

LEDGER_PARAMS = {
    'rsi_scale_in_out': dict(rsi_period=5, oversold=40, overbought=60, buy_notional=100, sell_notional=100),
    'grid': dict(lower_price=80, upper_price=140, grid_count=10, amount_per_grid=100),
    'dca': dict(amount=100, interval='daily', profit_target_pct=5),
}
V2_KEYS = ('schema_version', 'trades', 'equity_curve', 'metrics', 'data_manifest')


def request_body(name, params):
    data = compat.frame().iloc[60:]
    return {'backtest': dict(run_id='time_compat', provider_tool_id='local.backtesting_py.' + name,
        provider_params=params, symbol='BTCUSDT', market=('spot' if name in LEDGER_PARAMS else 'futures'), timeframe='1h',
        start_at=int(data.index[0].timestamp()), end_at=int(data.index[-1].timestamp()) + 3600,
        initial_capital='10000', fee_bps='10', slippage_bps='5')}


def response(name, params, warm=True):
    if name in ('bullish_engulfing', 'hammer_pin_bar'):
        import pandas as pd
        frozen = json.loads(Path(__file__).with_name('9t1_candle_off.json').read_text())['inputs'][name]
        full = pd.DataFrame(frozen['columns'], index=pd.to_datetime(frozen['index']))
    elif name in ('morning_star', 'three_white_soldiers', 'bullish_doji_reversal', 'inside_bar_breakout'):
        import pandas as pd
        frozen = json.loads(Path(__file__).with_name('9t2_candle_off.json').read_text())['inputs'][name]
        full = pd.DataFrame(frozen['columns'], index=pd.to_datetime(frozen['index']))
    elif name in ('double_bottom', 'inverse_head_shoulders'):
        import pandas as pd
        frozen = json.loads(Path(__file__).with_name('9t3_bottom_off.json').read_text())['inputs'][name]
        full = pd.DataFrame(frozen['columns'], index=pd.to_datetime(frozen['index']))
    else:
        full = compat.frame()
    def fetch(*args):
        return full.iloc[60:].copy()
    def warmup(*args):
        return full.iloc[:60].copy() if warm else full.iloc[:0].copy()
    with tempfile.TemporaryDirectory(prefix='time-proof-') as reports, \
         patch.object(provider, 'AUTH_TOKEN', ''), \
         patch.object(provider, 'REPORTS_DIR', Path(reports)), \
         patch.object(provider, '_fetch_ohlcv', fetch), \
         patch.object(provider, '_fetch_template_warmup', warmup), \
         patch.object(Backtest, 'plot', return_value=None):
        res = TestClient(provider.app).post('/cutie/backtest', json=request_body(name, params))
        assert res.status_code == 200, res.text
        body = res.json()
        assert body['result_status'] == 'success', body
        return body


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def ledger_fingerprint(name):
    body = response(name, LEDGER_PARAMS[name])
    return dict(trade_count=len(body['trades']), trades_sha256=digest(body['trades']),
        equity_sha256=digest(body['equity_curve']),
        result_v2_sha256=hashlib.sha256(canonical_json({k: body[k] for k in V2_KEYS}).encode()).hexdigest(),
        min_bars=provider.TOOL_SPECS['local.backtesting_py.' + name]['build'](LEDGER_PARAMS[name])['min_bars'],
        warmup_bars=body['assumptions']['indicator_warmup_bars'],
        assumptions_sha256=digest(body['assumptions']), raw_report_sha256=digest(body['raw_report']))


if __name__ == '__main__':
    sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    assert sha == '11e8cfbfccb68776eeb755aa3ad1296bf4034bce', sha
    cases = {name: ledger_fingerprint(name) for name in LEDGER_PARAMS}
    assert all(case['trade_count'] > 0 for case in cases.values())
    single = {}
    for name in compat.enumerate_mixin_cases():
        params = {'direction': 'short'} if name.endswith('_short') else {}
        single[name] = dict(min_bars=provider.TOOL_SPECS['local.backtesting_py.' + compat.tool_name(name)]['build'](params)['min_bars'])
        for warm in (False, True):
            body = response(compat.tool_name(name), params, warm)
            single[name][str(int(warm))] = dict(warmup_bars=body['assumptions']['indicator_warmup_bars'],
                assumptions_sha256=digest(body['assumptions']), raw_report_sha256=digest(body['raw_report']))
    Path(__file__).with_name('time_layer_ledger_11e8cfb.json').write_text(
        json.dumps(dict(baseline_sha=sha, ledger=cases, single=single), indent=2) + '\n')
    print(f'CAPTURED ledger={len(cases)} single_metadata={len(single)}')
