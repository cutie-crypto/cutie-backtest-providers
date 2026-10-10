"""P-PATCONF-2b1: pattern confirmation wired into the 15 self-entering pattern templates.

Candle (bullish_engulfing / hammer_pin_bar / morning_star / three_white_soldiers / bullish_doji_reversal /
inside_bar_breakout and the five short mirrors) and bottom / top (double_bottom / inverse_head_shoulders /
double_top / head_shoulders) templates place their own orders with a tag frozen at the signal bar s.
Switch on: s only registers (long level High[s], short level Low[s]) and the tag travels with the
signal; the template's entry gates (liquidation bar, open position / order, last main bar => no_next_open,
time layer, filter layer) are judged at the confirming bar k, and the order placed at k carries the
tag frozen at s, filling at k + 1. Switch off: tests/test_patconf1_pattern_confirm.py keeps its goldens.
"""
from __future__ import annotations

import random
import sys
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import backtesting.backtesting as bb
from backtesting import Backtest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p  # noqa: E402
import _10b2b_cases as b  # noqa: E402
import _10b2c_cases as c  # noqa: E402
import test_short_pat4_candles as sp  # noqa: E402
import strategy_bottom_patterns  # noqa: E402
import strategy_pattern_template  # noqa: E402
import strategy_top_patterns  # noqa: E402
from strategy_entry_filters import (PATTERN_CONFIRM_PARAM_SCHEMA_PROPERTIES, PatternConfirmQueue,  # noqa: E402
                                    pattern_confirm_entries)

PREFIX = 'local.backtesting_py.'
LAYER = dict(filter_layer_enabled=True, filter_pattern_confirm_enabled=True)
PATTERN_CONFIRM_SCHEMA_KEYS = tuple(PATTERN_CONFIRM_PARAM_SCHEMA_PROPERTIES)
WIRED_2A = set("""adx_di_cross bias_reversion bollinger_breakout bollinger_reversal bollinger_squeeze_breakout
breakout cci_rsi ema_cross ema_pullback ema_rsi_pullback ema_trend_rsi ema_triple_alignment
ichimoku_cloud_breakout keltner_breakout macd macd_above_zero parabolic_sar roc rsi_reversal
stoch_oversold_cross supertrend volume_breakout""".split())
LONG = c.CANDLE + c.BOTTOM
SHORT = sp.SHORT + ('double_top', 'head_shoulders')
WIRED_2B1 = set(LONG + SHORT)
# P-PATCONF-2b2: divergence 4, chan 2, fibonacci, red_streak_rsi, vwap_reversion.
WIRED_2B2 = set("""
macd_bullish_divergence rsi_bullish_divergence macd_bearish_divergence rsi_bearish_divergence
chan_3buy chan_3sell fibonacci_retracement red_streak_rsi vwap_reversion""".split())


def frame(name):
    if name in c.CASES:
        return c.CASES[name]()
    if name in sp.MIRROR:
        return sp.frame(name)
    return b.CASES[name][0]()


def post(monkeypatch, tmp_path, name, params, data, capital='10000'):
    step = int((data.index[1] - data.index[0]).total_seconds())
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a, **k: data.copy())
    monkeypatch.setattr(p, '_fetch_template_warmup', lambda *a, **k: data.iloc[:0].copy())
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **k: None)
    request = dict(run_id='patconf2b1', provider_tool_id=PREFIX + name, provider_params=dict(params),
                   symbol='BTCUSDT', market='futures' if name in SHORT else 'spot', timeframe='1h',
                   start_at=int(data.index[0].timestamp()), end_at=int(data.index[-1].timestamp()) + step,
                   initial_capital=capital, fee_bps='0', slippage_bps='0')
    body = TestClient(p.app).post('/cutie/backtest', json={'backtest': request}).json()
    assert body['result_status'] == 'success', body
    return body


