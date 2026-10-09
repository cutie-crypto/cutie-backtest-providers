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
    with pytest.raises(ValueError, match="unknown parameter 'direction'"):
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


# ======== 7-P3b2: double_bottom / inverse_head_shoulders / ORB / asia range / calendar ========
# Judgment bars: bottom = breakout close; F1 = first closed breakout of the cycle (a filtered one
# consumes the cycle); F2 = the event's closed bar (bar 0's event is placed at broker step 1).
# Hand filters use EMA10 (alpha 2/11) over an explicit warmup prefix, recomputed with ema() above.

import capture_7p3b2_off_c72c4a1 as off2  # noqa: E402
from strategy_time_layer import TimeContext  # noqa: E402

GOLDEN2 = json.loads((Path(__file__).parent / 'fixtures/7p3b2_off_c72c4a1.json').read_text())
EMA10 = dict(filter_layer_enabled=True, filter_ema_enabled=True, filter_ema_period=10)
CAL = dict(time_entry_at='02:00', time_max_holding_minutes=120, calendar_stop_enabled=False)
WIRED_7P3B2 = ('double_bottom', 'inverse_head_shoulders', 'opening_range_breakout', 'asia_range_breakout',
               'calendar_schedule')
SHORT_ONLY = {'macd_bearish_divergence', 'rsi_bearish_divergence', 'double_top', 'head_shoulders', 'chan_3sell'}


def flat_prefix(close, count, end, freq):
    index = pd.date_range(end=end - pd.Timedelta(freq), periods=count, freq=freq)
    return pd.DataFrame(dict(Open=[close] * count, High=[close + 1] * count, Low=[close - 1] * count,
                             Close=[close] * count, Volume=[1.] * count), index=index)


def direct2(tool, data, params, prefix=None, timeframe='1h'):
    cls = p.TOOL_SPECS['local.backtesting_py.' + tool]['build'](params)['strategy']
    cls._range_timeframe = cls._calendar_timeframe = timeframe
    if prefix is not None:
        cls._warmup_bars = len(prefix)
        cls._warmup_cols = {c: prefix[c].to_numpy(dtype='float64') for c in p._WARMUP_COLUMNS}
    if cls._time_config is not None:
        cls._time_context = TimeContext.build(cls._time_config, timeframe, data.index)
    return Backtest(data, cls, cash=100000, commission=0, exclusive_orders=True, finalize_trades=True).run()


def judged(prefix, data, bar, period=10):
    """close > EMA(period) at main bar `bar`, over prefix + main (independent of entry_mask)."""
    closes = prefix.Close.tolist() + data.Close.tolist()
    return closes[len(prefix) + bar] > ema(closes, period)[len(prefix) + bar]


# ---- off-state ----

@pytest.mark.parametrize('case', sorted(off2.cases()))
@pytest.mark.parametrize('explicit_defaults', [False, True])
def test_7p3b2_off_state_byte_identical_to_main(case, explicit_defaults, monkeypatch):
    tool, params, *rest = off2.cases()[case]
    if explicit_defaults:
        params = {**params, **DEFAULTS}
    monkeypatch.setattr(p, 'entry_mask', lambda *a, **k: pytest.fail('disabled filter performed indicator work'))
    assert off2.snapshot(tool, params, *rest) == GOLDEN2['cases'][case]


def test_7p3b2_golden_covers_every_capture_case():
    assert GOLDEN2['baseline_sha'] == 'c72c4a1'
    assert set(GOLDEN2['cases']) == set(off2.cases())
    assert {case.split('/')[0] for case in GOLDEN2['cases']} == set(WIRED_7P3B2)


# ---- on-state, bottom patterns (breakout close at 53, entry at the 54 open) ----

@pytest.mark.parametrize('name', ['double_bottom', 'inverse_head_shoulders'])
def test_bottom_filter_false_at_breakout_close_discards(name):
    # Breakout is 53 bars in: EMA100 over a 100-bar prefix at 300 stays above the breakout close.
    data = off2.bottom_frame(name)
    prefix = flat_prefix(300., 100, data.index[0], '1h')
    assert direct2(name, data, {}, prefix)['_trades'].EntryBar.tolist() == [54]
    assert not judged(prefix, data, 53, 100)
    # A later close that passes the filter must not replay the discarded breakout.
    data.iloc[54, :4] = [data.Open.iloc[54], 401., data.Low.iloc[54], 400.]
    assert judged(prefix, data, 54, 100)
    assert direct2(name, data, {**EMA10, 'filter_ema_period': 100}, prefix)['_trades'].empty


