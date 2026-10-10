"""P-PATCONF-1: pattern-confirmation filter engine and FILTER keys, no template wired yet.

- Keys omitted: every template supporting entry filters is byte-identical to 5ddd8eb
  (fixtures/patconf1_5ddd8eb.json; volume_breakout's trading path stays pinned by its own golden).
- Engine: a signal at bar s enters only when a close in s+1..s+N is strictly beyond the signal bar's
  high (long) / low (short); otherwise the signal is discarded. Driven by the real signal bars of
  bullish_engulfing and three_black_crows (entry bar - 1 of their own runs).
- No template is wired: the keys stay out of every catalog schema and enabling the switch fails closed.
"""
import json
import math
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent / 'fixtures'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import capture_patconf1_5ddd8eb as base  # noqa: E402
import capture_plow2a_b48e65e as cap  # noqa: E402
import cutie_backtesting_provider as p  # noqa: E402
import _10b2c_cases as c  # noqa: E402
import _10b2d_cases as range_cases  # noqa: E402
import _pliq1_cases as q  # noqa: E402
import test_short_pat4_candles as short_pat4  # noqa: E402
from strategy_entry_filters import (FILTER_PARAM_SCHEMA_PROPERTIES, PATTERN_CONFIRM_PARAM_SCHEMA_PROPERTIES,  # noqa: E402
                                    FilterConfig, pattern_confirm_entries)

PATTERN_CONFIRM_SCHEMA_KEYS = tuple(PATTERN_CONFIRM_PARAM_SCHEMA_PROPERTIES)

GOLDEN = json.loads((Path(__file__).parent / 'fixtures/patconf1_5ddd8eb.json').read_text())
# P-EVENT0: event_window postdates 5ddd8eb (not in the immutable golden); its keys-omitted body is pinned
# byte-for-byte by tests/fixtures/event0_window_golden.json (test_event0_event_window.py).
EVENT0 = {'event_window'}
CASES = {key: case for key, case in base.cases().items() if case[0] not in EVENT0}
LAYER = dict(filter_layer_enabled=True, filter_pattern_confirm_enabled=True)
# P-PATCONF-2a whitelist: templates whose entries all go through _risk_buy / _risk_sell,
# plus the 15 self-entering candle / bottom / top pattern templates of P-PATCONF-2b1.
WIRED = set("""adx_di_cross bias_reversion bollinger_breakout bollinger_reversal bollinger_squeeze_breakout
breakout cci_rsi ema_cross ema_pullback ema_rsi_pullback ema_trend_rsi ema_triple_alignment
ichimoku_cloud_breakout keltner_breakout macd macd_above_zero parabolic_sar roc rsi_reversal
stoch_oversold_cross supertrend volume_breakout""".split()) | set("""
bullish_engulfing hammer_pin_bar morning_star three_white_soldiers bullish_doji_reversal
inside_bar_breakout bearish_engulfing shooting_star evening_star three_black_crows bearish_doji_reversal
double_bottom inverse_head_shoulders double_top head_shoulders""".split()) | set("""
macd_bullish_divergence rsi_bullish_divergence macd_bearish_divergence rsi_bearish_divergence
chan_3buy chan_3sell fibonacci_retracement red_streak_rsi vwap_reversion""".split())  # P-PATCONF-2b2


def unwired_tools():
    return [tool for tool in base.filter_tools() if tool not in WIRED]


# --- keys omitted: byte-identical -------------------------------------------------------------------

@pytest.mark.parametrize('case', sorted(CASES))
def test_omitted_keys_byte_identical_to_5ddd8eb(case, monkeypatch, tmp_path):
    assert base.snapshot(monkeypatch, tmp_path, *CASES[case]) == GOLDEN['cases'][case]


