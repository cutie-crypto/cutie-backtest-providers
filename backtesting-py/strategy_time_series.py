"""Causal time-series foundations, deliberately absent from tool schemas/runners.

Naive inputs mean UTC, as in strategy_time_layer. Reset boundaries use the first
fold of repeated local times; nonexistent resets are skipped (the preceding
cycle continues). Adjacent half-open UTC cycles therefore partition real time.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import math
import re
from typing import Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from strategy_time_layer import TimeContext, utc_datetime


def _clock(value: str) -> tuple[int, int]:
    if not isinstance(value, str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', value):
        raise ValueError('INVALID_PARAMS:time-series clock must be HH:MM')
    return tuple(map(int, value.split(':')))


@dataclass(frozen=True)
class PeriodDefinition:
    timezone_name: str = 'UTC'
    reset_period: str = 'day'
    reset_at: str = '00:00'

    def __post_init__(self):
        try:
            ZoneInfo(self.timezone_name)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            raise ValueError('INVALID_PARAMS:unknown IANA timezone') from None
        if self.reset_period not in ('day', 'week'):
            raise ValueError('INVALID_PARAMS:reset_period must be day or week')
        _clock(self.reset_at)


@dataclass(frozen=True)
class PeriodBounds:
    start_utc: datetime
    end_utc: datetime


def _local_boundary(day: date, clock: str, zone: ZoneInfo) -> datetime | None:
    hour, minute = _clock(clock)
    wall = datetime(day.year, day.month, day.day, hour, minute)
    candidate = wall.replace(tzinfo=zone, fold=0).astimezone(timezone.utc)
    return candidate if candidate.astimezone(zone).replace(tzinfo=None) == wall else None


def period_bounds(bar_open: datetime, definition: PeriodDefinition) -> PeriodBounds:
    """Assign by OPEN, including bars closing at midnight; week starts Monday.

    Boundaries are constructed in the local calendar, never by adding 24h/168h
    in UTC. Missing reset wall times merge into the preceding cycle.
    """
    instant = utc_datetime(bar_open)
    zone = ZoneInfo(definition.timezone_name)
    day = instant.astimezone(zone).date()
    stride = timedelta(days=7 if definition.reset_period == 'week' else 1)
    if definition.reset_period == 'week':
        day -= timedelta(days=day.weekday())
    start = _local_boundary(day, definition.reset_at, zone)
    while start is None or start > instant:
        day -= stride
        start = _local_boundary(day, definition.reset_at, zone)
    next_day = day + stride
    end = _local_boundary(next_day, definition.reset_at, zone)
    while end is None or end <= instant:
        if end is not None:
            start = end
        next_day += stride
        end = _local_boundary(next_day, definition.reset_at, zone)
    return PeriodBounds(start, end)


class TimeHistoryError(ValueError):
    """Future runner adapters may map error_type/reason directly to failures."""
    error_type = 'INSUFFICIENT_DATA'
    reason = 'time_history_incomplete'

    def __init__(self, message: str):
        super().__init__(f'{self.error_type}:{self.reason}:{message}')


def required_history_start(backtest_start: datetime, definition: PeriodDefinition,
                           observation_start: datetime | None = None) -> datetime:
    """Cover the opening cycle and, if earlier, an explicitly selected window."""
    cycle_start = period_bounds(backtest_start, definition).start_utc
    return min(cycle_start, utc_datetime(observation_start)) if observation_start is not None else cycle_start


def validate_time_history(opens: Sequence[datetime], required_start: datetime,
                          end_utc: datetime, context: TimeContext) -> None:
    """Require exactly every complete candle in [required_start, end_utc).

    Endpoints must be candle boundaries. Duplicates, disorder and extra rows are
    rejected here; the fetch adapter crops source oversupply before validation.
    """
    start, end = utc_datetime(required_start), utc_datetime(end_utc)
    if end <= start or (end - start) % context.period:
        raise ValueError('INVALID_PARAMS:history bounds must align with complete candles')
    expected = start
    for value in opens:
        current = utc_datetime(value)
        if current != expected or current >= end:
            raise TimeHistoryError(f'expected {expected.isoformat()}, got {current.isoformat()}')
        expected += context.period
    if expected != end:
        raise TimeHistoryError(f'history ends at {expected.isoformat()}, requires {end.isoformat()}')


@dataclass(frozen=True)
class SeriesBar:
    open_utc: datetime
    high: float
    low: float
    close: float
    volume: float


def _check_bar(bar: SeriesBar) -> None:
    if not all(math.isfinite(v) for v in (bar.high, bar.low, bar.close, bar.volume)) or bar.volume < 0:
        raise TimeHistoryError('nonfinite price/volume or negative volume')


def range_window(cycle: PeriodBounds, definition: PeriodDefinition,
                 range_start: str, range_end: str) -> PeriodBounds:
    """One observation window per cycle, starting on/after its local reset.

    Equal clocks mean a positive 24 wall-hour window. Cross-midnight windows
    retain the START day's cycle, even when their end crosses the next reset.
    Nonexistent window endpoints are rejected; repeated endpoints use fold=0.
    """
    start_clock, end_clock = _clock(range_start), _clock(range_end)
    zone = ZoneInfo(definition.timezone_name)
    day = cycle.start_utc.astimezone(zone).date()
    start = _local_boundary(day, range_start, zone)
    if start is not None and start < cycle.start_utc:
        day += timedelta(days=1)
        start = _local_boundary(day, range_start, zone)
    end_day = day + timedelta(days=int(end_clock <= start_clock))
    end = _local_boundary(end_day, range_end, zone)
    if start is None or end is None or end <= start:
        raise ValueError('INVALID_PARAMS:observation window has nonexistent or nonpositive endpoints')
    return PeriodBounds(start, end)


@dataclass(frozen=True)
class FrozenRange:
    cycle: PeriodBounds
    freeze_utc: datetime
    high: float | None
    low: float | None
    available: bool
    warmup_only: bool


def freeze_range(bars: Sequence[SeriesBar], context: TimeContext, cycle: PeriodBounds,
                 definition: PeriodDefinition, range_start: str, range_end: str,
                 *, as_of: datetime, backtest_start: datetime) -> FrozenRange:
    """Snapshot for ONE cycle at a decision instant, with no future price reads.

    Before window end, both prices are absent. At/after end a complete history
    is mandatory; missing candles fail instead of freezing a partial range.
    Caller can request each cycle independently, including overnight windows.
    """
    window = range_window(cycle, definition, range_start, range_end)
    decision = utc_datetime(as_of)
    if not bars:
        raise TimeHistoryError('range requires candle grid')
    grid = utc_datetime(bars[0].open_utc)
    if (window.start_utc - grid) % context.period or (window.end_utc - grid) % context.period:
        raise ValueError('INVALID_PARAMS:observation endpoint cuts through a candle')
    warmup = window.end_utc <= utc_datetime(backtest_start)
    if decision < window.end_utc:
        return FrozenRange(cycle, window.end_utc, None, None, False, warmup)
    selected = [bar for bar in bars if window.start_utc <= utc_datetime(bar.open_utc)
                and context.decision_utc(bar.open_utc) <= window.end_utc
                and context.decision_utc(bar.open_utc) <= decision]
    validate_time_history([bar.open_utc for bar in selected], window.start_utc, window.end_utc, context)
    for bar in selected:
        _check_bar(bar)
    return FrozenRange(cycle, window.end_utc, max(bar.high for bar in selected),
                       min(bar.low for bar in selected), True, warmup)


@dataclass(frozen=True)
class VwapPoint:
    open_utc: datetime
    decision_utc: datetime
    cycle: PeriodBounds
    value: float | None
    warmup_only: bool


def prefix_vwap(bars: Sequence[SeriesBar], context: TimeContext, definition: PeriodDefinition,
                *, backtest_start: datetime, price_source: str = 'hlc3',
                as_of: datetime | None = None) -> list[VwapPoint]:
    """IEEE-754 float64-style scalar sums, no rounding, zero volume => None.

    Each output is computed before advancing to the next bar. Only candles
    closed at as_of are consumed; earlier outputs cannot depend on later prices.
    Prefixes must start at a cycle boundary and contain no missing candles.
    """
    if price_source not in ('hlc3', 'close'):
        raise ValueError('INVALID_PARAMS:vwap price_source must be hlc3 or close')
    result = []
    active = None
    numerator = denominator = 0.0
    expected = None
    for bar in bars:
        opened = utc_datetime(bar.open_utc)
        decision = context.decision_utc(opened)
        if as_of is not None and decision > utc_datetime(as_of):
            break
        cycle = period_bounds(opened, definition)
        if expected is None:
            expected = cycle.start_utc
        if opened != expected:
            raise TimeHistoryError(f'VWAP expected {expected.isoformat()}, got {opened.isoformat()}')
        expected = decision
        _check_bar(bar)
        if cycle != active:
            active = cycle
            numerator = denominator = 0.0
        price = float(bar.close) if price_source == 'close' else (float(bar.high) / 3 + float(bar.low) / 3 + float(bar.close) / 3)
        numerator += price * float(bar.volume)
        denominator += float(bar.volume)
        if not math.isfinite(numerator) or not math.isfinite(denominator):
            raise TimeHistoryError('VWAP accumulation overflow')
        value = numerator / denominator if denominator else None
        result.append(VwapPoint(opened, decision, cycle, value, opened < utc_datetime(backtest_start)))
    return result
