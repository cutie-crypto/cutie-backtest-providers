"""7-P3b: red_streak_rsi joins the single-position entry filter layer.

Judgment bar = the exact Nth red close (the bar where red-count and RSI both hold).
Filter false there => signal discarded (no replay); true => market entry at the next open.
Off-state (no filter_* keys, or explicit defaults) stays byte-identical to c72c4a1.
"""
from __future__ import annotations
import json
from pathlib import Path
import sys

import pandas as pd
import pytest
from backtesting import Backtest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent / 'fixtures'))
import cutie_backtesting_provider as p
import capture_7p3b_off_c72c4a1 as off
from strategy_entry_filters import FILTER_PARAM_SCHEMA_PROPERTIES

TOOL = 'local.backtesting_py.red_streak_rsi'
GOLDEN = json.loads((Path(__file__).parent / 'fixtures/7p3b_off_c72c4a1.json').read_text())
DEFAULTS = {key: value['default'] for key, value in FILTER_PARAM_SCHEMA_PROPERTIES.items()}
EMA2 = dict(filter_layer_enabled=True, filter_ema_enabled=True, filter_ema_period=2)


def ema(closes, period):
    """Independent scalar EMA (adjust=False): e0 = c0, e = a*c + (1-a)*e."""
    alpha, out = 2 / (period + 1), []
    for close in closes:
        out.append(close if not out else alpha * close + (1 - alpha) * out[-1])
    return out


def pass_frame():
    """Hand frame with bar 43 closing at 95 (open 96, still red): close > EMA2 there."""
    data = off.frame()
    data.loc[data.index[43], ['Open', 'Close', 'High', 'Low']] = [96., 95., 96.1, 94.9]
    return data


def run(params, data):
    cls = p.TOOL_SPECS[TOOL]['build'](params)['strategy']
    return Backtest(data, cls, cash=100000, exclusive_orders=True, finalize_trades=False).run()['_trades']


def post(monkeypatch, tmp_path, params, data, prefix, requested=None):
    def warmup(*args):
        if requested is not None:
            requested.append(args[5])
        return prefix
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a: data)
    monkeypatch.setattr(p, '_fetch_template_warmup', warmup)
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **kw: None)
    request = dict(run_id='7p3b', provider_tool_id=TOOL, provider_params=params, symbol='BTCUSDT',
        market='spot', timeframe='1h', start_at=int(data.index[0].timestamp()),
        end_at=int(data.index[-1].timestamp()) + 3600, initial_capital='10000', fee_bps='0', slippage_bps='0')
    return TestClient(p.app).post('/cutie/backtest', json={'backtest': request}).json()


# ---- off-state: byte-identical to c72c4a1 ----

@pytest.mark.parametrize('case', sorted(off.cases()))
@pytest.mark.parametrize('explicit_defaults', [False, True])
def test_off_state_byte_identical_to_main(case, explicit_defaults, monkeypatch):
    params, data_name, warm = off.cases()[case]
    if explicit_defaults:
        params = {**params, **DEFAULTS}
    def forbidden(*args, **kwargs):
        pytest.fail('disabled filter performed indicator work')
    monkeypatch.setattr(p, 'entry_mask', forbidden)
    assert off.snapshot(params, data_name, warm) == GOLDEN['cases'][case]


def test_golden_covers_every_capture_case():
    assert GOLDEN['baseline_sha'] == 'c72c4a1'
    assert set(GOLDEN['cases']) == set(off.cases())


# ---- on-state: hand-computed judgment bar ----

def test_hand_filter_false_at_judgment_bar_discards_signal():
    data = off.frame()
    # Without the filter the Nth red close (bar 43) enters at the bar-44 open.
    assert run({}, data).EntryBar.tolist() == [44]
    # EMA2 at 43: 98 -> 98.667, 96 -> 96.889, 94 -> 94.963, 92 -> 92.988; 92 > 92.988 is false.
    assert not data.Close.iloc[43] > ema(data.Close.tolist(), 2)[43]
    assert run(EMA2, data).empty


