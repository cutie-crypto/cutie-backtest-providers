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