def record_orders(monkeypatch, place=True):
    """(bar the order is placed at, its tag) for every entry order; place=False records only."""
    orders = []
    for side in ('buy', 'sell'):
        original = getattr(bb.Strategy, side)

        def wrapped(self, *args, _original=original, **kwargs):
            orders.append((len(self.data) - 1, kwargs.get('tag')))
            return _original(self, *args, **kwargs) if place else None
        monkeypatch.setattr(bb.Strategy, side, wrapped)
    return orders


def bar_of(data, ts):
    return int(data.index.get_loc(pd.Timestamp(ts, unit='s')))


def opened(body, data):
    return [(bar_of(data, t['opened_at']), Decimal(t['entry_price'])) for t in body['trades']]


def set_close(data, i, close):
    data.iloc[i, data.columns.get_loc('Close')] = close
    data.iloc[i, data.columns.get_loc('High')] = max(data['High'].iloc[i], close)
    data.iloc[i, data.columns.get_loc('Low')] = min(data['Low'].iloc[i], close)


def set_bar(data, i, o, h, low, close):
    data.iloc[i, [data.columns.get_loc(k) for k in ('Open', 'High', 'Low', 'Close')]] = [o, h, low, close]


# --- whitelist: 22 (P-PATCONF-2a) + 15 (this batch); the other 14 filter templates still fail closed ----

def test_whitelist_is_the_2a_22_plus_the_15_pattern_templates():
    wired = {k.removeprefix(PREFIX) for k, v in p.TOOL_SPECS.items()
             if getattr(v.get('build'), '_supports_pattern_confirm', False)}
    assert len(WIRED_2B1) == 15 and not WIRED_2B1 & WIRED_2A
    assert len(WIRED_2B2) == 9 and not WIRED_2B2 & (WIRED_2A | WIRED_2B1)
    assert wired == WIRED_2A | WIRED_2B1 | WIRED_2B2 and len(wired) == 46


def test_catalog_publishes_both_keys_for_the_15_and_the_other_14_still_reject(monkeypatch):
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    tools = {t['tool_id'].removeprefix(PREFIX): t for t in TestClient(p.app).get('/catalog').json()['tools']}
    for name in WIRED_2B1:
        properties = tools[name]['param_schema']['properties']
        assert set(PATTERN_CONFIRM_SCHEMA_KEYS) <= set(properties), name
    unwired = [k.removeprefix(PREFIX) for k, v in p.TOOL_SPECS.items()
               if getattr(v.get('build'), '_supports_entry_filters', False)
               and not v['build']._supports_pattern_confirm]
    assert len(unwired) == 5  # P-PATCONF-2b2: 9 more wired, the 5 range / calendar ones stay out
    for name in unwired:
        assert not set(PATTERN_CONFIRM_SCHEMA_KEYS) & set(tools[name]['param_schema']['properties']), name
        build = p.TOOL_SPECS[PREFIX + name]['build']
        params = dict(LAYER, **({'direction': 'long'} if build._filter_default_direction == 'both' else {}))
        with pytest.raises(ValueError, match='filter_pattern_confirm_enabled is not supported'):
            build(params)


# --- every template on its own trading frame -----------------------------------------------------------

def signal_of(monkeypatch, tmp_path, name, data):
    """Off state: the first entry order gives s (placed at s) and the tag frozen there."""
    orders = record_orders(monkeypatch)
    post(monkeypatch, tmp_path, name, {}, data)
    bar, tag = orders[0]
    assert tag.signal_bar == bar
    return bar, tag


