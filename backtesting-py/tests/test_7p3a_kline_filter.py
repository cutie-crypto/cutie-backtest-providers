"""7-P3a: six K-line pattern templates honour the single-position entry filter layer.

Every bar list below is hand-built; the expected entry bar / open price are literals worked out
from the pattern definitions in strategy_candle_patterns.py (see per-case comments). The EMA oracle
is a plain scalar recursion, independent of strategy_entry_filters.
"""
from __future__ import annotations
import sys
from pathlib import Path

import pandas as pd
import pytest
from backtesting import Backtest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p
from strategy_entry_filters import FilterConfig, HigherTimeframeContext

PREFIX = [(120, 120.5, 119.7, 120.2)] * 22 + [(100, 100.5, 99.7, 100.2)] * 4  # bars 0..25, flat bullish
FILTER_ON = {'filter_layer_enabled': True, 'filter_ema_enabled': True}

# name -> (params, bars from index 26, signal bar k, entry-bar open)
CASES = {
    # 26 bearish body 2.0; 27 engulfs (open 98.0 <= 98.2, close 101.0 >= 100.2, body 3.0 >= 1.2*2.0).
    'bullish_engulfing': ({'position_filter': False}, [
        (100.2, 100.4, 98, 98.2), (98.0, 101.2, 97.9, 101.0),
        (101.1, 101.3, 99.9, 100.0), (100.0, 100.4, 99.8, 100.1), (100.1, 100.5, 99.9, 100.2)], 27, 101.1),
    # 26 hammer: body 0.5, lower shadow 3.0 >= 1.0, upper 0.1 <= .15*3.6, body in top third.
    'hammer_pin_bar': ({'position_filter': False}, [
        (100.0, 100.6, 97.0, 100.5), (100.6, 100.8, 100.0, 100.3),
        (100.3, 100.6, 100.0, 100.2), (100.2, 100.5, 99.9, 100.3)], 26, 100.6),
    # 26 big bearish body 2.0 (>= 1.5*0.2); 27 middle body .3 <= .6 closing below 100.0; 28 closes 101.4 > mid 101.0.
    'morning_star': ({}, [
        (102.0, 102.2, 99.8, 100.0), (99.9, 100.0, 99.2, 99.6), (99.7, 101.6, 99.6, 101.4),
        (101.5, 101.7, 100.4, 100.6), (100.6, 101.0, 100.2, 100.5)], 28, 101.5),
    # 26..28 white soldiers: opens inside previous bodies, closes 101.0 < 101.9 < 102.8, tiny upper shadows.
    'three_white_soldiers': ({'position_filter': False}, [
        (100.2, 101.1, 100.1, 101.0), (100.8, 102.0, 100.7, 101.9), (101.7, 102.9, 101.6, 102.8),
        (102.9, 103.0, 101.9, 102.0), (102.0, 102.4, 101.8, 102.1)], 28, 102.9),
    # 26 doji (body .1 <= .2 of span 2); 27 closes 102 above doji high 101 and confirms.
    'bullish_doji_reversal': ({'position_filter': False}, [
        (100.0, 101.0, 99.0, 100.1), (100.5, 102.5, 100.4, 102.0),
        (102.1, 102.3, 100.2, 100.4), (100.4, 100.8, 100.1, 100.5)], 27, 102.1),
    # 26 mother [98,102]; 27 inside [99,101.5]; 28 closes 103.0 > mother high 102 => signal.
    'inside_bar_breakout': ({}, [
        (100.0, 102.0, 98.0, 101.0), (100.5, 101.5, 99.0, 100.8), (101.0, 103.2, 100.9, 103.0),
        (103.1, 103.4, 101.8, 102.0), (102.0, 102.6, 101.5, 102.2)], 28, 103.1),
}
NAMES = list(CASES)


def frame(name):
    rows = PREFIX + CASES[name][1]
    data = pd.DataFrame(rows, columns=['Open', 'High', 'Low', 'Close'], dtype=float,
                        index=pd.date_range('2026-01-01', periods=len(rows), freq='h'))
    data['Volume'] = 100.0
    return data


def ema_mask(closes, period):
    """Scalar recursion: close > EMA (pandas adjust=False, seed = first close)."""
    alpha = 2 / (period + 1)
    ema, out = closes[0], [False]
    for close in closes[1:]:
        ema = alpha * close + (1 - alpha) * ema
        out.append(close > ema)
    return out


def run(name, extra):
    built = p.TOOL_SPECS['local.backtesting_py.' + name]['build']({**CASES[name][0], **extra})
    return Backtest(frame(name), built['strategy'], cash=100000, exclusive_orders=True, finalize_trades=True).run()['_trades']


