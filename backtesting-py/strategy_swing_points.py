"""Strict, causal swing events indexed by CONFIRMATION bar, without engine wiring."""
from __future__ import annotations

from dataclasses import dataclass
import math
from numbers import Real
from typing import Sequence


@dataclass(frozen=True)
class SwingPoint:
    index: int
    confirmed_at: int
    price: float


@dataclass(frozen=True)
class SwingSeries:
    highs: tuple[SwingPoint | None, ...]
    lows: tuple[SwingPoint | None, ...]


def _prices(values: Sequence[float]) -> tuple[float, ...]:
    result = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value) or value <= 0:
            raise ValueError('INVALID_PARAMS:swing prices must be finite positive numbers')
        result.append(float(value))
    return tuple(result)


def swing_points(high: Sequence[float], low: Sequence[float], *, n: int = 5) -> SwingSeries:
    """Left/right each have n CLOSED bars; ties never qualify (codex-2 定).

    Slots before 2*n and incomplete right tails contain None, not backfilled
    extrema. A point at j is emitted only at j+n. n is a strict int in 1..500.
    Indices refer to the supplied sequence, including any caller's warmup prefix.
    """
    if type(n) is not int or not 1 <= n <= 500:
        raise ValueError('INVALID_PARAMS:n must be an integer within 1-500')
    high, low = _prices(high), _prices(low)
    if len(high) != len(low) or any(h < l for h, l in zip(high, low)):
        raise ValueError('INVALID_PARAMS:swing High/Low lengths or ordering invalid')
    highs, lows = [None] * len(high), [None] * len(low)
    for confirmation in range(2 * n, len(high)):
        j = confirmation - n
        neighbours = tuple(range(j - n, j)) + tuple(range(j + 1, confirmation + 1))
        if all(low[j] < low[k] for k in neighbours):
            lows[confirmation] = SwingPoint(j, confirmation, low[j])
        if all(high[j] > high[k] for k in neighbours):
            highs[confirmation] = SwingPoint(j, confirmation, high[j])
    return SwingSeries(tuple(highs), tuple(lows))


def confirmed_points(series: SwingSeries, *, as_of: int, kind: str, count: int = 3) -> tuple[SwingPoint, ...]:
    """Return up to count latest confirmed points in chronological order.

    An explicit decision bar is mandatory even for a fully precomputed series.
    This does not promise a point exists; no future point is used as a fallback.
    """
    if kind not in ('high', 'low'):
        raise ValueError('INVALID_PARAMS:kind must be high or low')
    if type(as_of) is not int or not 0 <= as_of < len(series.highs):
        raise ValueError('INVALID_PARAMS:as_of must be an available bar index')
    if type(count) is not int or count < 1:
        raise ValueError('INVALID_PARAMS:count must be a positive integer')
    events = series.highs if kind == 'high' else series.lows
    available = [point for point in events[:as_of + 1]
                 if point is not None and point.confirmed_at <= as_of]
    return tuple(available[-count:])
