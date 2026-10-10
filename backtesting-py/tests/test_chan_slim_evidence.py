"""CHANSLIM: raw_report.chan keeps only the decision evidence by default.

Default chan_3buy / chan_3sell responses drop the five intermediate traces (merged_bars,
merges, fractals, rejected_strokes, replacements), strokes and third-buy / third-sell
segments keep direction / high / low / confirmed_at, and centers stop copying their three
strokes. CUTIE_BACKTEST_CHAN_DEBUG=1 (local diagnosis only, never a tool parameter) restores
the full recognizer report. Trades, metrics, equity and every other raw_report key are
byte-identical between the two paths. Over-limit failures carry the real provenance.
"""
from pathlib import Path
from unittest.mock import patch
import json
import sys
import tempfile

import numpy as np
import pandas as pd
import pytest
from backtesting import Backtest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p
from test_9t6_chan_3buy import frame as long_frame
from test_short_pat3_chan_3sell import frame as short_frame, http_response, request

BUY = 'local.backtesting_py.chan_3buy'
SELL = 'local.backtesting_py.chan_3sell'
DEBUG_ENV = 'CUTIE_BACKTEST_CHAN_DEBUG'
INTERMEDIATE = {'merged_bars', 'merges', 'fractals', 'rejected_strokes', 'replacements'}
SEGMENT = {'direction', 'high', 'low', 'confirmed_at'}
CENTER = {'id', 'zg', 'zd', 'start_stroke', 'end_stroke', 'confirmed_at', 'used'}


def walk(bars, seed=7):
    """Seeded hourly random walk; enough strokes and centers for a long debug report."""
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.006, bars)))
    spread = np.abs(rng.normal(0, 0.004, bars)) * close
    frame = pd.DataFrame(dict(Open=np.r_[close[0], close[:-1]], High=close + spread, Low=close - spread,
        Close=close, Volume=100.0), index=pd.date_range('2025-01-01', periods=bars, freq='h'))
    frame['High'] = frame[['Open', 'High', 'Close']].max(axis=1)
    frame['Low'] = frame[['Open', 'Low', 'Close']].min(axis=1)
    return frame.astype(float)


CASES = [
    (BUY, 'spot', {}, long_frame, 0),
    (BUY, 'spot', {}, long_frame, 10),
    (BUY, 'futures', {'bi_mode': 'old'}, long_frame, 0),
    (BUY, 'futures', {'leverage': 3, 'position_size_pct': 10}, long_frame, 0),
    (BUY, 'spot', {}, lambda: walk(700), 0),
    (SELL, 'futures', {}, short_frame, 0),
    (SELL, 'futures', {}, short_frame, 10),
    (SELL, 'futures', {'leverage': 3, 'position_size_pct': 10}, short_frame, 0),
    (SELL, 'futures', {}, lambda: walk(700), 0),
]


def run(monkeypatch, tool, market, params, data, warm, debug):
    if debug:
        monkeypatch.setenv(DEBUG_ENV, '1')
    else:
        monkeypatch.delenv(DEBUG_ENV, raising=False)
    return http_response(params, data, warm, tool, market)


def project(chan, facts_key):
    """Independent restatement of the slim projection, applied to the debug report."""
    seg = lambda s: {k: s[k] for k in SEGMENT}
    out = {k: v for k, v in chan.items() if k not in INTERMEDIATE}
    out['strokes'] = [seg(s) for s in chan['strokes']]
    out['centers'] = [{k: c[k] for k in CENTER} for c in chan['centers']]
    out[facts_key] = [dict(f, departure=seg(f['departure']), pullback=seg(f['pullback'])) for f in chan[facts_key]]
    return out