@pytest.mark.parametrize('name', NAMES)
def test_control_pattern_enters_next_open_without_filter(name):
    _, _, k, entry_open = CASES[name]
    trades = run(name, {})
    assert [int(b) for b in trades.EntryBar] == [k + 1]
    assert float(trades.EntryPrice.iloc[0]) == entry_open


@pytest.mark.parametrize('name', NAMES)
def test_filter_true_at_signal_bar_enters_next_open(name):
    _, _, k, entry_open = CASES[name]
    closes = list(frame(name).Close)
    mask = ema_mask(closes, 2)
    # Data property: allowed at the signal bar but NOT on the following (entry) bar, so a
    # decision taken one bar late would block this entry.
    assert mask[k] and not mask[k + 1]
    trades = run(name, {**FILTER_ON, 'filter_ema_period': 2})
    assert [int(b) for b in trades.EntryBar] == [k + 1]
    assert float(trades.EntryPrice.iloc[0]) == entry_open


@pytest.mark.parametrize('name', NAMES)
def test_filter_false_at_signal_bar_blocks_entry(name):
    _, _, k, _ = CASES[name]
    # EMA30 still sits near the 120 plateau => close <= EMA at the signal bar.
    mask = ema_mask(list(frame(name).Close), 30)
    assert not mask[k]
    assert len(run(name, {**FILTER_ON, 'filter_ema_period': 30})) == 0


@pytest.mark.parametrize('name', NAMES)
def test_wiring_flags_and_long_only(name):
    tool = p.TOOL_SPECS['local.backtesting_py.' + name]
    assert getattr(tool['build'], '_supports_entry_filters', False)
    assert 'filter_layer_enabled' in tool['param_schema_properties']
    assert 'local.backtesting_py.' + name not in p.FILTER_LAYER_UNWIRED_TOOLS
    assert issubclass(tool['build']({})['strategy'], p._FilterLayerMixin)
    with pytest.raises(ValueError, match='^INVALID_PARAMS'):
        tool['build']({**FILTER_ON, 'direction': 'short'})


def test_catalog_filter_keys_only_on_wired_templates():
    for tool_id, tool in p.TOOL_SPECS.items():
        has = any(key.startswith('filter_') for key in tool['param_schema_properties'])
        if tool_id.removeprefix('local.backtesting_py.') in NAMES:
            assert has, tool_id
        elif tool_id in p.FILTER_LAYER_UNWIRED_TOOLS:
            assert not has, tool_id
    # 集成 E：7-P3a 移出 6 个后 10，SHORT-PAT-3 新增 chan_3sell 进未接名单，合并后 11；7-P3b 移出 red_streak_rsi 后 11 - 1 = 10；
    # 7-P3b2 移出双底、头肩底、ORB、亚洲区间、日历后 10 - 5 = 5
    assert len(p.FILTER_LAYER_UNWIRED_TOOLS) == 5
    assert not set('local.backtesting_py.' + n for n in NAMES) & set(p.FILTER_LAYER_UNWIRED_TOOLS)


def coarse_source():
    closes = [100] * 10 + [110, 90, 10000]
    rows = [(c, c + 1, c - 1, c) for c in closes]
    data = pd.DataFrame(rows, columns=['Open', 'High', 'Low', 'Close'], dtype=float,
                        index=pd.date_range('2026-01-01', periods=len(rows), freq='4h'))
    data['Volume'] = 100.0
    return data


def run_mtf(start):
    name = 'bullish_engulfing'
    data = frame(name)
    data.index = pd.date_range(start, periods=len(data), freq='15min')
    config = FilterConfig.parse({**FILTER_ON, 'filter_ema_period': 2, 'filter_timeframe': '4h'})
    context = HigherTimeframeContext.build(config, '15m', data.index, lambda *args: coarse_source().copy(),
                                            p._supertrend_arrays)
    built = p.TOOL_SPECS['local.backtesting_py.' + name]['build'](
        {**CASES[name][0], **FILTER_ON, 'filter_ema_period': 2, 'filter_timeframe': '4h'})
    cls = built['strategy']
    cls._filter_context = context
    trades = Backtest(data, cls, cash=100000, exclusive_orders=True, finalize_trades=True).run()['_trades']
    return context, trades


def test_multi_timeframe_uses_only_closed_coarse_bar():
    # Coarse up candle opens 2026-01-02 16:00 and closes 20:00 => EMA2 allows from 20:00 on, never earlier.
    # Signal bar k=27: start 16:00 => opens 22:45 (allowed); start 12:00 => opens 18:45 (candle unclosed).
    context, trades = run_mtf('2026-01-02 16:00')
    assert bool(context.mask[27]) and [int(b) for b in trades.EntryBar] == [28]
    context, trades = run_mtf('2026-01-02 12:00')
    assert not bool(context.mask[27]) and len(trades) == 0
