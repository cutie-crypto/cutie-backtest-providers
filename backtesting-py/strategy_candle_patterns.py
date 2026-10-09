"""Causal OHLC geometry and six pattern families; no orders, schemas or engines.

All means/extrema use the preceding lookback bars, excluding the current bar.
Signals here are candidates at the last pattern close. Doji confirmation and
inside-bar breakout/expiry belong to the order template, not this library.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from numbers import Real
from typing import Mapping, Sequence


@dataclass(frozen=True)
class Geometry:
    open: tuple[float, ...]
    high: tuple[float, ...]
    low: tuple[float, ...]
    close: tuple[float, ...]
    body: tuple[float, ...]
    span: tuple[float, ...]
    upper: tuple[float, ...]
    lower: tuple[float, ...]
    mean_body: tuple[float | None, ...]
    mean_span: tuple[float | None, ...]
    lookback: int


@dataclass(frozen=True)
class PatternConfig:
    large_body_multiple: float = 1.5
    small_body_multiple: float = .5
    engulf_body_multiple: float = 1.2
    position_tolerance_pct: float = .5
    pin_shadow_multiple: float = 2
    pin_short_shadow_fraction: float = .15
    pin_body_zone_fraction: float = 1 / 3
    star_middle_body_fraction: float = .3
    soldier_shadow_fraction: float = .3
    doji_body_fraction: float = .1
    doji_span_multiple: float = .8

    def __post_init__(self):
        for name, value in self.__dict__.items():
            if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value) or value <= 0:
                raise ValueError(f'INVALID_PARAMS:{name} must be finite and positive')
            if name.endswith('_fraction') and value > 1:
                raise ValueError(f'INVALID_PARAMS:{name} must be <= 1')
        if self.small_body_multiple >= self.large_body_multiple:
            raise ValueError('INVALID_PARAMS:small body threshold must be below large body threshold')


@dataclass(frozen=True)
class PatternSeries:
    bullish_shape: tuple[bool, ...]
    bearish_shape: tuple[bool, ...]
    bullish: tuple[bool, ...]
    bearish: tuple[bool, ...]
    bullish_anchor: tuple[float | None, ...]
    bearish_anchor: tuple[float | None, ...]
    large_body: tuple[bool, ...]
    small_body: tuple[bool, ...]
    # Anchors are raw extrema, not buffered stop prices or trade instructions.


def _numbers(values: Sequence[float], *, nullable: bool = False) -> tuple:
    out = []
    for value in values:
        if nullable and value is None:
            out.append(None)
            continue
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError('INVALID_PARAMS:prices/indicators must be numbers')
        if nullable and math.isnan(value):
            out.append(None)
        elif not math.isfinite(value) or (not nullable and value <= 0):
            raise ValueError('INVALID_PARAMS:OHLC must be finite positive; indicators must be finite or missing')
        else:
            out.append(float(value))
    return tuple(out)


def candle_geometry(open: Sequence[float], high: Sequence[float], low: Sequence[float],
                    close: Sequence[float], *, lookback: int = 20) -> Geometry:
    """Lengths/order are validated; missing prior history produces None means."""
    if type(lookback) is not int or not 1 <= lookback <= 500:
        raise ValueError('INVALID_PARAMS:lookback must be an integer within 1-500')
    o, h, l, c = (_numbers(values) for values in (open, high, low, close))
    if len({len(values) for values in (o, h, l, c)}) != 1:
        raise ValueError('INVALID_PARAMS:OHLC lengths must match')
    if any(not lo <= min(op, cl) <= max(op, cl) <= hi for op, hi, lo, cl in zip(o, h, l, c)):
        raise ValueError('INVALID_PARAMS:OHLC ordering invalid')
    body = tuple(abs(cl - op) for op, cl in zip(o, c))
    span = tuple(hi - lo for hi, lo in zip(h, l))
    upper = tuple(hi - max(op, cl) for op, hi, cl in zip(o, h, c))
    lower = tuple(min(op, cl) - lo for op, lo, cl in zip(o, l, c))
    def prior_mean(values):
        return tuple(math.fsum(values[i - lookback:i]) / lookback if i >= lookback else None
                     for i in range(len(values)))
    return Geometry(o, h, l, c, body, span, upper, lower, prior_mean(body), prior_mean(span), lookback)


def candle_patterns(kind: str, geometry: Geometry, *, config: PatternConfig | None = None,
                    indicators: Mapping[str, Sequence[float | None]] | None = None) -> PatternSeries:
    """kind: engulfing, pin_bar, star, soldiers, doji, inside_bar.

    bullish_shape/bearish_shape are recognition only; bullish/bearish include
    the specified position filter. Caller supplies aligned causal EMA20/60,
    BB-lower/upper or RSI arrays. Missing indicators never satisfy a filter.
    No MACD/RSI/EMA lookahead guard can be inferred for caller-supplied arrays.
    """
    if kind not in ('engulfing', 'pin_bar', 'star', 'soldiers', 'doji', 'inside_bar'):
        raise ValueError('INVALID_PARAMS:unknown candle pattern')
    if config is not None and not isinstance(config, PatternConfig):
        raise ValueError('INVALID_PARAMS:config must be PatternConfig')
    cfg, g = config or PatternConfig(), geometry
    n = len(g.open)
    supplied = indicators if indicators is not None else {}
    if set(supplied) - {'ema20', 'ema60', 'bb_lower', 'bb_upper', 'rsi'}:
        raise ValueError('INVALID_PARAMS:unknown position indicator')
    lines = {name: _numbers(values, nullable=True) for name, values in supplied.items()}
    if any(len(values) != n for values in lines.values()):
        raise ValueError('INVALID_PARAMS:indicator lengths must match OHLC')
    large = tuple(mean is not None and body >= mean * cfg.large_body_multiple
                  for body, mean in zip(g.body, g.mean_body))
    small = tuple(mean is not None and body <= mean * cfg.small_body_multiple
                  for body, mean in zip(g.body, g.mean_body))
    bull_shape, bear_shape, bull, bear = ([False] * n for _ in range(4))
    bull_anchor, bear_anchor = [None] * n, [None] * n

    def touch(name, i):
        value = lines[name][i] if name in lines else None
        return value is not None and g.low[i] <= value <= g.high[i]

    def near_extreme(i, bottom, price):
        if i < g.lookback:
            return False
        if bottom:
            extreme = min(g.low[i - g.lookback:i])
            return price <= extreme + extreme * cfg.position_tolerance_pct / 100
        extreme = max(g.high[i - g.lookback:i])
        return price >= extreme - extreme * cfg.position_tolerance_pct / 100

    for i in range(n):
        b, s, bp, sp = False, False, True, True
        ba, sa = g.low[i], g.high[i]
        up, down = g.close[i] > g.open[i], g.close[i] < g.open[i]
        if kind == 'engulfing' and i >= 1:
            p = i - 1
            b = (g.close[p] < g.open[p] and up and g.open[i] <= g.close[p]
                 and g.close[i] >= g.open[p] and g.body[i] >= g.body[p] * cfg.engulf_body_multiple)
            s = (g.close[p] > g.open[p] and down and g.open[i] >= g.close[p]
                 and g.close[i] <= g.open[p] and g.body[i] >= g.body[p] * cfg.engulf_body_multiple)
            ba, sa = min(g.low[p:i + 1]), max(g.high[p:i + 1])
            bp, sp = near_extreme(i, True, ba) or touch('bb_lower', i), near_extreme(i, False, sa) or touch('bb_upper', i)
        elif kind == 'pin_bar':
            positive = g.body[i] > 0 and g.span[i] > 0
            b = (positive and g.lower[i] >= g.body[i] * cfg.pin_shadow_multiple
                 and g.upper[i] <= g.span[i] * cfg.pin_short_shadow_fraction
                 and min(g.open[i], g.close[i]) >= g.high[i] - g.span[i] * cfg.pin_body_zone_fraction)
            s = (positive and g.upper[i] >= g.body[i] * cfg.pin_shadow_multiple
                 and g.lower[i] <= g.span[i] * cfg.pin_short_shadow_fraction
                 and max(g.open[i], g.close[i]) <= g.low[i] + g.span[i] * cfg.pin_body_zone_fraction)
            ema_touch = touch('ema20', i) or touch('ema60', i)
            bp = (i >= g.lookback and g.low[i] < min(g.low[i - g.lookback:i])) or ema_touch
            sp = (i >= g.lookback and g.high[i] > max(g.high[i - g.lookback:i])) or ema_touch
        elif kind == 'star' and i >= 2:
            a, m = i - 2, i - 1
            middle = g.body[m] <= g.body[a] * cfg.star_middle_body_fraction
            b = (large[a] and g.close[a] < g.open[a] and middle and g.close[m] < g.close[a]
                 and up and g.close[i] > (g.open[a] + g.close[a]) / 2)
            s = (large[a] and g.close[a] > g.open[a] and middle and g.close[m] > g.close[a]
                 and down and g.close[i] < (g.open[a] + g.close[a]) / 2)
            ba, sa = g.low[m], g.high[m]
        elif kind == 'soldiers' and i >= 3:
            a = i - 2
            increasing = all(g.close[k] > g.open[k] for k in range(a, i + 1))
            decreasing = all(g.close[k] < g.open[k] for k in range(a, i + 1))
            inside = all(min(g.open[k - 1], g.close[k - 1]) <= g.open[k] <= max(g.open[k - 1], g.close[k - 1])
                         for k in range(a, i + 1))
            b = increasing and inside and all(g.close[k] > g.close[k - 1] for k in range(a, i + 1)) and all(
                g.upper[k] <= g.body[k] * cfg.soldier_shadow_fraction for k in range(a, i + 1))
            s = decreasing and inside and all(g.close[k] < g.close[k - 1] for k in range(a, i + 1)) and all(
                g.lower[k] <= g.body[k] * cfg.soldier_shadow_fraction for k in range(a, i + 1))
            bp = a >= g.lookback and g.open[a] < g.close[a - g.lookback]
            sp = a >= g.lookback and g.open[a] > g.close[a - g.lookback]
            ba, sa = g.low[a], g.high[a]
        elif kind == 'doji':
            shape = (g.span[i] > 0 and g.body[i] <= g.span[i] * cfg.doji_body_fraction
                     and g.mean_span[i] is not None and g.span[i] >= g.mean_span[i] * cfg.doji_span_multiple)
            b, s = shape, shape
            rsi = lines['rsi'][i] if 'rsi' in lines else None
            bp, sp = rsi is not None and rsi < 30, rsi is not None and rsi > 70
        elif kind == 'inside_bar' and i >= 1:
            b = s = g.high[i] < g.high[i - 1] and g.low[i] > g.low[i - 1]
            ba, sa = g.low[i - 1], g.high[i - 1]
        bull_shape[i], bear_shape[i] = bool(b), bool(s)
        bull[i], bear[i] = bool(b and bp), bool(s and sp)
        bull_anchor[i] = ba if bull[i] else None
        bear_anchor[i] = sa if bear[i] else None
    return PatternSeries(tuple(bull_shape), tuple(bear_shape), tuple(bull), tuple(bear),
                         tuple(bull_anchor), tuple(bear_anchor), large, small)