def dumps(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


@pytest.mark.parametrize('tool,market,params,make,warm', CASES)
def test_default_chan_keys_pinned(tool, market, params, make, warm, monkeypatch):
    facts_key = 'third_sells' if tool == SELL else 'third_buys'
    chan = run(monkeypatch, tool, market, params, make(), warm, debug=False)['raw_report']['chan']
    assert set(chan) == {'index_basis', 'strokes', 'centers', facts_key, 'skipped_entries', 'entries', 'exits'}
    assert chan['strokes'] and (chan['centers'] or params.get('bi_mode') == 'old')
    assert all(set(s) == SEGMENT for s in chan['strokes'])
    assert all(set(c) == CENTER for c in chan['centers'])
    level, extreme = ('zd', 'pullback_high') if tool == SELL else ('zg', 'pullback_low')
    for fact in chan[facts_key]:
        assert set(fact) - {'frozen_stop'} == {'center', 'departure', 'pullback', 'confirmed_at', level, extreme, 'status'}
        assert ('frozen_stop' in fact) == (fact['status'] == 'signal')
        assert set(fact['departure']) == SEGMENT and set(fact['pullback']) == SEGMENT


@pytest.mark.parametrize('tool,market,params,make,warm', CASES)
def test_trades_and_everything_but_chan_identical_to_debug(tool, market, params, make, warm, monkeypatch):
    facts_key = 'third_sells' if tool == SELL else 'third_buys'
    slim = run(monkeypatch, tool, market, params, make(), warm, debug=False)
    full = run(monkeypatch, tool, market, params, make(), warm, debug=True)
    if not params.get('bi_mode') == 'old':
        assert slim['trades'] and slim['raw_report']['chan']['entries']
    assert set(slim) == set(full)
    for key in set(slim) - {'raw_report', 'provider_run_id', 'report_url'}:
        assert dumps(slim[key]) == dumps(full[key]), key
    assert set(slim['raw_report']) == set(full['raw_report'])
    for key in set(slim['raw_report']) - {'chan'}:
        assert dumps(slim['raw_report'][key]) == dumps(full['raw_report'][key]), key
    for key in ('entries', 'exits', 'skipped_entries', 'index_basis'):
        assert dumps(slim['raw_report']['chan'][key]) == dumps(full['raw_report']['chan'][key]), key
    assert dumps(slim['raw_report']['chan']) == dumps(project(full['raw_report']['chan'], facts_key))


@pytest.mark.parametrize('tool,market,params,make,warm', [CASES[0], CASES[4], CASES[5], CASES[8]])
def test_debug_switch_keeps_full_intermediate_traces(tool, market, params, make, warm, monkeypatch):
    chan = run(monkeypatch, tool, market, params, make(), warm, debug=True)['raw_report']['chan']
    assert INTERMEDIATE <= set(chan)
    assert chan['merged_bars'] and chan['fractals']
    assert all({'start', 'end'} <= set(s) for s in chan['strokes'])
    assert all(len(c['strokes']) == 3 for c in chan['centers'])


def test_debug_switch_is_not_a_tool_parameter():
    for tool in (BUY, SELL):
        assert 'chan_debug' not in p.TOOL_SPECS[tool]['param_schema_properties']
    assert 'chan_debug' not in json.dumps(TestClient(p.app).get('/catalog').json())
    body = request({'chan_debug': 1}, long_frame(), 'spot')
    body['backtest']['provider_tool_id'] = BUY
    with patch.object(p, 'AUTH_TOKEN', ''):
        result = TestClient(p.app).post('/cutie/backtest', json=body).json()
    assert result['result_status'] == 'failed' and 'chan_debug' in result['error_message']


PROVENANCE = {'provider_revision': 'chanslim-test', 'source': 'ccxt:okx(test-injected)',
              'central_market_data_used': False, 'auth_mode': 'none', 'cache_hit': True}


def test_over_limit_failure_keeps_fetched_provenance():
    body = {'metrics': {}, 'equity_curve': [], 'trades': [], 'assumptions': {}, 'limitations': {},
            'data_manifest': {}, 'raw_report': {'chan': {'pad': 'x' * (5 * 1024 * 1024)},
                                                'market_data_provenance': PROVENANCE}}
    result = json.loads(p._bounded_template_response('chanslim', body).body)
    assert result['result_status'] == 'failed' and 'raw_report evidence exceeds' in result['error_message']
    assert result['raw_report']['market_data_provenance'] == PROVENANCE
    assert result['raw_report']['market_data_provenance']['source'] is not None


def test_over_limit_http_failure_reports_real_source(monkeypatch):
    # The debug report of a long run is far beyond the callback limit; the fetch did happen.
    monkeypatch.setenv(DEBUG_ENV, '1')
    data = walk(20000)
    data.attrs.update(cutie_data_source='ccxt:binanceus(test-injected)', cutie_central_market_data_used=False,
                      cutie_market_data_cache_hit=True)
    body = request({}, data, 'spot')
    body['backtest']['provider_tool_id'] = BUY
    with tempfile.TemporaryDirectory(prefix='chanslim-') as reports, \
         patch.object(p, 'AUTH_TOKEN', ''), patch.object(p, 'REPORTS_DIR', Path(reports)), \
         patch.object(p, '_fetch_ohlcv', lambda *a: data.copy()), \
         patch.object(p, '_fetch_template_warmup', lambda *a: data.iloc[:0].copy()), \
         patch.object(Backtest, 'plot', return_value=None):
        result = TestClient(p.app).post('/cutie/backtest', json=body).json()
    assert result['result_status'] == 'failed', result.get('error_message')
    assert 'raw_report evidence exceeds' in result['error_message']
    provenance = result['raw_report']['market_data_provenance']
    assert provenance['source'] == 'ccxt:binanceus(test-injected)'
    assert provenance['cache_hit'] is True and 'central_market_data_attempted' not in provenance
