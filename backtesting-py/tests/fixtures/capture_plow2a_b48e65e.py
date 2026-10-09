"""Capture once at b48e65e (before P-LOW2a); tests never regenerate this immutable baseline.

P-LOW2a extends the same-timeframe filter warmup continuity guard from F1/F2 to every other
template that supports entry filters. Baseline proof for the two states the guard must not touch:
- `on/<tool>`: same-timeframe EMA filter on, warmup prefix contiguous and adjacent to the main range;
- `off/<tool>`: filter off, warmup prefix with a missing bar (best-effort warmup, never judged).
Each case is the sorted-key JSON of the full HTTP body (report_path dropped). Template list comes from
the runtime `_supports_entry_filters` enumeration minus the three P-LOW1 tools; params reuse the
risk-overlay compatibility table (`test_risk_overlay_compatibility.PARAMS`), defaults otherwise.
"""
from pathlib import Path
import json
import subprocess
import sys

import pandas as pd
import pytest
from backtesting import Backtest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import cutie_backtesting_provider as p  # noqa: E402
import test_risk_overlay_compatibility as compat  # noqa: E402

BASELINE_SHA = 'b48e65e'
PLOW1_TOOLS = {'opening_range_breakout', 'asia_range_breakout', 'calendar_schedule'}
EMA10 = dict(filter_layer_enabled=True, filter_ema_enabled=True, filter_ema_period=10)
PREFIX_BARS = 60
GAP_AT = -2  # bar removed from the gapped prefix: inside every tail(bars) with bars >= 2
# compat.PARAMS rows that later validation tightened (ema_period >= 50, lookback >= 10): use defaults.
DEFAULT_PARAMS = {'ema_rsi_pullback', 'volume_breakout'}
# default direction "both" is rejected with a filter on; pin long for both states.
LONG = {'cci_rsi', 'cme_weekend_gap', 'us_open_momentum'}
# US open boundaries (13:30 UTC) need a sub-hour grid.
TIMEFRAME = {'us_open_momentum': '30min'}


def tools():
    return sorted(k.removeprefix('local.backtesting_py.') for k, v in p.TOOL_SPECS.items()
                  if getattr(v.get('build'), '_supports_entry_filters', False)
                  and k.removeprefix('local.backtesting_py.') not in PLOW1_TOOLS)


def params_for(tool):
    params = {} if tool in DEFAULT_PARAMS else dict(compat.PARAMS.get(tool, {}))
    return {**params, 'direction': 'long'} if tool in LONG else params


def series(freq='1h'):
    """compat.frame() values (420 bars, re-indexed at `freq`); first 60 are the warmup prefix."""
    data = compat.frame()
    data.index = pd.date_range('2026-01-01', periods=len(data), freq=freq)
    return data.iloc[:PREFIX_BARS], data.iloc[PREFIX_BARS:].copy()


def prefix(kind, freq='1h'):
    full, _ = series(freq)
    if kind == 'contiguous':
        return full
    assert kind == 'gap'
    return full.drop(full.index[GAP_AT])


def post(monkeypatch, tmp_path, tool, params, kind, source=None):
    """POST one futures run; `_fetch_template_warmup` returns the tail(bars) of the chosen prefix."""
    freq = TIMEFRAME.get(tool, '1h')
    timeframe = {'1h': '1h', '30min': '30m'}[freq]
    _, data = series(freq)
    warm = prefix(kind, freq)
    calls = []

    def warmup(*args):
        calls.append(args[5])
        return warm.tail(args[5]).copy() if args[5] > 0 else warm.iloc[:0].copy()

    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda ex, mk, sym, tf, since, until:
                        (data if tf == timeframe else source).copy())
    monkeypatch.setattr(p, '_fetch_template_warmup', warmup)
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **kw: None)
    request = dict(run_id='plow2a', provider_tool_id='local.backtesting_py.' + tool, provider_params=params,
                   symbol='BTCUSDT', market='futures', timeframe=timeframe, start_at=int(data.index[0].timestamp()),
                   end_at=int((data.index[-1] + pd.Timedelta(freq)).timestamp()), initial_capital='10000', fee_bps='0',
                   slippage_bps='0')
    return TestClient(p.app).post('/cutie/backtest', json={'backtest': request}).json(), calls


def cases():
    """case -> (tool, params, prefix kind)."""
    out = {}
    for tool in tools():
        out['on/' + tool] = (tool, {**params_for(tool), **EMA10}, 'contiguous')
        out['off/' + tool] = (tool, params_for(tool), 'gap')
    return out


def snapshot(monkeypatch, tmp_path, tool, params, kind):
    body, _ = post(monkeypatch, tmp_path, tool, params, kind)
    body.pop('report_path', None)
    return json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


if __name__ == '__main__':
    import tempfile
    sha = subprocess.check_output(['git', 'rev-parse', '--short=7', 'HEAD'], text=True).strip()
    assert sha == BASELINE_SHA, sha
    values = {}
    for key, case in cases().items():
        with pytest.MonkeyPatch.context() as mp, tempfile.TemporaryDirectory() as reports:
            values[key] = snapshot(mp, Path(reports), *case)
    Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).with_name('plow2a_b48e65e.json')).write_text(
        json.dumps(dict(baseline_sha=sha, cases=values), indent=2, ensure_ascii=False) + '\n')
    print('CAPTURED', len(values))