@pytest.mark.parametrize('name', sorted(WIRED_2B1))
def test_confirmed_signal_fills_at_k_plus_1_with_the_stop_frozen_at_s(name, monkeypatch, tmp_path):
    data = frame(name)
    s, frozen = signal_of(monkeypatch, tmp_path, name, data)
    level = data['Low' if name in SHORT else 'High'].iloc[s]
    set_close(data, s + 1, level - 1 if name in SHORT else level + 1)
    assert signal_of(monkeypatch, tmp_path, name, data) == (s, frozen)  # s and its tag are causal
    orders = record_orders(monkeypatch)
    body = post(monkeypatch, tmp_path, name, {**LAYER, 'filter_pattern_confirm_bars': 1}, data)
    placed = [(bar, tag) for bar, tag in orders if tag.signal_bar == s]
    assert placed == [(s + 1, frozen)]
    assert (s + 2, Decimal(str(data['Open'].iloc[s + 2]))) in opened(body, data)
    assert all(bar > s + 1 for bar, _ in opened(body, data))
    counts = body['assumptions']['pattern_confirm']
    assert counts['submitted'] >= 1 and counts['registered'] >= 1


@pytest.mark.parametrize('name', sorted(WIRED_2B1))
def test_unconfirmed_signal_leaves_no_order_and_no_trade(name, monkeypatch, tmp_path):
    data = frame(name)
    s, _ = signal_of(monkeypatch, tmp_path, name, data)
    # Close exactly at the level does not confirm (strict); one-bar window.
    set_close(data, s + 1, data['Low' if name in SHORT else 'High'].iloc[s])
    orders = record_orders(monkeypatch)
    body = post(monkeypatch, tmp_path, name, {**LAYER, 'filter_pattern_confirm_bars': 1}, data)
    assert [bar for bar, tag in orders if tag.signal_bar == s] == []
    assert all(bar != s + 2 for bar, _ in opened(body, data))
    assert body['assumptions']['pattern_confirm']['discarded']['unconfirmed'] >= 1


@pytest.mark.parametrize('name', ['bullish_engulfing', 'three_black_crows', 'double_bottom', 'head_shoulders'])
def test_time_gate_is_judged_at_k_not_at_s(name, monkeypatch, tmp_path):
    data = frame(name)
    s, _ = signal_of(monkeypatch, tmp_path, name, data)
    level = data['Low' if name in SHORT else 'High'].iloc[s]
    set_close(data, s + 1, level - 1 if name in SHORT else level + 1)
    decide_s = data.index[s] + pd.Timedelta('1h')  # a bar decides at its close
    # Session covers only k's decision time: closed at s, open at k.
    session = dict(time_layer_enabled=True, time_session_start=(decide_s + pd.Timedelta('30min')).strftime('%H:%M'),
                   time_session_end=(decide_s + pd.Timedelta('90min')).strftime('%H:%M'))
    off = post(monkeypatch, tmp_path, name, session, data)
    assert all(bar != s + 1 for bar, _ in opened(off, data))  # off: the gate blocks s
    on = post(monkeypatch, tmp_path, name, {**session, **LAYER, 'filter_pattern_confirm_bars': 1}, data)
    assert (s + 2, Decimal(str(data['Open'].iloc[s + 2]))) in opened(on, data)
    # Session covers only s: open at s, closed at k => discarded at k.
    session.update(time_session_start=(decide_s - pd.Timedelta('30min')).strftime('%H:%M'),
                   time_session_end=(decide_s + pd.Timedelta('30min')).strftime('%H:%M'))
    on = post(monkeypatch, tmp_path, name, {**session, **LAYER, 'filter_pattern_confirm_bars': 1}, data)
    assert all(bar != s + 2 for bar, _ in opened(on, data))
    assert on['assumptions']['pattern_confirm']['discarded']['time_gate'] >= 1


# --- hand-computed trades -------------------------------------------------------------------------------

def hand_trades(monkeypatch, tmp_path, name, data, params=None):
    body = post(monkeypatch, tmp_path, name, {**LAYER, **(params or {})}, data)
    return [(bar_of(data, t['opened_at']), t['side'], t['entry_price'], bar_of(data, t['closed_at']),
             t['exit_price']) for t in body['trades']], body


