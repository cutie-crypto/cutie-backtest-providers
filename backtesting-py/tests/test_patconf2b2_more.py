"""P-PATCONF-2b2: pattern confirmation wired into divergence (4), chan (2), fibonacci, red_streak_rsi, vwap.

Frozen-value classes (worker-2 rules 2 / 3):
- structure frozen at the signal bar s and carried with the signal: divergence / chan order tags (stop, setup,
  center, zd / zg) and the Fibonacci wave stop / target;
- close-based risk state frozen at the confirming bar k from Close[k]: red_streak_rsi / vwap_reversion
  initial_risk_state, Fibonacci's entry reference and any user stop.
Template gates re-judged at k: VWAP's same-UTC-day window, the Fibonacci wave's used / invalid state.
"""
from __future__ import annotations

import random
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from backtesting import Backtest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cutie_backtesting_provider as p  # noqa: E402
import _10b2a_cases as a  # noqa: E402
import _10b2b_cases as b  # noqa: E402
import _10b2d_cases as d  # noqa: E402
import strategy_chan  # noqa: E402
import strategy_divergence  # noqa: E402
import test_patconf2b1_patterns as b1  # noqa: E402
from strategy_entry_filters import (PATTERN_CONFIRM_PARAM_SCHEMA_PROPERTIES, PatternConfirmQueue,  # noqa: E402
                                    pattern_confirm_entries)

PREFIX = 'local.backtesting_py.'
LAYER = dict(filter_layer_enabled=True, filter_pattern_confirm_enabled=True)
TAGGED = ('macd_bullish_divergence', 'rsi_bullish_divergence', 'macd_bearish_divergence', 'rsi_bearish_divergence',
          'chan_3buy', 'chan_3sell')
NINE = TAGGED + ('fibonacci_retracement', 'red_streak_rsi', 'vwap_reversion')
SHORT = ('macd_bearish_divergence', 'rsi_bearish_divergence', 'chan_3sell')
CASES = {**{n: a for n in a.CASES}, **{n: b for n in ('macd_bearish_divergence', 'rsi_bearish_divergence',
                                                       'chan_3buy', 'chan_3sell')}, 'red_streak_rsi': d}
record_orders, set_close, set_bar, bar_of, opened = b1.record_orders, b1.set_close, b1.set_bar, b1.bar_of, b1.opened


def frame(name):
    return d.frame(name) if name == 'red_streak_rsi' else CASES[name].CASES[name][0]()


def post(monkeypatch, tmp_path, name, params, data, capital='10000'):
    body = CASES[name].post(monkeypatch, tmp_path, name, dict(params), data, capital=capital)
    assert body['result_status'] == 'success', body
    return body


def capture_strategy(monkeypatch):
    runs, run = [], Backtest.run

    def record(self, *args, **kwargs):
        runs.append(run(self, *args, **kwargs))
        return runs[-1]
    monkeypatch.setattr(Backtest, 'run', record)
    return runs


def signal_of(monkeypatch, tmp_path, name, data, nth=0):
    """Off state: the nth entry order gives s (it is placed at s) and its tag (None for untagged templates)."""
    orders = record_orders(monkeypatch)
    post(monkeypatch, tmp_path, name, {}, data)
    bar, tag = orders[nth]
    assert tag is None or tag.signal_bar == bar
    return bar, tag


# VWAP's first signal (bar 2, 12:00 UTC on a 6h grid) confirms at 18:00, whose order would fill at 00:00 of the
# next UTC day: the generic cases use its second signal (bar 5, 06:00), whose k + 1 stays on the same day.
NTH = {'vwap_reversion': 1}


def level_of(name, data, s):
    return data['Low' if name in SHORT else 'High'].iloc[s]


# --- whitelist: 22 + 15 + 9 = 46; the 5 range / calendar templates still fail closed --------------------