def test_golden_covers_every_filter_template():
    assert GOLDEN['baseline_sha'] == '5ddd8eb' and set(GOLDEN['cases']) == set(CASES)
    tools = base.filter_tools()
    assert len(tools) == 52 and EVENT0 <= set(tools)
    assert {k.split('/', 1)[1] for k in CASES if k.startswith('off/')} == set(tools) - EVENT0
    assert json.loads((Path(__file__).parent / 'fixtures/event0_window_golden.json').read_text())['trades']
    traded = {k.split('/', 1)[1] for k, v in GOLDEN['cases'].items() if json.loads(v)['trades']}
    assert set(tools) - EVENT0 - traded == {'volume_breakout'}


# --- schema / parse ---------------------------------------------------------------------------------

def test_pattern_confirm_schema_is_two_keys_apart_from_published_filter_keys():
    assert PATTERN_CONFIRM_PARAM_SCHEMA_PROPERTIES == {
        'filter_pattern_confirm_enabled': {'type': 'boolean', 'default': False},
        'filter_pattern_confirm_bars': {'type': 'integer', 'default': 2, 'minimum': 1, 'maximum': 5}}
    assert len(FILTER_PARAM_SCHEMA_PROPERTIES) == 10
    assert not set(PATTERN_CONFIRM_SCHEMA_KEYS) & set(FILTER_PARAM_SCHEMA_PROPERTIES)


def test_no_catalog_schema_publishes_the_keys_outside_the_whitelist(monkeypatch):
    # P-PATCONF-2a / 2b1: only the 22 _risk_buy/_risk_sell templates and the 15 self-entering pattern
    # templates are wired, plus the 9 of P-PATCONF-2b2; the other 5 stay unpublished.
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    tools = TestClient(p.app).get('/catalog').json()['tools']
    assert len(tools) == 60  # main 59 + P-EVENT0 event_window
    published = set()
    for tool in tools:
        keys = set(PATTERN_CONFIRM_SCHEMA_KEYS) & set(tool['param_schema']['properties'])
        assert keys in (set(), set(PATTERN_CONFIRM_SCHEMA_KEYS)), tool['tool_id']
        if keys:
            published.add(tool['tool_id'].removeprefix('local.backtesting_py.'))
    assert published == WIRED
    for name, spec in p.TOOL_SPECS.items():
        if getattr(spec.get('build'), '_supports_entry_filters', False):
            assert spec['build']._supports_pattern_confirm is (name.removeprefix('local.backtesting_py.') in WIRED)


def test_parse_defaults_and_range():
    config = FilterConfig.parse({})
    assert (config.pattern_confirm_enabled, config.pattern_confirm_bars) == (False, 2)
    for bars in (1, 5):
        assert FilterConfig.parse({**LAYER, 'filter_pattern_confirm_bars': bars}).pattern_confirm_bars == bars
    for bars in (0, 6):
        with pytest.raises(ValueError, match='filter_pattern_confirm_bars must be within 1-5'):
            FilterConfig.parse({**LAYER, 'filter_pattern_confirm_bars': bars})
    with pytest.raises(ValueError, match='filter_pattern_confirm_bars must be a finite integer'):
        FilterConfig.parse({**LAYER, 'filter_pattern_confirm_bars': 2.0})


def test_parse_switch_rules():
    # the switch alone counts as a filter
    assert FilterConfig.parse(LAYER).enabled
    with pytest.raises(ValueError, match='require filter_layer_enabled=true'):
        FilterConfig.parse({'filter_pattern_confirm_enabled': True})
    with pytest.raises(ValueError, match='non-default pattern_confirm parameters require filter_pattern_confirm_enabled'):
        FilterConfig.parse({'filter_layer_enabled': True, 'filter_ema_enabled': True, 'filter_pattern_confirm_bars': 3})
    with pytest.raises(ValueError, match='filter_timeframe requires an ema, macd or supertrend filter'):
        FilterConfig.parse({**LAYER, 'filter_timeframe': '4h'})
    assert FilterConfig.parse({**LAYER, 'filter_ema_enabled': True, 'filter_timeframe': '4h'}).timeframe == '4h'