def test_bullish_engulfing_hand_computed(monkeypatch, tmp_path):
    # s = 25 (O99 H105 L97 C104 engulfs bar 24); level High[25] = 105; bar 26 closes 106 > 105 -> k = 26.
    # Stop frozen at s = min(Low[24], Low[25]) * 0.999 = 97 * 0.999 = 96.903; fill at Open[27] = 100.
    # Bar 30 Low 96.5 <= 96.903 -> stop_loss at Open[31] = 97.
    data = frame('bullish_engulfing')
    set_bar(data, 30, 100, 100.5, 96.5, 97)
    set_bar(data, 31, 97, 97.5, 96.5, 97)
    trades, body = hand_trades(monkeypatch, tmp_path, 'bullish_engulfing', data)
    assert trades == [(27, 'long', '100', 31, '97')]
    # 96.903 is a stop at s: with the switch off the same signal fills at 26 and stops on the same bar.
    off = post(monkeypatch, tmp_path, 'bullish_engulfing', {}, data)
    assert [(bar_of(data, t['opened_at']), bar_of(data, t['closed_at'])) for t in off['trades']] == [(26, 31)]


def test_three_black_crows_hand_computed(monkeypatch, tmp_path):
    # Mirror frame: s = 25 (O97 H98 L92.5 C93); level Low[25] = 92.5; bar 26 closes 90 < 92.5 -> k = 26.
    # Stop frozen at s = High[23] * 1.001 = 103 * 1.001 = 103.103; short fill at Open[27] = 90;
    # target = 90 - (103.103 - 90) * 2 = 63.794. Bar 30 High 104 >= 103.103 -> stop_loss at Open[31] = 104.
    data = frame('three_black_crows')
    set_bar(data, 30, 90, 104, 89, 90)
    set_bar(data, 31, 104, 104.5, 103.5, 104)
    trades, body = hand_trades(monkeypatch, tmp_path, 'three_black_crows', data)
    assert trades == [(27, 'short', '90', 31, '104')]
    # Target, not stop: bar 30 Low 63 <= 63.794 -> take_profit at Open[31] = 64.
    set_bar(data, 30, 90, 91, 63, 64)
    set_bar(data, 31, 64, 65, 63.5, 64)
    trades, body = hand_trades(monkeypatch, tmp_path, 'three_black_crows', data)
    assert trades == [(27, 'short', '90', 31, '64')]


def test_double_bottom_hand_computed(monkeypatch, tmp_path):
    # Bottoms Low 100 at bars 28 and 46, neckline High 110 at bar 35; breakout s = 53 closes 111 > 110.
    # Frozen at s: stop = 100 * 0.999 = 99.9, target = 2 * 110 - 100 = 120. Level High[53] = 112:
    # the flat 111 closes never confirm, so bar 54 closes 113 -> k = 54, fill at Open[55] = 111.
    # Bar 58 High 121 >= 120 -> take_profit at Open[59] = 120.5.
    data = frame('double_bottom')
    trades, body = hand_trades(monkeypatch, tmp_path, 'double_bottom', data)
    assert trades == [] and body['assumptions']['pattern_confirm']['discarded']['unconfirmed'] == 1
    set_bar(data, 54, 111, 113, 110, 113)
    set_bar(data, 58, 111, 121, 110, 120)
    set_bar(data, 59, 120.5, 121, 119, 120)
    trades, body = hand_trades(monkeypatch, tmp_path, 'double_bottom', data)
    assert trades == [(55, 'long', '111', 59, '120.5')]