def test_whitelist_is_46_and_the_catalog_publishes_both_keys_only_for_them(monkeypatch):
    wired = {k.removeprefix(PREFIX) for k, v in p.TOOL_SPECS.items()
             if getattr(v.get('build'), '_supports_pattern_confirm', False)}
    assert set(NINE) <= wired and not set(NINE) & (b1.WIRED_2A | b1.WIRED_2B1)
    assert wired == b1.WIRED_2A | b1.WIRED_2B1 | set(NINE) and len(wired) == 46
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    tools = b1.TestClient(p.app).get('/catalog').json()['tools']
    keys = set(PATTERN_CONFIRM_PARAM_SCHEMA_PROPERTIES)
    published = {t['tool_id'].removeprefix(PREFIX) for t in tools if keys <= set(t['param_schema']['properties'])}
    assert published == wired
    assert all(not keys & set(t['param_schema']['properties']) for t in tools
               if t['tool_id'].removeprefix(PREFIX) not in wired)
    unwired = sorted(k.removeprefix(PREFIX) for k, v in p.TOOL_SPECS.items()
                     if getattr(v.get('build'), '_supports_entry_filters', False)
                     and not v['build']._supports_pattern_confirm)
    # the 5 range / calendar templates + event_window (P-EVENT0, filter layer but no pattern confirmation)
    assert unwired == ['asia_range_breakout', 'calendar_schedule', 'cme_weekend_gap', 'event_window',
                       'opening_range_breakout', 'us_open_momentum']


@pytest.mark.parametrize('name', ['asia_range_breakout', 'calendar_schedule', 'cme_weekend_gap',
                                  'opening_range_breakout', 'us_open_momentum'])
def test_the_5_excluded_templates_still_answer_invalid_params(name, monkeypatch, tmp_path):
    data = frame('red_streak_rsi')
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a, **k: pytest.fail('must fail before data'))
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    request = dict(run_id='patconf2b2', provider_tool_id=PREFIX + name,
                   provider_params={**LAYER, 'direction': 'long'}, symbol='BTCUSDT', market='futures',
                   timeframe='1h', start_at=int(data.index[0].timestamp()), end_at=int(data.index[-1].timestamp()),
                   initial_capital='10000')
    body = b1.TestClient(p.app).post('/cutie/backtest', json={'backtest': request}).json()
    assert body['error_type'] == 'INVALID_PARAMS', body


# --- every template on its own trading frame -----------------------------------------------------------

@pytest.mark.parametrize('name', NINE)
def test_confirmed_signal_fills_at_k_plus_1_with_its_structure_frozen_at_s(name, monkeypatch, tmp_path):
    data = frame(name)
    s, frozen = signal_of(monkeypatch, tmp_path, name, data, NTH.get(name, 0))
    level = level_of(name, data, s)
    set_close(data, s + 1, level - 1 if name in SHORT else level + 1)
    assert signal_of(monkeypatch, tmp_path, name, data, NTH.get(name, 0)) == (s, frozen)  # s is causal
    orders = record_orders(monkeypatch)
    runs = capture_strategy(monkeypatch)
    body = post(monkeypatch, tmp_path, name, {**LAYER, 'filter_pattern_confirm_bars': 1}, data)
    assert s + 1 in [bar for bar, _ in orders] and s not in [bar for bar, _ in orders]
    if frozen is not None:  # divergence / chan: the tag frozen at s is the one placed at k
        assert [(bar, tag) for bar, tag in orders if tag.signal_bar == s] == [(s + 1, frozen)]
    assert (s + 2, Decimal(str(data['Open'].iloc[s + 2]))) in opened(body, data)
    strategy = runs[-1]['_strategy']
    if name == 'fibonacci_retracement':  # wave stop / target frozen at s; decision index = k
        wave = next(w for w in body['raw_report']['fibonacci_retracement']['waves'] if w.get('signal_index') == s + 1)
        assert (wave['frozen_stop'], wave['frozen_target']) == (wave['stop_price'], wave['target_price'])
    if name in ('red_streak_rsi', 'vwap_reversion'):  # close-based entry reference frozen at k
        state = strategy._f6_frozen if name == 'red_streak_rsi' else strategy._f5_frozen
        assert state.entry_price == Decimal(str(data['Close'].iloc[s + 1]))
    counts = body['assumptions']['pattern_confirm']
    assert counts['submitted'] >= 1 and counts['registered'] >= 1


