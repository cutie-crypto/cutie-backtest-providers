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
    assert wired == WIRED and len(wired) == 22


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
            isolated_liquidation_bar=0, time_gate=0, filter_gate=0))


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
            isolated_liquidation_bar=0, time_gate=0, filter_gate=0))


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
                                       no_next_open=1, isolated_liquidation_bar=0, time_gate=0, filter_gate=0)


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