def test_head_shoulders_hand_computed(monkeypatch, tmp_path):
    # Shoulders High 100 / 99 at bars 16 / 46, head 105 at bar 31, neckline lows 90 / 86 at bars 23 / 38;
    # breakdown s = 53 opens 81. Frozen at s: stop = 99 * 1.001 = 99.099. Level Low[53] = 80: the flat
    # 81 closes never confirm within 2 bars; bar 55 closes 79 < 80 -> k = 55 (second window bar),
    # short fill at Open[56] = 81. Bar 60 High 99.5 >= 99.099 -> stop_loss at Open[61] = 99.5.
    data = frame('head_shoulders')
    trades, body = hand_trades(monkeypatch, tmp_path, 'head_shoulders', data)
    assert trades == [] and body['assumptions']['pattern_confirm']['discarded']['unconfirmed'] == 1
    set_bar(data, 55, 81, 82, 78, 79)
    set_bar(data, 60, 81, 99.5, 80, 81)
    set_bar(data, 61, 99.5, 100, 99, 99.5)
    trades, body = hand_trades(monkeypatch, tmp_path, 'head_shoulders', data)
    assert trades == [(56, 'short', '81', 61, '99.5')]


# --- equivalence: random signals, bar-by-bar wiring == pattern_confirm_entries ---------------------------

FACTORIES = {'bullish_engulfing': (strategy_pattern_template, 'make_pattern_strategy'),
             'bearish_engulfing': (strategy_pattern_template, 'make_short_pattern_strategy'),
             'double_bottom': (strategy_bottom_patterns, 'make_bottom_strategy'),
             'double_top': (strategy_top_patterns, 'make_top_strategy')}


def random_frame(rng, n):
    closes = np.cumsum([rng.uniform(-1, 1) for _ in range(n)]) + 100
    opens = np.r_[closes[0], closes[:-1]]
    high = np.maximum(opens, closes) + [rng.choice([0, 0, .3, 1]) for _ in range(n)]
    low = np.minimum(opens, closes) - [rng.choice([0, .3, 1]) for _ in range(n)]
    return pd.DataFrame(dict(Open=opens, High=high, Low=low, Close=closes, Volume=1.0),
                        index=pd.date_range('2026-01-01', periods=n, freq='h'))


@pytest.mark.parametrize('seed', range(16))
@pytest.mark.parametrize('name', sorted(FACTORIES))
def test_random_signals_match_pattern_confirm_entries(name, seed, monkeypatch, tmp_path):
    rng = random.Random(seed)
    n, bars = 90, rng.randint(1, 5)
    data = random_frame(rng, n)
    signals = [i >= 20 and rng.random() < .3 for i in range(n)]
    module, factory = FACTORIES[name]
    make = getattr(module, factory)
    entry = {'double_bottom': strategy_bottom_patterns.BottomEntry,
             'double_top': strategy_top_patterns.TopEntry}.get(name)

    def with_signals(*args, **kwargs):
        class Injected(make(*args, **kwargs)):
            def init(self):
                super().init()
                if entry is None:
                    self._signals = list(signals)
                else:
                    self._signals = [entry(i, 1.0, 2.0) if on else None for i, on in enumerate(signals)]
        return Injected
    monkeypatch.setattr(module, factory, with_signals)
    orders = record_orders(monkeypatch, place=False)  # never fills: no position ever blocks a confirmation
    body = post(monkeypatch, tmp_path, name, {**LAYER, 'filter_pattern_confirm_bars': bars}, data)
    short = name in SHORT
    entries, source = pattern_confirm_entries(signals, data['High'], data['Low'], data['Close'],
                                              direction='short' if short else 'long', bars=bars)
    expected = [k for k in np.flatnonzero(entries) if k < n - 1]  # the last bar has no next open
    assert [bar for bar, _ in orders] == expected
    assert [tag.signal_bar for _, tag in orders] == [source[k] for k in expected]
    for _, tag in orders:
        s = tag.signal_bar
        if name == 'bullish_engulfing':  # anchor of s, never of k
            assert tag.stop == min(data['Low'].iloc[s - 1], data['Low'].iloc[s]) * 0.999
        elif name == 'bearish_engulfing':
            assert tag.stop == max(data['High'].iloc[s - 1], data['High'].iloc[s]) * 1.001
        else:
            assert (tag.stop, tag.target) == (1.0, 2.0)
    counts = body['assumptions']['pattern_confirm']
    assert counts['registered'] == sum(signals)
    assert counts['submitted'] == len(expected)
    assert counts['discarded']['no_next_open'] == int(entries[n - 1])