@pytest.mark.parametrize('name', ['double_bottom', 'inverse_head_shoulders'])
def test_bottom_filter_true_at_breakout_close_enters_next_open(name):
    data = off2.bottom_frame(name)
    prefix = flat_prefix(50., 20, data.index[0], '1h')
    assert judged(prefix, data, 53)
    trades = direct2(name, data, EMA10, prefix)['_trades']
    assert trades.EntryBar.tolist() == [54]
    assert trades.EntryPrice.tolist() == [111. if name == 'double_bottom' else 119.]


# ---- on-state, F1 ranges (range 90..110; breakout close 111 at `signal`, entry at the next open) ----

F1_SIGNAL = {'opening_range_breakout': 4, 'asia_range_breakout': 28}
# Asia breaks out 28 bars in: EMA60 over a 60-bar prefix keeps the 300 level from decaying below 111.
F1_PERIOD = {'opening_range_breakout': 10, 'asia_range_breakout': 60}


def f1_frame(tool):
    return off2.range_frame() if tool == 'opening_range_breakout' else off2.asia_frame()


@pytest.mark.parametrize('tool', ['opening_range_breakout', 'asia_range_breakout'])
def test_range_filter_false_at_first_breakout_consumes_cycle(tool):
    data, signal, period = f1_frame(tool), F1_SIGNAL[tool], F1_PERIOD[tool]
    prefix = flat_prefix(300., period, data.index[0], '15min')
    assert direct2(tool, data, {}, prefix, '15m')['_trades'].EntryBar.tolist()[:1] == [signal + 1]
    assert not judged(prefix, data, signal, period)
    # The next bar is still a breakout and passes the filter; the cycle is already consumed.
    data.iloc[signal + 1] = [112., 400., 111., 400., 1.]
    assert judged(prefix, data, signal + 1, period)
    assert direct2(tool, data, {**EMA10, 'filter_ema_period': period}, prefix, '15m')['_trades'].empty


@pytest.mark.parametrize('tool', ['opening_range_breakout', 'asia_range_breakout'])
def test_range_filter_true_at_first_breakout_enters_next_open(tool):
    data, signal = f1_frame(tool), F1_SIGNAL[tool]
    prefix = flat_prefix(50., 20, data.index[0], '15min')
    assert judged(prefix, data, signal)
    trades = direct2(tool, data, EMA10, prefix, '15m')['_trades']
    assert trades.EntryBar.tolist()[:1] == [signal + 1] and trades.EntryPrice.tolist()[:1] == [112.]


# ---- on-state, F2 calendar ----

def two_day_frame():
    return pd.DataFrame(dict(Open=[100.] * 48, High=[101.] * 48, Low=[99.] * 48, Close=[100.] * 48,
                             Volume=[1.] * 48), index=pd.date_range('2026-01-01', periods=48, freq='h'))


def test_calendar_filter_false_skips_this_event_next_event_still_fires():
    data = two_day_frame()
    data.iloc[25] = [100., 103., 99., 102., 1.]  # day-2 02:00 event closes at 102
    prefix = flat_prefix(300., 20, data.index[0], '1h')
    assert not judged(prefix, data, 1) and judged(prefix, data, 25)
    stats = direct2('calendar_schedule', data, {**CAL, **EMA10}, prefix)
    assert stats['_trades'].EntryBar.tolist() == [26]
    events = stats['_strategy'].calendar_events
    assert [(e['event_utc'][:13], e['status'], e['reason']) for e in events][:2] == [
        ('2026-01-01T02', 'skipped', 'entry_filter'), ('2026-01-02T02', 'filled', None)]


def test_calendar_filter_true_enters_next_open():
    data = two_day_frame()
    data.iloc[1] = [100., 103., 99., 102., 1.]
    prefix = flat_prefix(50., 20, data.index[0], '1h')
    assert judged(prefix, data, 1)
    assert direct2('calendar_schedule', data, {**CAL, **EMA10}, prefix)['_trades'].EntryBar.tolist()[:1] == [2]


@pytest.mark.parametrize('bar1_close,prefix_close,enters', [(1000., 300., False), (1., 50., True)])
def test_calendar_bar0_event_judged_on_bar0_not_submission_bar(bar1_close, prefix_close, enters):
    data = two_day_frame()
    data.iloc[1] = [100., max(100., bar1_close) + 1, min(100., bar1_close) - .5, bar1_close, 1.]
    prefix = flat_prefix(prefix_close, 20, data.index[0], '1h')
    # The 01:00 event is bar 0's close; bar 1 is where the broker first sees it.
    assert judged(prefix, data, 0) == enters and judged(prefix, data, 1) != enters
    stats = direct2('calendar_schedule', data, dict(time_entry_at='01:00', time_max_holding_minutes=60,
                                                     calendar_stop_enabled=False, **EMA10), prefix)
    first = stats['_strategy'].calendar_events[0]
    assert first['event_utc'] == '2026-01-01T01:00:00+00:00'
    assert (first['status'] != 'skipped') == enters
    assert stats['_trades'].EntryBar.tolist()[:1] == ([1] if enters else [])