def test_hand_filter_true_at_judgment_bar_enters_next_open():
    data = pass_frame()
    # EMA2 at 43 = 2/3*95 + 94.963/3 = 94.988 < 95, so the filter allows bar 43.
    assert data.Close.iloc[43] > ema(data.Close.tolist(), 2)[43]
    trades = run(EMA2, data)
    assert trades.EntryBar.tolist() == [44]
    assert trades.EntryPrice.tolist() == [93.]
    assert trades[['EntryBar', 'ExitBar']].values.tolist() == run({}, data)[['EntryBar', 'ExitBar']].values.tolist()


def test_filter_is_queried_only_on_judgment_bar(monkeypatch):
    seen = []
    original = p._FilterLayerMixin._filter_allow_entry
    def observe(self):
        seen.append(len(self.data) - 1)
        return original(self)
    monkeypatch.setattr(p._FilterLayerMixin, '_filter_allow_entry', observe)
    run(EMA2, off.frame())
    assert seen == [43]


def test_short_direction_is_not_offered():
    properties = p.TOOL_SPECS[TOOL]['param_schema_properties']
    assert 'direction' not in properties
    with pytest.raises(ValueError, match='long direction only'):
        p.TOOL_SPECS[TOOL]['build']({**EMA2, 'direction': 'short'})


# ---- warmup: filter needs more bars than the template ----

def ramp_prefix(count):
    closes = [40. + 0.6 * i for i in range(count)]
    index = pd.date_range(end=off.frame().index[0] - pd.Timedelta(hours=1), periods=count, freq='h')
    return pd.DataFrame(dict(Open=[c - .2 for c in closes], Close=closes, High=[c + .1 for c in closes],
        Low=[c - .3 for c in closes], Volume=[1] * count), index=index)


def test_filter_warmup_exceeding_template_warmup_uses_full_prefix(monkeypatch, tmp_path):
    params = dict(filter_layer_enabled=True, filter_ema_enabled=True, filter_ema_period=100)
    data, prefix = off.frame(), ramp_prefix(100)
    min_bars = p.TOOL_SPECS[TOOL]['build']({})['min_bars']
    assert min_bars == 43 < 100
    full = prefix.Close.tolist() + data.Close.tolist()
    # EMA100 over ramp 40..99.4 + main is ~88.74 at main bar 43; main-only EMA100 is ~99.61 > 92.
    assert data.Close.iloc[43] > ema(full, 100)[100 + 43]
    assert not data.Close.iloc[43] > ema(data.Close.tolist(), 100)[43]
    requested = []
    body = post(monkeypatch, tmp_path, params, data, prefix, requested)
    assert body['result_status'] == 'success', body
    assert requested == [100]
    assert body['assumptions']['indicator_warmup_bars'] == 100
    assert [t['opened_at'] for t in body['trades']] == [int(data.index[44].timestamp())]


def test_filter_warmup_short_history_fails_closed(monkeypatch, tmp_path):
    params = dict(filter_layer_enabled=True, filter_ema_enabled=True, filter_ema_period=100)
    body = post(monkeypatch, tmp_path, params, off.frame(), ramp_prefix(20))
    assert body['result_status'] != 'success'
    assert 'filter_history_insufficient' in json.dumps(body)


# ---- catalog ----

def test_catalog_publishes_filter_keys_and_unwired_list():
    properties = p.TOOL_SPECS[TOOL]['param_schema_properties']
    assert set(FILTER_PARAM_SCHEMA_PROPERTIES) <= set(properties)
    assert TOOL not in p.FILTER_LAYER_UNWIRED_TOOLS
    assert getattr(p.TOOL_SPECS[TOOL]['build'], '_supports_entry_filters', False)


def test_http_catalog_lists_filter_keys(monkeypatch):
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    body = TestClient(p.app).get('/catalog').json()
    tools = body.get('tools', body) if isinstance(body, dict) else body
    tool = next(t for t in tools if t.get('tool_id', t.get('provider_tool_id')) == TOOL)
    schema = json.dumps(tool)
    for key in FILTER_PARAM_SCHEMA_PROPERTIES:
        assert key in schema, key