def test_open_position_at_k_discards_the_confirmed_signal(monkeypatch, tmp_path):
    # Bar 26 confirms s = 25 and places the order (fills at Open[27] = 100); an extra signal on bar 26
    # confirms at bar 27 (close 108 > High[26] = 107) while that position is open: discarded.
    data = frame('bullish_engulfing')
    set_bar(data, 27, 100, 108, 99.5, 108)
    make = strategy_pattern_template.make_pattern_strategy

    def with_extra_signal(*args, **kwargs):
        class Extra(make(*args, **kwargs)):
            def init(self):
                super().init()
                self._signals[26] = True
        return Extra
    monkeypatch.setattr(strategy_pattern_template, 'make_pattern_strategy', with_extra_signal)
    orders = record_orders(monkeypatch)
    body = post(monkeypatch, tmp_path, 'bullish_engulfing', LAYER, data)
    counts = body['assumptions']['pattern_confirm']
    assert [(bar, tag.signal_bar) for bar, tag in orders] == [(26, 25)]
    assert counts['discarded']['position_or_order_open'] >= 1


# --- queue payload ---------------------------------------------------------------------------------------

def test_queue_hands_back_the_payload_of_the_latest_confirming_signal():
    queue = PatternConfirmQueue(2)
    assert queue.advance(0, 100) is None and queue.payload is None
    queue.register(0, 'long', 101, 'tag0')
    assert queue.advance(1, 100) is None and queue.payload is None
    queue.register(1, 'long', 101.5, 'tag1')
    assert queue.advance(2, 102) == (1, 'long') and queue.payload == 'tag1'
    assert queue.report()['discarded']['superseded_by_later_signal'] == 1
    assert queue.advance(3, 200) is None and queue.payload is None  # consumed: never confirms twice
    queue.register(3, 'short', 99)  # 2a callers: no payload
    assert queue.advance(4, 98) == (3, 'short') and queue.payload is None


# --- placed orders (pi MEDIUM on 2b1): random signals with positions open, all 15 templates ----------------
# Orders really fill here, so positions are open at many confirming bars. Every confirming bar k is checked
# against the trades the run produced: a position open at k (entry bar <= k < exit bar) discards the signal
# as position_or_order_open and places nothing; otherwise exactly one order is placed at k for source[k].

PLACED_FACTORIES = {**{name: (strategy_pattern_template, 'make_pattern_strategy', None) for name in c.CANDLE},
                    **{name: (strategy_pattern_template, 'make_short_pattern_strategy', None) for name in sp.SHORT},
                    **{name: (strategy_bottom_patterns, 'make_bottom_strategy', strategy_bottom_patterns.BottomEntry)
                       for name in c.BOTTOM},
                    **{name: (strategy_top_patterns, 'make_top_strategy', strategy_top_patterns.TopEntry)
                       for name in ('double_top', 'head_shoulders')}}