def test_report_names_the_confirmation_rule():
    config = FilterConfig.parse({**LAYER, 'filter_pattern_confirm_bars': 3})
    assert config.report('long')['predicates'] == {'pattern_confirm': dict(
        rule='close > signal_bar_high', window='signal_bar+1..signal_bar+3', timeframe='primary',
        entry='first_confirming_bar_close_then_next_open', unconfirmed='discard_signal')}
    assert config.report('short')['predicates']['pattern_confirm']['rule'] == 'close < signal_bar_low'
    assert config.required_bars == 0
    assert 'pattern_confirm' not in FilterConfig.parse({'filter_layer_enabled': True,
                                                        'filter_ema_enabled': True}).report()['predicates']


def test_whitelist_splits_the_filter_templates_46_and_5_plus_event_window():
    assert WIRED <= set(base.filter_tools()) and len(WIRED) == 22 + 15 + 9
    # the 5 range / calendar templates + event_window (P-EVENT0, also unwired)
    assert len(unwired_tools()) == 5 + len(EVENT0) and EVENT0 <= set(unwired_tools())


@pytest.mark.parametrize('tool', unwired_tools())
def test_enabling_the_switch_fails_closed_on_every_unwired_template(tool, monkeypatch, tmp_path):
    if tool in cap.PLOW1_TOOLS:
        body = range_cases.post(monkeypatch, tmp_path, tool, LAYER)
    else:
        body, _ = cap.post(monkeypatch, tmp_path, tool, {**cap.params_for(tool), **LAYER}, 'contiguous')
    # first gate: the key is outside the tool's published param_schema
    assert body['result_status'] != 'success' and body['error_type'] == 'INVALID_PARAMS', body
    assert body['error_message'] == "unknown parameter 'filter_pattern_confirm_enabled' (not in tool param_schema)"


@pytest.mark.parametrize('tool', unwired_tools())
def test_build_guard_rejects_the_switch_behind_the_schema_gate(tool):
    # second gate: a build reached without the schema check (or a schema published before wiring)
    params = {**(cap.params_for(tool) if tool not in cap.PLOW1_TOOLS else {}), **LAYER}
    with pytest.raises(ValueError, match='INVALID_PARAMS:filter_pattern_confirm_enabled is not supported by this template'):
        p.TOOL_SPECS['local.backtesting_py.' + tool]['build'](params)


# --- engine on real template signal bars ------------------------------------------------------------

def signal_bar(name):
    """The real template's decision bar on its own frame: entry bar - 1 (fills at next open)."""
    with pytest.MonkeyPatch.context() as mp, tempfile.TemporaryDirectory() as reports:
        if name == 'bullish_engulfing':
            data = c.CASES[name]()
            body = c.post(mp, Path(reports), name, {}, data=data)
        else:
            body, data = short_pat4.post(mp, Path(reports), name)
    assert body['result_status'] == 'success' and body['trades'], body
    return data, q.bar_of(data, body['trades'][0]['opened_at']) - 1


@pytest.fixture(scope='module')
def real_signals():
    return {name: signal_bar(name) for name in ('bullish_engulfing', 'three_black_crows')}


SIDES = {'bullish_engulfing': ('long', 'High', 1), 'three_black_crows': ('short', 'Low', -1)}


def arrays(data):
    return {k: data[k].to_numpy(dtype='float64').copy() for k in ('High', 'Low', 'Close')}


def run(cols, s, direction, bars=2):
    signals = np.zeros(len(cols['Close']), dtype=bool)
    signals[s] = True
    return pattern_confirm_entries(signals, cols['High'], cols['Low'], cols['Close'], direction=direction, bars=bars)


@pytest.mark.parametrize('name', sorted(SIDES))
def test_confirmed_signal_entry_is_deferred(name, real_signals):
    data, s = real_signals[name]
    direction, level_col, sign = SIDES[name]
    cols = arrays(data)
    level = cols[level_col][s]
    cols['Close'][s + 1] = level              # touching the level is not a confirmation
    cols['Close'][s + 2] = level + sign * 0.5  # strictly beyond: confirms on the 2nd bar
    entries, source = run(cols, s, direction)
    # the template's own decision bar is s (fill s + 1 open); confirmed decision moves to s + 2
    assert list(np.flatnonzero(entries)) == [s + 2] and source[s + 2] == s


