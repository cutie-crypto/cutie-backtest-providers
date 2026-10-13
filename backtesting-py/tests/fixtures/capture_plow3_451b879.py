"""Capture once at 451b879 (before P-LOW3); tests never regenerate this immutable baseline.

P-LOW3 judges main-range gaps only while a same-timeframe entry filter is on. Baseline proof for the
states the change must not touch, all with a one-bar main-range gap (or a failure before any run):
- `off/<tool>`: filter off, main range missing one bar, contiguous warmup prefix (5 template families);
- `mtf/ema_cross`: filter_timeframe=4h, main range missing one bar;
- `fail/<kind>`: three other `_business_failure` callers (NO_DATA, RATE_LIMITED, SYMBOL_UNSUPPORTED).
Each case is the sorted-key JSON of the full HTTP body (report_path dropped).
Run from an export of 451b879: `python3 tests/fixtures/capture_plow3_451b879.py <out.json>`.
"""
from pathlib import Path
import json
import hashlib
import sys
import tempfile

import pandas as pd
import pytest
from backtesting import Backtest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import capture_plow2a_b48e65e as cap  # noqa: E402
import cutie_backtesting_provider as p  # noqa: E402
import test_risk_overlay_compatibility as compat  # noqa: E402
from _exit_kinds_strip import without_exit_kinds
from _complete_seed_fixture import extend_seed

BASELINE_SHA = '451b879'
GAP_AT = 180  # main-range bar removed for the one-bar gap
OFF_TOOLS = ['ema_cross', 'rsi_reversal', 'bollinger_breakout', 'double_bottom', 'bullish_engulfing']
FAILURES = {'no_data': ValueError('NO_DATA'), 'rate_limited': RuntimeError('RATE_LIMITED'),
            'symbol_unsupported': ValueError('symbol not supported')}


def gapped(freq='1h', drop=(GAP_AT,)):
    warm, data = cap.series(freq)
    return warm, data.drop(data.index[list(drop)])


def post(monkeypatch, tmp_path, tool, params, data, warm, source=None, fetch_error=None, timeframe='1h', *, complete=False):
    """POST one futures run; `_fetch_template_warmup` returns the tail(bars) of `warm`."""
    def fetch(ex, mk, sym, tf, since, until):
        if fetch_error is not None:
            raise fetch_error
        return (data if tf == timeframe else source).copy()

    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(p, '_fetch_ohlcv', fetch)
    def warmup(*args, **kwargs):
        seed = warm.tail(args[5]).copy() if args[5] > 0 else warm.iloc[:0].copy()
        return extend_seed(seed, args[3], args[5], args[6]) if complete else seed
    monkeypatch.setattr(p, '_fetch_template_warmup', warmup if complete else
                        lambda *a: warm.tail(a[5]).copy() if a[5] > 0 else warm.iloc[:0].copy())
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **kw: None)
    step = pd.Timedelta(timeframe.replace('m', 'min') if timeframe.endswith('m') else timeframe)
    request = dict(run_id='plow3', provider_tool_id='local.backtesting_py.' + tool, provider_params=params,
                   symbol='BTCUSDT', market='futures', timeframe=timeframe, start_at=int(data.index[0].timestamp()),
                   end_at=int((data.index[-1] + step).timestamp()), initial_capital='10000', fee_bps='0',
                   slippage_bps='0')
    body = TestClient(p.app).post('/cutie/backtest', json={'backtest': request}).json()
    if complete and body['result_status'] == 'success':
        digest = lambda value: hashlib.sha256(json.dumps(value, sort_keys=True,
            separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        frozen = json.loads(Path(__file__).with_name('warmup_b4_plow_financial.json').read_text())
        expected = [r['financial_sha256'] for r in frozen['rows']
                    if r['request_sha256'] == digest({'backtest': request})
                    and r['main_manifest_sha256'] == digest(body['data_manifest'])]
        actual = digest({k: body[k] for k in ('trades', 'equity_curve', 'metrics', 'data_manifest')})
        assert actual in expected, (tool, 'financial fields changed or unaudited request; stop')
    return body


def dump(body):
    body = without_exit_kinds(body)
    body.pop('report_path', None)
    return json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def cases():
    """case -> kwargs for post() (monkeypatch/tmp_path excluded)."""
    warm, data = gapped()
    out = {'off/' + tool: dict(tool=tool, params=cap.params_for(tool), data=data, warm=warm) for tool in OFF_TOOLS}
    full = compat.frame()
    full.index = pd.date_range('2026-01-01', periods=len(full), freq='1h')
    source = full.resample('4h').agg(dict(Open='first', High='max', Low='min', Close='last', Volume='sum'))
    out['mtf/ema_cross'] = dict(tool='ema_cross', params={**cap.params_for('ema_cross'), **cap.EMA10,
                                                          'filter_timeframe': '4h'},
                                data=data, warm=warm, source=source)
    for kind, error in FAILURES.items():
        out['fail/' + kind] = dict(tool='ema_cross', params={**cap.params_for('ema_cross'), **cap.EMA10},
                                   data=data, warm=warm, fetch_error=error)
    return out


def snapshot(monkeypatch, tmp_path, case):
    return dump(post(monkeypatch, tmp_path, **cases()[case]))


if __name__ == '__main__':
    values = {}
    for key in cases():
        with pytest.MonkeyPatch.context() as mp, tempfile.TemporaryDirectory() as reports:
            values[key] = snapshot(mp, Path(reports), key)
    Path(sys.argv[1]).write_text(json.dumps(dict(baseline_sha=BASELINE_SHA, cases=values), indent=2,
                                            ensure_ascii=False) + '\n')
    print('CAPTURED', len(values))