@pytest.mark.parametrize('name', NINE)
def test_unconfirmed_signal_leaves_no_order_and_no_trade(name, monkeypatch, tmp_path):
    data = frame(name)
    s, _ = signal_of(monkeypatch, tmp_path, name, data, NTH.get(name, 0))
    set_close(data, s + 1, level_of(name, data, s))  # exactly at the level: strict, never confirms
    orders = record_orders(monkeypatch)
    body = post(monkeypatch, tmp_path, name, {**LAYER, 'filter_pattern_confirm_bars': 1}, data)
    assert s + 1 not in [bar for bar, _ in orders]
    assert all(bar != s + 2 for bar, _ in opened(body, data))
    assert body['assumptions']['pattern_confirm']['discarded']['unconfirmed'] >= 1


# --- hand-computed trades -------------------------------------------------------------------------------

def hand_trades(monkeypatch, tmp_path, name, data, params=None):
    body = post(monkeypatch, tmp_path, name, {**LAYER, **(params or {})}, data)
    return [(bar_of(data, t['opened_at']), t['side'], t['entry_price'], bar_of(data, t['closed_at']),
             t['exit_price']) for t in body['trades']], body


def test_rsi_bullish_divergence_hand_computed(monkeypatch, tmp_path):
    # Signal s = 23 (O94 H95 L93 C94), stop frozen at s = Low(L2) * 0.999 = 77.922. Level High[23] = 95:
    # bar 24 closes 94 (no), bar 25 closes 110 > 95 -> k = 25 (second bar of the default 2-bar window),
    # fill at Open[26] = 110. Bar 26 Low 77 <= 77.922 -> stop_loss (checked first) at Open[27] = 80.
    data = frame('rsi_bullish_divergence')
    set_bar(data, 26, 110, 111, 77, 79)
    set_bar(data, 27, 80, 81, 79, 80)
    assert signal_of(monkeypatch, tmp_path, 'rsi_bullish_divergence', data)[1].stop == 77.922
    trades, body = hand_trades(monkeypatch, tmp_path, 'rsi_bullish_divergence', data)
    assert trades[0] == (26, 'long', '110', 27, '80')
    entry = body['raw_report']['divergence']['entries'][0]
    # target frozen at the fill: 110 + 2 * (110 - 77.922) = 174.156
    assert (entry['entry_bar'], entry['frozen_stop'], round(entry['frozen_target'], 6)) == (26, 77.922, 174.156)


def test_chan_3sell_hand_computed(monkeypatch, tmp_path):
    # Signal s = 12 (O118 H119 L117 C118), pullback stop frozen at s = 126.126, zd = 134. Level Low[12] = 117:
    # bar 13 closes 115 < 117 -> k = 13, short fill at Open[14] = 110; target = 110 - 2 * (126.126 - 110)
    # = 77.748 (Lows 93 / 92 never reach it). Bar 15 High 127 >= 126.126 -> stop_loss at Open[16] = 127.
    data = frame('chan_3sell')
    set_bar(data, 15, 100, 127, 92, 100)
    set_bar(data, 16, 127, 128, 126, 127)
    assert signal_of(monkeypatch, tmp_path, 'chan_3sell', data)[1].stop == 126.12599999999999
    trades, body = hand_trades(monkeypatch, tmp_path, 'chan_3sell', data)
    assert trades == [(14, 'short', '110', 16, '127')]
    entry = body['raw_report']['chan']['entries'][0]
    assert (entry['entry_bar'], round(entry['frozen_stop'], 6), round(entry['frozen_target'], 6)) == (
        14, 126.126, 77.748)