def run_placed(name, seed, monkeypatch, tmp_path):
    """One placed run; asserts every confirming bar and returns the bars discarded at an open position."""
    rng = random.Random(1000 + seed)
    n, bars = 90, rng.randint(1, 5)
    data = random_frame(rng, n)
    short = name in SHORT
    # No signal within 5 bars of the end: no confirmation lands on the tail, every k has a next open.
    signals = [20 <= i < n - 6 and rng.random() < .5 for i in range(n)]
    module, factory, entry = PLACED_FACTORIES[name]
    make = getattr(module, factory)
    high, low = data['High'].to_numpy(), data['Low'].to_numpy()

    def with_signals(*args, **kwargs):
        class Injected(make(*args, **kwargs)):
            def init(self):
                super().init()
                if entry is None:
                    self._signals = list(signals)
                elif short:  # stop above, target below
                    self._signals = [entry(i, high[i] + 2, low[i] - 3) if on else None for i, on in enumerate(signals)]
                else:
                    self._signals = [entry(i, low[i] - 2, high[i] + 3) if on else None for i, on in enumerate(signals)]
        return Injected
    monkeypatch.setattr(module, factory, with_signals)
    registered, runs = [], []
    register, run = PatternConfirmQueue.register, Backtest.run

    def record_register(self, s, *args, **kwargs):
        registered.append(s)
        return register(self, s, *args, **kwargs)

    def record_run(self, *args, **kwargs):
        runs.append(run(self, *args, **kwargs))
        return runs[-1]
    monkeypatch.setattr(PatternConfirmQueue, 'register', record_register)
    monkeypatch.setattr(Backtest, 'run', record_run)
    orders = record_orders(monkeypatch)  # real orders: they fill at k + 1 and hold positions
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
    assert [bar for bar, _ in orders] == placed  # no order at a bar with an open position
    assert [tag.signal_bar for _, tag in orders] == [source[k] for k in placed]
    assert all(e - 1 in placed for e in trades['EntryBar'])  # every fill comes from one of those orders
    counts = body['assumptions']['pattern_confirm']
    # confirmed counts signals, entries count bars: the signals beaten at the same k are superseded
    assert counts['registered'] == len(registered)
    assert counts['confirmed'] - counts['discarded']['superseded_by_later_signal'] == len(confirmed)
    assert counts['submitted'] == len(placed)
    assert counts['discarded']['position_or_order_open'] == len(blocked)
    return blocked


@pytest.mark.parametrize('seed', range(4))
@pytest.mark.parametrize('name', sorted(PLACED_FACTORIES))
def test_placed_random_signals_discard_at_an_open_position_without_a_second_order(name, seed, monkeypatch,
                                                                                  tmp_path):
    run_placed(name, seed, monkeypatch, tmp_path)


def test_placed_random_runs_do_hit_open_positions(tmp_path):
    # The suite above is only evidence if open positions really block confirmations on these seeds.
    for name in sorted(PLACED_FACTORIES):
        hits = 0
        for seed in range(4):
            with pytest.MonkeyPatch.context() as mp:
                hits += len(run_placed(name, seed, mp, tmp_path))
        assert hits >= 1, name


# --- the frozen-stop skip (guarded_process_orders) still applies to a confirmed order ---------------------

REPORT_KEY = {'bullish_engulfing': 'candle_pattern', 'double_top': 'top_pattern'}


@pytest.mark.parametrize('name', sorted(REPORT_KEY))
def test_confirmed_order_opening_beyond_the_frozen_stop_is_skipped(name, monkeypatch, tmp_path):
    data = frame(name)
    s, frozen = signal_of(monkeypatch, tmp_path, name, data)
    short = name in SHORT
    level = data['Low' if short else 'High'].iloc[s]
    set_close(data, s + 1, level - 1 if short else level + 1)  # k = s + 1, order placed at k
    gap = frozen.stop + 0.5 if short else frozen.stop - 0.5  # Open[k + 1] beyond the stop frozen at s
    set_bar(data, s + 2, gap, gap + 0.5, gap - 0.5, gap)
    assert signal_of(monkeypatch, tmp_path, name, data) == (s, frozen)
    orders = record_orders(monkeypatch)
    body = post(monkeypatch, tmp_path, name, {**LAYER, 'filter_pattern_confirm_bars': 1}, data)
    assert [(bar, tag) for bar, tag in orders if tag.signal_bar == s] == [(s + 1, frozen)]
    assert all(bar != s + 2 for bar, _ in opened(body, data))
    skipped = body['raw_report'][REPORT_KEY[name]]['skipped_entries']
    assert [(e['signal_bar'], e['entry_bar'], e['frozen_stop']) for e in skipped if e['signal_bar'] == s] == [
        (s, s + 2, frozen.stop)]
    assert body['assumptions']['pattern_confirm']['submitted'] >= 1
