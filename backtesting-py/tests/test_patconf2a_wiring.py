"""P-PATCONF-2a: pattern confirmation wired into the 22 templates that enter via _risk_buy / _risk_sell.

Switch on: the signal bar s only registers (long level High[s], short level Low[s], window N); every
bar k first advances the pending signals; the latest one confirming at k goes down the original entry
path at k (liquidation-bar guard, time gate, filter gate judged at k) and fills at the next open. Any
confirmed signal is consumed: position / order open at k or a closed gate discards it for good.
Switch off: tests/test_patconf1_pattern_confirm.py keeps the 128 byte-identical golden cases.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from backtesting import Backtest, Strategy
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent / 'fixtures'))
import capture_plow2a_b48e65e as cap  # noqa: E402
import cutie_backtesting_provider as p  # noqa: E402
import test_risk_overlay_compatibility as compat  # noqa: E402
from strategy_entry_filters import (FilterConfig, PatternConfirmQueue,  # noqa: E402
                                    pattern_confirm_entries)

WIRED = set("""adx_di_cross bias_reversion bollinger_breakout bollinger_reversal bollinger_squeeze_breakout
breakout cci_rsi ema_cross ema_pullback ema_rsi_pullback ema_trend_rsi ema_triple_alignment
ichimoku_cloud_breakout keltner_breakout macd macd_above_zero parabolic_sar roc rsi_reversal
stoch_oversold_cross supertrend volume_breakout""".split())
# P-PATCONF-2b1: the 15 self-entering pattern templates (candle 11 + bottom / top 4), wired on top of the 22.
WIRED_2B1 = set("""bearish_doji_reversal bearish_engulfing bullish_doji_reversal bullish_engulfing double_bottom
double_top evening_star hammer_pin_bar head_shoulders inside_bar_breakout inverse_head_shoulders morning_star
shooting_star three_black_crows three_white_soldiers""".split())
# P-PATCONF-2b2: divergence 4, chan 2, fibonacci, red_streak_rsi, vwap_reversion.
WIRED_2B2 = set("""
macd_bullish_divergence rsi_bullish_divergence macd_bearish_divergence rsi_bearish_divergence
chan_3buy chan_3sell fibonacci_retracement red_streak_rsi vwap_reversion""".split())
LAYER = dict(filter_layer_enabled=True, filter_pattern_confirm_enabled=True)
PREFIX = 'local.backtesting_py.'
# volume_breakout never signals on the golden frame with its defaults (zero trades in both P-PATCONF-1
# and P-LOW2a goldens); the same frame with a short lookback and the schema-minimum multiple does.
GOLDEN_PARAMS = {'volume_breakout': dict(lookback=10, volume_avg_period=5, volume_multiple=1.2)}


def build(tool, params):
    return p.TOOL_SPECS[PREFIX + tool]['build'](params)['strategy']


# --- whitelist == every template the decorator marks -------------------------------------------------

def test_wired_templates_are_exactly_the_whitelist():
    wired = {k.removeprefix(PREFIX) for k, v in p.TOOL_SPECS.items()
             if getattr(v.get('build'), '_supports_pattern_confirm', False)}
    assert len(WIRED) == 22 and len(WIRED_2B1) == 15 and not WIRED & WIRED_2B1
    assert len(WIRED_2B2) == 9 and not (WIRED | WIRED_2B1) & WIRED_2B2
    assert wired == WIRED | WIRED_2B1 | WIRED_2B2 and len(wired) == 46


# --- wiring evidence: every whitelisted template registers on its golden frame -----------------------

@pytest.mark.parametrize('tool', sorted(WIRED))
def test_switch_on_registers_signals_on_the_golden_frame(tool, monkeypatch, tmp_path):
    params = {**cap.params_for(tool), **GOLDEN_PARAMS.get(tool, {}), **LAYER}
    body, _ = cap.post(monkeypatch, tmp_path, tool, params, 'contiguous')
    assert body['result_status'] == 'success', body
    filters = body['raw_report']['entry_filters']
    assert filters['predicates'] == {'pattern_confirm': FilterConfig.parse(LAYER).report()['predicates']['pattern_confirm']}
    assert filters['direction'] == params.get('direction', 'long')
    counts = body['assumptions']['pattern_confirm']
    assert counts['registered'] > 0, counts
    discarded = sum(counts['discarded'].values())
    assert counts['registered'] == counts['submitted'] + discarded + counts['pending_at_end']
    assert counts['confirmed'] == counts['submitted'] + discarded - counts['discarded']['unconfirmed']
    assert len(body['trades']) <= counts['submitted']


def test_volume_breakout_golden_defaults_have_no_signal_to_register(monkeypatch, tmp_path):
    body, _ = cap.post(monkeypatch, tmp_path, 'volume_breakout', {**cap.params_for('volume_breakout'), **LAYER},
                       'contiguous')
    assert body['assumptions']['pattern_confirm']['registered'] == 0 and body['trades'] == []


def test_switch_off_adds_no_pattern_confirm_assumption(monkeypatch, tmp_path):
    body, _ = cap.post(monkeypatch, tmp_path, 'ema_cross', cap.params_for('ema_cross'), 'contiguous')
    assert 'pattern_confirm' not in body['assumptions'] and 'entry_filters' not in body['raw_report']


# --- hand frames ----------------------------------------------------------------------------------

EMA = dict(ema_fast=2, ema_slow=3)
S = 11  # ema_cross signal bar: bar 10 dips (fast < slow), bar 11 jumps (fast crosses above slow)


def ohlc(closes, start='2026-01-01'):
    closes = [float(c) for c in closes]
    opens = [closes[0]] + [c + .1 for c in closes[:-1]]
    return pd.DataFrame(dict(Open=opens, High=[max(o, c) + .5 for o, c in zip(opens, closes)],
                             Low=[min(o, c) - .5 for o, c in zip(opens, closes)], Close=closes,
                             Volume=[100.0] * len(closes)),
                        index=pd.date_range(start, periods=len(closes), freq='h'))


def ema_cross_frame(after, lead=()):
    """lead bars, ten flat bars at 100, dip 99 at bar 10 of that block, signal 102, then `after`."""
    return ohlc([*lead, *[100] * 10, 99, 102, *after])


def run(tool, params, data):
    stats = Backtest(data, build(tool, params), cash=1_000_000, commission=0, finalize_trades=True).run()
    trades = stats['_trades']
    return trades, stats['_strategy']


def test_ema_cross_hand_frame_signal_bar_and_off_state():
    data = ema_cross_frame([103] * 6)
    assert data['High'].iloc[S] == 102.5
    trades, strategy = run('ema_cross', EMA, data)
    assert list(trades['EntryBar']) == [S + 1] and trades['EntryPrice'].iloc[0] == data['Open'].iloc[S + 1]
    assert strategy._pattern_confirm_queue is None


def test_ema_cross_confirmed_entry_fills_at_the_open_after_the_confirming_bar():
    data = ema_cross_frame([103] * 6)  # close[12] = 103 > High[11] = 102.5 -> k = 12
    trades, strategy = run('ema_cross', {**EMA, **LAYER}, data)
    assert list(trades['EntryBar']) == [S + 2]
    assert trades['EntryPrice'].iloc[0] == data['Open'].iloc[S + 2] == 103.1
    assert strategy._pattern_confirm_queue.report() == dict(
        registered=1, confirmed=1, submitted=1, pending_at_end=0, discarded=dict(
            unconfirmed=0, superseded_by_later_signal=0, position_or_order_open=0, no_next_open=0,
            time_gate=0, filter_gate=0, template_gate=0))


@pytest.mark.parametrize('after, bars', [
    ([102.5, 102.5, 102.5, 102.5], 2),  # closes only equal High[s]: strict comparison never confirms
    ([102.4, 102.4, 103, 103], 2),       # breaks out at s+3, outside N = 2
    ([102.4, 102.4, 102.4, 102.4, 102.4, 103, 103], 5),  # breaks out at s+6, outside N = 5
])
def test_ema_cross_unconfirmed_signal_leaves_no_trade(after, bars):
    params = {**EMA, **LAYER, 'filter_pattern_confirm_bars': bars}
    trades, strategy = run('ema_cross', params, ema_cross_frame(after))
    assert len(trades) == 0
    report = strategy._pattern_confirm_queue.report()
    assert (report['registered'], report['confirmed'], report['discarded']['unconfirmed']) == (1, 0, 1)


def test_ema_cross_within_n_confirms_on_the_last_window_bar():
    trades, _ = run('ema_cross', {**EMA, **LAYER, 'filter_pattern_confirm_bars': 3},
                    ema_cross_frame([102.4, 102.4, 103, 103]))
    assert list(trades['EntryBar']) == [S + 4]


def test_ema_cross_filter_gate_closed_on_the_confirming_bar_discards():
    # 60 bars at 130 keep EMA(50) above every close after the drop to 100.
    lead = [130] * 60
    data = ema_cross_frame([103] * 6, lead=lead)
    params = {**EMA, **LAYER, 'filter_ema_enabled': True, 'filter_ema_period': 50}
    trades, strategy = run('ema_cross', params, data)
    assert len(trades) == 0
    report = strategy._pattern_confirm_queue.report()
    assert (report['registered'], report['confirmed'], report['discarded']['filter_gate']) == (1, 1, 1)
    # same frame, switch off: the EMA filter alone already blocks the signal bar
    off = {**EMA, 'filter_layer_enabled': True, 'filter_ema_enabled': True, 'filter_ema_period': 50}
    assert len(run('ema_cross', off, data)[0]) == 0


def post_frame(monkeypatch, tmp_path, tool, params, data, prefix):
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *args: data.copy())
    monkeypatch.setattr(p, '_fetch_template_warmup',
                        lambda *args: prefix.tail(args[5]).copy() if args[5] > 0 else prefix.iloc[:0].copy())
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **kw: None)
    request = dict(run_id='patconf2a', provider_tool_id=PREFIX + tool, provider_params=params, symbol='BTCUSDT',
                   market='futures', timeframe='1h', start_at=int(data.index[0].timestamp()),
                   end_at=int((data.index[-1] + pd.Timedelta('1h')).timestamp()), initial_capital='10000',
                   fee_bps='0', slippage_bps='0')
    return TestClient(p.app).post('/cutie/backtest', json={'backtest': request}).json()


def http_frames(after=(103,) * 6):
    full = ema_cross_frame(after, lead=[100] * 200)
    full.index = pd.date_range(pd.Timestamp('2026-01-01 11:00') - pd.Timedelta(hours=200 + S), periods=len(full),
                               freq='h')
    prefix, data = full.iloc[:200], full.iloc[200:]
    assert data.index[S] == pd.Timestamp('2026-01-01 11:00')  # decision (bar close) 12:00, k decides 13:00
    return prefix, data


@pytest.mark.parametrize('session_end, entry_bar', [('12:30', None), ('13:30', S + 2)])
def test_time_gate_is_judged_on_the_confirming_bar(session_end, entry_bar, monkeypatch, tmp_path):
    prefix, data = http_frames()
    params = {**EMA, **LAYER, 'time_layer_enabled': True, 'time_session_start': '11:30',
              'time_session_end': session_end}
    body = post_frame(monkeypatch, tmp_path, 'ema_cross', params, data, prefix)
    assert body['result_status'] == 'success', body
    counts = body['assumptions']['pattern_confirm']
    assert (counts['registered'], counts['confirmed']) == (1, 1)
    if entry_bar is None:
        # the signal bar decides inside the session, the confirming bar outside: discarded at k
        assert body['trades'] == [] and counts['discarded']['time_gate'] == 1
    else:
        assert [t['opened_at'] for t in body['trades']] == [int(data.index[entry_bar].timestamp())]
        assert body['trades'][0]['entry_price'] == str(data['Open'].iloc[entry_bar])


def test_time_gate_off_state_trades_the_same_signal_inside_the_session(monkeypatch, tmp_path):
    prefix, data = http_frames()
    params = {**EMA, 'time_layer_enabled': True, 'time_session_start': '11:30', 'time_session_end': '12:30'}
    body = post_frame(monkeypatch, tmp_path, 'ema_cross', params, data, prefix)
    assert [t['opened_at'] for t in body['trades']] == [int(data.index[S + 1].timestamp())]


# ema_pullback direction=short on the mirrored compat frame: the first off-state short gives s.
PULLBACK = dict(ema_fast=5, ema_slow=30, pullback_tolerance_pct=1, direction='short')


def mirrored():
    data = compat.frame()
    return pd.DataFrame(dict(Open=200 - data['Open'], High=200 - data['Low'], Low=200 - data['High'],
                             Close=200 - data['Close'], Volume=data['Volume']), index=data.index)


def pullback_signal():
    data = mirrored()
    trades, _ = run('ema_pullback', PULLBACK, data)
    assert len(trades) > 0 and trades['Size'].iloc[0] < 0
    return data, int(trades['EntryBar'].iloc[0]) - 1


def pullback_frame(after):
    data, s = pullback_signal()
    head = data.iloc[:s + 1]
    low = head['Low'].iloc[-1]
    index = pd.date_range(head.index[0], periods=s + 1 + len(after), freq='h')
    rows = [dict(Open=head['Close'].iloc[-1] - .1 if i == 0 else low + after[i - 1] - .1,
                 Close=low + d) for i, d in enumerate(after)]
    tail = pd.DataFrame([dict(Open=r['Open'], High=max(r['Open'], r['Close']) + .2,
                              Low=min(r['Open'], r['Close']) - .2, Close=r['Close'], Volume=100.0)
                         for r in rows])
    return pd.concat([head, tail]).set_axis(index), s, low


def test_ema_pullback_short_confirmed_entry_is_later_at_the_next_open():
    data, s, low = pullback_frame([-1, -1.5, -1.5])  # close[s+1] < Low[s] -> k = s+1
    off, _ = run('ema_pullback', PULLBACK, data)
    on, strategy = run('ema_pullback', {**PULLBACK, **LAYER}, data)
    assert int(off['EntryBar'].iloc[0]) == s + 1
    assert int(on['EntryBar'].iloc[0]) == s + 2 and on['Size'].iloc[0] < 0
    assert on['EntryPrice'].iloc[0] == data['Open'].iloc[s + 2]
    report = strategy._pattern_confirm_queue.report()
    assert report['registered'] >= 1 and report['submitted'] == 1


@pytest.mark.parametrize('after', [
    [0, 0, 0],            # closes only equal Low[s]
    [.2, .2, -2, -2],     # breaks down at s+3, outside N = 2
])
def test_ema_pullback_short_unconfirmed_signal_leaves_no_trade(after):
    data, s, _ = pullback_frame(after)
    on, strategy = run('ema_pullback', {**PULLBACK, **LAYER}, data)
    assert len(on) == 0
    report = strategy._pattern_confirm_queue.report()
    assert report['confirmed'] == 0 and report['discarded']['unconfirmed'] >= 1


# --- mixin bar-by-bar advance == pattern_confirm_entries ----------------------------------------------

def scripted(signals, direction, opened):
    """A template whose next() only calls _risk_buy/_risk_sell; _risk_open records instead of ordering."""
    class Scripted(p._FixedRiskMixin, Strategy):
        def init(self):
            self._risk_init()

        def next(self):
            if signals[len(self.data) - 1]:
                (self._risk_buy if direction == 'long' else self._risk_sell)()

        def _risk_open(self, is_long):
            opened.append((len(self.data) - 1, is_long))
            return None

    Scripted._filter_config = FilterConfig.parse(LAYER)
    return p._pattern_confirm_strategy(Scripted)


@pytest.mark.parametrize('seed', range(12))
def test_mixin_bar_by_bar_matches_pattern_confirm_entries(seed):
    rng = random.Random(seed)
    n, bars = 120, rng.randint(1, 5)
    direction = 'short' if seed % 2 else 'long'
    closes = np.cumsum([rng.uniform(-1, 1) for _ in range(n)]) + 100
    data = ohlc(closes)
    data['High'] = data['High'] + [rng.choice([0, 0, .3, 1]) for _ in range(n)]
    signals = [rng.random() < .3 for _ in range(n)]
    signals[0] = False  # backtesting.py starts next() at bar 1
    opened = []
    cls = scripted(signals, direction, opened)
    cls._filter_config = FilterConfig.parse({**LAYER, 'filter_pattern_confirm_bars': bars})
    stats = Backtest(data, cls, cash=100_000).run()
    entries, _ = pattern_confirm_entries(signals, data['High'], data['Low'], data['Close'],
                                         direction=direction, bars=bars)
    # a confirmation on the last bar has no next open: discarded before the entry path is reached
    assert [bar for bar, _ in opened] == list(np.flatnonzero(entries[:-1]))
    assert stats['_strategy']._pattern_confirm_queue.report()['discarded']['no_next_open'] == int(entries[-1])
    assert {is_long for _, is_long in opened} <= {direction == 'long'}


def test_one_order_per_bar_when_two_signals_confirm_and_the_template_signals_again():
    # bars 4 and 5 both confirm at bar 6; bar 6 registers a third signal that confirms at bar 7.
    closes = [100, 100, 100, 100, 100, 100, 103, 106, 106, 106]
    data = ohlc(closes)
    signals = [False] * 10
    signals[4] = signals[5] = signals[6] = True
    opened = []
    cls = scripted(signals, 'long', opened)
    stats = Backtest(data, cls, cash=100_000).run()
    assert [bar for bar, _ in opened] == [6, 7]
    report = stats['_strategy']._pattern_confirm_queue.report()
    assert (report['registered'], report['confirmed'], report['submitted']) == (3, 3, 2)
    assert report['discarded']['superseded_by_later_signal'] == 1


def test_real_order_on_bar_k_discards_the_signal_registered_and_confirmed_while_it_is_open():
    # orders reach the broker: the bar-6 signal confirms at bar 7 while the bar-6 order holds a position.
    closes = [100, 100, 100, 100, 100, 100, 103, 106, 106, 106]
    signals = [False] * 10
    signals[4] = signals[6] = True

    class Ordered(p._FixedRiskMixin, Strategy):
        # 10% per order: a second same-bar order would also fill instead of failing on margin.
        _risk = dict(position_size_pct=0.1)

        def init(self):
            self._risk_init()

        def next(self):
            if signals[len(self.data) - 1]:
                self._risk_buy()

    Ordered._filter_config = FilterConfig.parse(LAYER)
    stats = Backtest(ohlc(closes), p._pattern_confirm_strategy(Ordered), cash=100_000,
                     finalize_trades=True).run()
    assert list(stats['_trades']['EntryBar']) == [7]
    report = stats['_strategy']._pattern_confirm_queue.report()
    assert report['submitted'] == 1 and report['discarded']['position_or_order_open'] == 1


@pytest.mark.parametrize('seed', range(40))
def test_queue_matches_engine_source_on_random_sequences(seed):
    rng = random.Random(1000 + seed)
    n, bars, direction = rng.randint(1, 60), rng.randint(1, 5), rng.choice(['long', 'short'])
    close = [rng.choice([100.0, 101.0, 102.0, 103.0, float('nan')]) for _ in range(n)]
    high = [rng.choice([100.0, 101.0, 102.0, float('nan')]) for _ in range(n)]
    low = [rng.choice([100.0, 101.0, 102.0, float('inf')]) for _ in range(n)]
    signals = [rng.random() < .4 for _ in range(n)]
    queue = PatternConfirmQueue(bars)
    got = []
    for k in range(n):
        hit = queue.advance(k, close[k])
        got.append(-1 if hit is None else hit[0])
        if signals[k]:
            queue.register(k, direction, (low if direction == 'short' else high)[k])
    _, source = pattern_confirm_entries(signals, high, low, close, direction=direction, bars=bars)
    assert got == list(source)
    report = queue.report()
    assert report['registered'] == sum(signals)
    assert report['registered'] == report['confirmed'] + report['discarded']['unconfirmed'] + report['pending_at_end']


# --- review fixes: judging bar of the filter gate, tail confirmation --------------------------------

def masked(params, allow):
    """ema_cross with the filter mask overwritten at the given main-range bars (pattern-only mask is all True)."""
    class Masked(build('ema_cross', params)):
        def init(self):
            super().init()
            self._filter_mask = self._filter_mask.copy()
            for bar, ok in allow.items():
                self._filter_mask[self._warmup_bars + bar] = ok
    return Masked


@pytest.mark.parametrize('s_ok, k_ok, entry', [(True, False, None), (False, True, S + 2)])
def test_filter_gate_is_judged_on_the_confirming_bar_not_the_signal_bar(s_ok, k_ok, entry):
    data = ema_cross_frame([103] * 6)  # s = 11 confirms at k = 12
    stats = Backtest(data, masked({**EMA, **LAYER}, {S: s_ok, S + 1: k_ok}), cash=1_000_000,
                     finalize_trades=True).run()
    report = stats['_strategy']._pattern_confirm_queue.report()
    assert (report['registered'], report['confirmed']) == (1, 1)
    if entry is None:
        assert len(stats['_trades']) == 0 and report['discarded']['filter_gate'] == 1
    else:
        assert list(stats['_trades']['EntryBar']) == [entry] and report['submitted'] == 1
        assert report['discarded']['filter_gate'] == 0


def test_signal_on_the_last_but_one_bar_confirming_on_the_last_bar_is_no_next_open():
    data = ema_cross_frame([103])  # s = 11 = n - 2, close[12] = 103 > High[11] confirms on the tail
    assert len(data) == S + 2
    trades, strategy = run('ema_cross', {**EMA, **LAYER}, data)
    assert len(trades) == 0
    assert strategy._pattern_confirm_queue.report() == dict(
        registered=1, confirmed=1, submitted=0, pending_at_end=0, discarded=dict(
            unconfirmed=0, superseded_by_later_signal=0, position_or_order_open=0, no_next_open=1,
            time_gate=0, filter_gate=0, template_gate=0))


@pytest.mark.parametrize('time_layer', [False, True])
def test_tail_confirmation_counts_no_next_open_in_assumptions(time_layer, monkeypatch, tmp_path):
    prefix, data = http_frames(after=(103,))
    params = {**EMA, **LAYER}
    if time_layer:  # the confirming bar decides at 13:00, inside the session: only the tail refuses it
        params.update(time_layer_enabled=True, time_session_start='11:30', time_session_end='13:30')
    body = post_frame(monkeypatch, tmp_path, 'ema_cross', params, data, prefix)
    assert body['result_status'] == 'success', body
    counts = body['assumptions']['pattern_confirm']
    assert body['trades'] == [] and (counts['registered'], counts['confirmed'], counts['submitted']) == (1, 1, 0)
    assert counts['discarded'] == dict(unconfirmed=0, superseded_by_later_signal=0, position_or_order_open=0,
                                       no_next_open=1, time_gate=0, filter_gate=0, template_gate=0)


# --- one hand frame per state-machine shape: state condition, state flag, crossover, both ---------------
# Rows up to the first off-state signal bar s of the compat frame are kept; bar s + 1 closes 1 beyond the
# level (High[s] long, Low[s] short) and so confirms at k = s + 1; the flat tail repeats that close.

SHAPES = [('ema_trend_rsi', {}), ('ema_rsi_pullback', {}), ('macd', {}),
          ('cci_rsi', dict(direction='long')), ('cci_rsi', dict(direction='short'))]


def shape_frame(tool, params, tail=6):
    data = compat.frame()
    off, _ = run(tool, params, data)
    short = params.get('direction') == 'short'
    assert (off['Size'].iloc[0] < 0) == short
    s = int(off['EntryBar'].iloc[0]) - 1
    head = data.iloc[:s + 1]
    close = (head['Low'].iloc[-1] - 1) if short else (head['High'].iloc[-1] + 1)
    opens = [head['Close'].iloc[-1]] + [close] * (tail - 1)
    rows = pd.DataFrame(dict(Open=opens, High=[max(o, close) + .2 for o in opens],
                             Low=[min(o, close) - .2 for o in opens], Close=[close] * tail,
                             Volume=[100.0] * tail))
    index = pd.date_range(head.index[0], periods=s + 1 + tail, freq='h')
    return pd.concat([head, rows]).set_axis(index), s, short


@pytest.mark.parametrize('tool, params', SHAPES, ids=[f"{t}-{q.get('direction', 'long')}" for t, q in SHAPES])
def test_state_machine_shapes_enter_one_bar_after_the_confirming_bar(tool, params):
    data, s, short = shape_frame(tool, params)
    off, _ = run(tool, params, data)
    on, strategy = run(tool, {**params, **LAYER}, data)
    assert int(off['EntryBar'].iloc[0]) == s + 1  # off: the signal bar s fills at s + 1
    # on: s only registers, k = s + 1 confirms, the order fills at the open of k + 1
    assert int(on['EntryBar'].iloc[0]) == s + 2 and (on['Size'].iloc[0] < 0) == short
    assert on['EntryPrice'].iloc[0] == data['Open'].iloc[s + 2]
    report = strategy._pattern_confirm_queue.report()
    # signals the template repeats inside the window never add a second order
    assert report['submitted'] == len(on) == 1
    assert report['registered'] == report['submitted'] + sum(report['discarded'].values()) + report['pending_at_end']


# --- the template signals again inside the window, then a second trade after the first exit ------------
# Tail closes are offsets from Close[s]; each tail bar opens at the previous close, so an order sent on the
# confirming bar k fills at Open[k + 1] == Close[k]. Offsets picked so that, by hand:
#   macd: s + 1 drops 10 (MACD back under its signal), s + 2 rises 15 -> Close[s] + 5 > High[s] confirms s at
#         k1 = s + 2 while crossing up again (registers s + 2 inside the window); -15, -15 cross down (exit),
#         +15, +15 cross up at s + 8 (registers), s + 9 closes 2 above it -> k2 = s + 9.
#   cci_rsi long: s + 1 stays oversold (registers), s + 2 closes above High[s] and High[s + 1] -> k1 = s + 2;
#         RSI > 50 exits, the slide re-signals from s + 7, a later bar closes above that level -> k2 = s + 11.
#   cci_rsi short: mirror; s + 1 still overbought (registers), k1 = s + 2; re-signals at s + 8, k2 = s + 11.
REPEATS = [
    ('macd', {}, [-10, 5, 5, 5, -10, -25, -10, 5, 7, 7, 7], 2, 8, 9),
    ('cci_rsi', dict(direction='long'),
     [.2, 4.2, 6.2, 4.2, 2.2, -1.8, -5.8, -13.8, -19.8, -13.8, -9.8, -9.8, -9.8, -9.8], 2, 7, 11),
    ('cci_rsi', dict(direction='short'), [2, -4, -6, -6, -5, -7, 1, 9, 15, 9, 3, 3, 3, 3], 2, 8, 11),
]


def repeat_frame(tool, params, offsets):
    data = compat.frame()
    off, _ = run(tool, params, data)
    s = int(off['EntryBar'].iloc[0]) - 1
    head = data.iloc[:s + 1]
    closes = [round(head['Close'].iloc[-1] + x, 2) for x in offsets]
    opens = [head['Close'].iloc[-1]] + closes[:-1]
    rows = pd.DataFrame(dict(Open=opens, High=[max(o, c) + .2 for o, c in zip(opens, closes)],
                             Low=[min(o, c) - .2 for o, c in zip(opens, closes)], Close=closes,
                             Volume=[100.0] * len(closes)))
    index = pd.date_range(head.index[0], periods=s + 1 + len(rows), freq='h')
    return pd.concat([head, rows]).set_axis(index), s


def registering(monkeypatch):
    bars = []
    register = PatternConfirmQueue.register

    def record(self, bar, direction, level):
        bars.append(bar)
        register(self, bar, direction, level)
    monkeypatch.setattr(PatternConfirmQueue, 'register', record)
    return bars


@pytest.mark.parametrize('tool, params, offsets, k1, second_signal, k2', REPEATS,
                         ids=[f"{t}-{q.get('direction', 'long')}" for t, q, *_ in REPEATS])
def test_repeat_inside_the_window_gives_one_order_and_the_second_entry_is_hand_computed(
        tool, params, offsets, k1, second_signal, k2, monkeypatch):
    data, s = repeat_frame(tool, params, offsets)
    short = params.get('direction') == 'short'
    level = data['Low'].iloc[s] if short else data['High'].iloc[s]
    assert (data['Close'].iloc[s + k1] < level) if short else (data['Close'].iloc[s + k1] > level)
    # A: the frame up to the second signal -- the window repeat registers but never adds an order
    bars = registering(monkeypatch)
    first, strategy = run(tool, {**params, **LAYER}, data.iloc[:s + second_signal])
    report = strategy._pattern_confirm_queue.report()
    assert bars[0] == s and any(s < bar <= s + k1 for bar in bars), bars
    assert report['registered'] >= 2 and report['submitted'] == 1 == len(first)
    assert int(first['EntryBar'].iloc[0]) == s + k1 + 1
    assert first['EntryPrice'].iloc[0] == data['Open'].iloc[s + k1 + 1] == data['Close'].iloc[s + k1]
    # B: the whole frame -- after the first exit a second signal confirms at s + k2
    bars.clear()
    on, strategy = run(tool, {**params, **LAYER}, data)
    report = strategy._pattern_confirm_queue.report()
    assert s + second_signal in bars and report['submitted'] == len(on) == 2
    assert list(on['EntryBar']) == [s + k1 + 1, s + k2 + 1]
    assert int(on['ExitBar'].iloc[0]) < s + second_signal
    assert list(on['EntryPrice']) == [data['Close'].iloc[s + k1], data['Close'].iloc[s + k2]]
    assert list(on['Size'] < 0) == [short, short]


# --- isolated liquidation bar on the confirmation path ------------------------------------------------
# 10x isolated long: bars 0-4 flat 100; s = 4 (High 100.5) confirms at bar 5 (close 103), filled at bar 6
# open 103.1; bar 7 lows 80 < 0.9 * 103.1 and liquidates the position. The confirmation step runs before
# the template's next(), so on bar 7 the position is still open: a signal confirming there is discarded as
# position_or_order_open and the isolated_liquidation_bar gate is never reached.
LIQUIDATION_ROWS = [(100, 100.5, 99.5, 100)] * 5 + [
    (100, 103.5, 99.5, 103), (103.1, 103.6, 102.6, 103.2), (103.2, 104.5, 80, 104),
    (104, 105.5, 103.5, 105), (105, 105.5, 104.5, 105), (105, 105.5, 104.5, 105)]


def leveraged(signals, confirm):
    class Lev(p._FixedRiskMixin, Strategy):
        _risk = dict(leverage=10, position_size_pct=0.1)

        def init(self):
            self._risk_init()

        def next(self):
            self._risk_check_exit()  # registers on the liquidation bar too, after the liquidation
            if len(self.data) - 1 in signals:
                self._risk_buy()

    if not confirm:
        return Lev
    Lev._filter_config = FilterConfig.parse({**LAYER, 'filter_pattern_confirm_bars': 3})
    return p._pattern_confirm_strategy(Lev)


def run_leveraged(signals, confirm):
    data = pd.DataFrame(LIQUIDATION_ROWS, columns=['Open', 'High', 'Low', 'Close'],
                        index=pd.date_range('2026-01-01', periods=len(LIQUIDATION_ROWS), freq='h'))
    data['Volume'] = 100.0
    stats = Backtest(data, leveraged(signals, confirm), cash=100_000, margin=0.1, finalize_trades=True).run()
    return stats['_trades'], stats['_strategy'], data


def test_signal_confirming_on_the_liquidation_bar_counts_as_position_or_order_open():
    # s2 = 5 (High 103.5) confirms on the liquidation bar 7 (close 104): no entry for it
    trades, strategy, _ = run_leveraged({4, 5}, confirm=True)
    assert strategy._isolated_blocked_bar == 7 and len(strategy._isolated_liquidations) == 1
    assert list(trades['EntryBar']) == [6]
    report = strategy._pattern_confirm_queue.report()
    assert (report['registered'], report['confirmed'], report['submitted']) == (2, 2, 1)
    assert report['discarded'] == dict(unconfirmed=0, superseded_by_later_signal=0, position_or_order_open=1,
                                       no_next_open=0, time_gate=0, filter_gate=0, template_gate=0)


def test_signal_on_the_liquidation_bar_confirming_on_a_normal_bar_enters():
    # s2 = 7 (High 104.5, registered after the liquidation) confirms at bar 8 (close 105): fills at Open[9]
    trades, strategy, data = run_leveraged({4, 7}, confirm=True)
    assert strategy._isolated_blocked_bar == 7
    assert list(trades['EntryBar']) == [6, 9] and trades['EntryPrice'].iloc[1] == data['Open'].iloc[9] == 105
    report = strategy._pattern_confirm_queue.report()
    assert (report['registered'], report['submitted'], report['discarded']['position_or_order_open']) == (2, 2, 0)
    # switch off, same signals: the liquidation-bar gate of _risk_open still refuses the bar-7 entry
    off, _, _ = run_leveraged({4, 7}, confirm=False)
    assert list(off['EntryBar']) == [5]


def test_isolated_liquidation_gate_result_on_the_confirmation_path_is_counted_not_raised(monkeypatch):
    # unreachable in practice; if _risk_open ever returns it here, it lands in position_or_order_open
    monkeypatch.setattr(p._FixedRiskMixin, '_risk_open', lambda self, is_long: 'isolated_liquidation_bar')
    trades, strategy, _ = run_leveraged({4}, confirm=True)
    report = strategy._pattern_confirm_queue.report()
    assert len(trades) == 0 and report['submitted'] == 0 and report['discarded']['position_or_order_open'] == 1
    assert 'isolated_liquidation_bar' not in report['discarded']