def test_fibonacci_retracement_hand_computed(monkeypatch, tmp_path):
    # 9T5 frame, swing_n = 2: wave low 100 (bar 2) -> high 120 (bar 5, confirmed at 7); level 120 - 0.618 * 20
    # = 107.64, stop = (120 - 0.786 * 20) * 0.999 = 104.17572, target = swing high 120, all frozen with the
    # wave. Signal s = 8 (O107 H114 L107 C108: Low <= 107.64 * 1.003, bullish close); level High[8] = 114.
    # Bar 9 closes 115 > 114 -> k = 9, fill at Open[10] = 109. Lows 105 stay above 104.17572; bar 11 High 121
    # >= 120 -> take_profit (high / low touch, next-open market) at Open[12] = 120.5.
    data = frame('fibonacci_retracement')
    set_bar(data, 9, 109, 121, 106, 115)
    set_bar(data, 10, 109, 115, 105, 108)
    set_bar(data, 11, 108, 121, 105, 109)
    set_bar(data, 12, 120.5, 121, 119, 120)
    trades, body = hand_trades(monkeypatch, tmp_path, 'fibonacci_retracement', data)
    wave = body['raw_report']['fibonacci_retracement']['waves'][0]
    assert (wave['signal_index'], wave['frozen_stop'], wave['frozen_target']) == (
        9, wave['stop_price'], wave['target_price'])
    assert float(wave['stop_price']) == 104.17572
    assert trades == [(10, 'long', '109', 12, '120.5')]


# --- template gates and risk states judged at k ---------------------------------------------------------

def test_vwap_confirmation_landing_on_the_next_utc_day_is_discarded(monkeypatch, tmp_path):
    # 6h grid: s = 2 at 12:00 (close 94 <= VWAP band). Same-day k = 3 (18:00) decides at 00:00 of the next day,
    # so its order would fill on another UTC day -> template_gate; switch off, s fills at bar 3 (12:00 -> 18:00).
    data = frame('vwap_reversion')
    off = post(monkeypatch, tmp_path, 'vwap_reversion', {}, data)
    assert opened(off, data)[0][0] == 3
    body = post(monkeypatch, tmp_path, 'vwap_reversion', {**LAYER, 'filter_pattern_confirm_bars': 1}, data)
    assert all(bar not in (3, 4) for bar, _ in opened(body, data))
    assert body['assumptions']['pattern_confirm']['discarded']['template_gate'] >= 1
    # k on the next UTC day itself: bar 3 does not confirm (close 94 = High[2] is not above it), bar 4 at
    # 00:00 of day 2 closes 101 > 100 -> k = 4 is a day-2 bar, s is a day-1 signal -> template_gate.
    set_bar(data, 3, 94, 94, 94, 94)
    set_bar(data, 4, 101, 101, 101, 101)
    orders = record_orders(monkeypatch)
    body = post(monkeypatch, tmp_path, 'vwap_reversion', {**LAYER, 'filter_pattern_confirm_bars': 2}, data)
    assert 4 not in [bar for bar, _ in orders] and all(bar != 5 for bar, _ in opened(body, data))
    assert body['assumptions']['pattern_confirm']['discarded']['template_gate'] >= 1


def test_red_streak_percent_stop_is_frozen_at_close_k(monkeypatch, tmp_path):
    # Signal s = 43 closes 92 (High 93.1); bar 44 closes 94 > 93.1 -> k = 44, fill at Open[45].
    # Default 3% stop / 3% target frozen from Close[k] = 94: stop 91.18, target 96.82 (not 89.24 from Close[s]).
    data = frame('red_streak_rsi')
    s, _ = signal_of(monkeypatch, tmp_path, 'red_streak_rsi', data)
    assert s == 43 and data['Close'].iloc[s] == 92
    set_bar(data, 44, 93, 94.1, 92.9, 94)
    runs = capture_strategy(monkeypatch)
    body = post(monkeypatch, tmp_path, 'red_streak_rsi', {**LAYER, 'filter_pattern_confirm_bars': 1}, data)
    assert opened(body, data)[0] == (45, Decimal(str(data['Open'].iloc[45])))
    state = runs[-1]['_strategy']._f6_frozen
    assert state.initial_stop == Decimal('94') * (1 - Decimal('0.03')) == Decimal('91.18')
    assert state.take_price == Decimal('96.82')