@pytest.mark.parametrize('name', sorted(SIDES))
def test_unconfirmed_signal_is_discarded(name, real_signals):
    data, s = real_signals[name]
    direction, level_col, sign = SIDES[name]
    cols = arrays(data)
    level = cols[level_col][s]
    cols['Close'][s + 1] = cols['Close'][s + 2] = level
    cols['Close'][s + 3] = level + sign * 5   # beyond, but outside N=2
    entries, source = run(cols, s, direction)
    assert not entries.any() and (source == -1).all()
    entries, source = run(cols, s, direction, bars=3)
    assert list(np.flatnonzero(entries)) == [s + 3] and source[s + 3] == s


@pytest.mark.parametrize('name', sorted(SIDES))
def test_wrong_side_close_never_confirms(name, real_signals):
    data, s = real_signals[name]
    direction, level_col, sign = SIDES[name]
    cols = arrays(data)
    for k in range(s + 1, s + 6):
        cols['Close'][k] = cols[level_col][s] - sign * 5
    entries, _ = run(cols, s, direction, bars=5)
    assert not entries.any()


def test_first_confirmation_consumes_the_signal():
    high = np.array([10, 0, 0, 0, 0.0])
    close = np.array([5, 11, 12, 13, 14.0])
    entries, source = pattern_confirm_entries([1, 0, 0, 0, 0], high, high * 0, close, direction='long', bars=5)
    assert list(np.flatnonzero(entries)) == [1] and list(source) == [-1, 0, -1, -1, -1]


def test_latest_signal_wins_when_two_confirm_on_one_bar():
    high = np.array([10, 9, 0, 0.0])
    close = np.array([5, 8, 12, 12.0])
    entries, source = pattern_confirm_entries([1, 1, 0, 0], high, high * 0, close, direction='long', bars=2)
    assert list(np.flatnonzero(entries)) == [2] and source[2] == 1


def test_causal_future_bars_do_not_change_past_entries():
    rng = np.random.default_rng(7)
    close = 100 + rng.normal(0, 2, 200).cumsum()
    high, low = close + 1, close - 1
    signals = rng.random(200) < 0.1
    full, full_source = pattern_confirm_entries(signals, high, low, close, direction='short', bars=3)
    for cut in (50, 120, 199):
        part, part_source = pattern_confirm_entries(signals[:cut], high[:cut], low[:cut], close[:cut],
                                                    direction='short', bars=3)
        assert (part == full[:cut]).all() and (part_source == full_source[:cut]).all()


def test_signal_on_last_bars_and_non_finite_values():
    nan = math.nan
    entries, _ = pattern_confirm_entries([0, 0, 1], [1, 1, 1.0], [0, 0, 0.0], [1, 1, 1.0], direction='long', bars=2)
    assert not entries.any()
    entries, _ = pattern_confirm_entries([1, 0, 0], [nan, 0, 0], [0, 0, 0.0], [0, 9, 9.0], direction='long', bars=2)
    assert not entries.any()
    entries, _ = pattern_confirm_entries([1, 0, 0], [5, 0, 0.0], [0, 0, 0.0], [0, nan, 6.0], direction='long', bars=2)
    assert list(np.flatnonzero(entries)) == [2]


def test_engine_rejects_bad_arguments():
    with pytest.raises(ValueError, match='direction must be long or short'):
        pattern_confirm_entries([1], [1.0], [1.0], [1.0], direction='both', bars=2)
    for bars in (0, 6, 2.0, True):
        with pytest.raises(ValueError, match='bars must be an integer within 1-5'):
            pattern_confirm_entries([1], [1.0], [1.0], [1.0], direction='long', bars=bars)
    with pytest.raises(ValueError, match='equal length'):
        pattern_confirm_entries([1, 0], [1.0], [1.0], [1.0, 2.0], direction='long', bars=2)