# ---- runner warmup: F1/F2 fetch the filter prefix only when enabled ----

def post2(monkeypatch, tmp_path, tool, params, data, prefix, timeframe, market, requested):
    def warmup(*args):
        requested.append(args[5])
        return prefix
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a: data.copy())
    monkeypatch.setattr(p, '_fetch_template_warmup', warmup)
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **kw: None)
    step = int(pd.Timedelta(timeframe).total_seconds())
    request = dict(run_id='7p3b2', provider_tool_id='local.backtesting_py.' + tool, provider_params=params,
        symbol='BTCUSDT', market=market, timeframe=timeframe, start_at=int(data.index[0].timestamp()),
        end_at=int(data.index[-1].timestamp()) + step, initial_capital='10000', fee_bps='0', slippage_bps='0')
    return TestClient(p.app).post('/cutie/backtest', json={'backtest': request}).json()


@pytest.mark.parametrize('tool,frame,signal,timeframe,market,params', [
    ('opening_range_breakout', off2.range_frame, 4, '15m', 'futures', {}),
    ('calendar_schedule', off2.calendar_frame, 1, '1h', 'spot', dict(time_entry_at='02:00', time_max_holding_minutes=120)),
])
def test_range_calendar_filter_warmup_first_judgable_bar(monkeypatch, tmp_path, tool, frame, signal, timeframe,
                                                         market, params):
    data = frame()
    prefix = flat_prefix(50., 10, data.index[0], pd.Timedelta(timeframe))
    assert p.TOOL_SPECS['local.backtesting_py.' + tool]['build'](params)['min_bars'] == 2 < 10
    # EMA10 needs 10 bars: main-only, bar `signal` (< 9) is not judgable; with the prefix it passes.
    assert signal < 9 and judged(prefix, data, signal)
    requested = []
    body = post2(monkeypatch, tmp_path, tool, {**params, **EMA10}, data, prefix, timeframe, market, requested)
    assert body['result_status'] == 'success', body
    assert requested == [10]
    assert body['assumptions']['indicator_warmup_bars'] == 10
    assert [t['opened_at'] for t in body['trades']][:1] == [int(data.index[signal + 1].timestamp())]


# ---- catalog / list ----

@pytest.mark.parametrize('name', WIRED_7P3B2)
def test_7p3b2_catalog_publishes_filter_keys(name):
    tool = 'local.backtesting_py.' + name
    properties = p.TOOL_SPECS[tool]['param_schema_properties']
    assert set(FILTER_PARAM_SCHEMA_PROPERTIES) <= set(properties)
    assert getattr(p.TOOL_SPECS[tool]['build'], '_supports_entry_filters', False)
    assert tool not in p.FILTER_LAYER_UNWIRED_TOOLS


def test_range_calendar_schema_trim_keeps_filter_keys_and_no_unconsumed_risk_keys():
    sizing = {'position_size_pct', 'position_size_notional', 'risk_layer_enabled', 'max_holding_bars'}
    consumed = {'opening_range_breakout': sizing, 'asia_range_breakout': sizing,
                'calendar_schedule': sizing | {'stop_loss_pct', 'take_profit_pct'}}
    assert not set(FILTER_PARAM_SCHEMA_PROPERTIES) & set(p._FIXED_RISK_PARAM_SCHEMA_PROPERTIES)
    for name, keys in consumed.items():
        properties = p.TOOL_SPECS['local.backtesting_py.' + name]['param_schema_properties']
        risk = set(p._FIXED_RISK_PARAM_SCHEMA_PROPERTIES) & set(properties)
        assert risk <= keys, (name, risk - keys)
        assert set(FILTER_PARAM_SCHEMA_PROPERTIES) <= set(properties), name


def test_unwired_list_empty_after_7p4_short_templates():
    # 基点 11 - red_streak_rsi(7-P3b) = 10；7-P3b2 再移出 5 个做多模板：10 - 5 = 5；7-P4 接入这 5 个做空模板后为 0。
    assert not p.FILTER_LAYER_UNWIRED_TOOLS
    assert all(getattr(p.TOOL_SPECS['local.backtesting_py.' + n]['build'], '_supports_entry_filters', False)
               for n in SHORT_ONLY)