# --- placed orders: random signals with positions open (divergence / chan) ------------------------------
# Orders really fill, so positions are open at many confirming bars: a position open at k discards the
# signal as position_or_order_open and places nothing; otherwise exactly one order is placed at k for source[k].

PLACED = {'rsi_bullish_divergence': (strategy_divergence, 'make_divergence_strategy'),
          'macd_bearish_divergence': (strategy_divergence, 'make_divergence_strategy'),
          'chan_3buy': (strategy_chan, 'make_chan_strategy'),
          'chan_3sell': (strategy_chan, 'make_chan_strategy')}


def run_placed(name, seed, monkeypatch, tmp_path):
    rng = random.Random(2000 + seed)
    n, bars = 90, rng.randint(1, 5)
    data = b1.random_frame(rng, n)
    short = name in SHORT
    signals = [20 <= i < n - 6 and rng.random() < .5 for i in range(n)]
    module, factory = PLACED[name]
    make = getattr(module, factory)
    high, low = data['High'].to_numpy(), data['Low'].to_numpy()

    def with_signals(*args, **kwargs):
        class Injected(make(*args, **kwargs)):
            def init(self):
                super().init()
                self._signals = [SimpleNamespace(stop=high[i] + 2 if short else low[i] - 2, setup=i, center=i,
                                                 zd=high[i] + 1, zg=low[i] - 1) if on else None
                                 for i, on in enumerate(signals)]
        return Injected
    monkeypatch.setattr(module, factory, with_signals)
    registered, register = [], PatternConfirmQueue.register

    def record_register(self, s, *args, **kwargs):
        registered.append(s)
        return register(self, s, *args, **kwargs)
    monkeypatch.setattr(PatternConfirmQueue, 'register', record_register)
    runs = capture_strategy(monkeypatch)
    orders = record_orders(monkeypatch)
    body = post(monkeypatch, tmp_path, name, {**LAYER, 'filter_pattern_confirm_bars': bars}, data,
                capital='100000000')
    trades = runs[-1]['_trades']
    spans = list(zip(trades['EntryBar'], trades['ExitBar']))

    def held(k):
        return any(e <= k < x for e, x in spans)
    entries, source = pattern_confirm_entries([i in registered for i in range(n)], data['High'], data['Low'],
                                              data['Close'], direction='short' if short else 'long', bars=bars)
    confirmed = list(np.flatnonzero(entries))
    assert confirmed and all(k < n - 1 for k in confirmed)
    blocked = [k for k in confirmed if held(k)]
    placed = [k for k in confirmed if not held(k)]
    assert [bar for bar, _ in orders] == placed
    assert [tag.signal_bar for _, tag in orders] == [source[k] for k in placed]
    assert all(e - 1 in placed for e in trades['EntryBar'])
    counts = body['assumptions']['pattern_confirm']
    assert counts['registered'] == len(registered)
    assert counts['confirmed'] - counts['discarded']['superseded_by_later_signal'] == len(confirmed)
    assert counts['submitted'] == len(placed)
    assert counts['discarded']['position_or_order_open'] == len(blocked)
    return blocked, len(trades)


@pytest.mark.parametrize('seed', range(4))
@pytest.mark.parametrize('name', sorted(PLACED))
def test_placed_random_signals_discard_at_an_open_position_without_a_second_order(name, seed, monkeypatch,
                                                                                  tmp_path):
    run_placed(name, seed, monkeypatch, tmp_path)


def test_placed_random_runs_do_hit_open_positions(tmp_path):
    # The suite above is only evidence if positions really fill and block confirmations on these seeds.
    for name in sorted(PLACED):
        hits = filled = 0
        for seed in range(4):
            with pytest.MonkeyPatch.context() as mp:
                blocked, count = run_placed(name, seed, mp, tmp_path)
                hits, filled = hits + len(blocked), filled + count
        assert hits >= 1 and filled >= 1, name
