"""Request-local entry clock. No engine, indicator or market-data dependencies."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from importlib import metadata
from pathlib import Path
import re
from typing import Any, Mapping, Sequence
from zoneinfo import TZPATH, ZoneInfo, ZoneInfoNotFoundError

_TIME_PARAM_SCHEMA_PROPERTIES = {
    'time_layer_enabled': {'type': 'boolean', 'default': False},
    'time_timezone': {'type': 'string', 'default': 'UTC'},
    'time_session_start': {'type': 'string', 'default': ''},
    'time_session_end': {'type': 'string', 'default': ''},
    'time_weekdays': {'type': 'integer', 'default': 127, 'minimum': 1, 'maximum': 127},
    'time_max_holding_minutes': {'type': 'integer', 'default': 0, 'minimum': 0, 'maximum': 525600},
    'time_flatten_at': {'type': 'string', 'default': ''},
    'time_flatten_weekdays': {'type': 'integer', 'default': 127, 'minimum': 1, 'maximum': 127},
}


@dataclass(frozen=True)
class TimeConfig:
    enabled: bool = False
    timezone_name: str = 'UTC'
    session_start: str = ''
    session_end: str = ''
    weekdays: int = 127
    max_holding_minutes: int = 0
    flatten_at: str = ''
    flatten_weekdays: int = 127

    @classmethod
    def parse(cls, params: Mapping[str, Any]) -> TimeConfig:
        for key in params:
            if key.startswith('time_') and key not in _TIME_PARAM_SCHEMA_PROPERTIES:
                raise ValueError(f'INVALID_PARAMS:unknown time parameter {key}')
        values = {key: params.get(key, spec['default']) for key, spec in _TIME_PARAM_SCHEMA_PROPERTIES.items()}
        for key, spec in _TIME_PARAM_SCHEMA_PROPERTIES.items():
            expected = {'boolean': bool, 'integer': int, 'string': str}[spec['type']]
            if type(values[key]) is not expected:
                raise ValueError(f'INVALID_PARAMS:{key} must be a {spec["type"]}')
        for key, spec in _TIME_PARAM_SCHEMA_PROPERTIES.items():
            if 'minimum' in spec and not spec['minimum'] <= values[key] <= spec['maximum']:
                raise ValueError(f'INVALID_PARAMS:{key} must be within {spec["minimum"]}-{spec["maximum"]}')
        name = values['time_timezone']
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f'INVALID_PARAMS:unknown IANA timezone {name}') from None
        start, end = values['time_session_start'], values['time_session_end']
        for key in ('time_session_start', 'time_session_end', 'time_flatten_at'):
            value = values[key]
            if value and not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', value):
                raise ValueError(f'INVALID_PARAMS:{key} must be HH:MM (00:00-23:59)')
        if bool(start) != bool(end) or (start and start == end):
            raise ValueError('INVALID_PARAMS:session bounds must be paired and different')
        if values['time_flatten_weekdays'] != 127 and not values['time_flatten_at']:
            raise ValueError('INVALID_PARAMS:time_flatten_weekdays requires time_flatten_at')
        if not values['time_layer_enabled'] and any(
            values[key] != spec['default'] for key, spec in _TIME_PARAM_SCHEMA_PROPERTIES.items()
        ):
            raise ValueError('INVALID_PARAMS:non-default time parameters require time_layer_enabled=true')
        return cls(values['time_layer_enabled'], name, start, end, values['time_weekdays'],
                   values['time_max_holding_minutes'], values['time_flatten_at'], values['time_flatten_weekdays'])


class TimeDataGapError(ValueError):
    """Known next-open cannot equal the declared close boundary."""


def fixed_timeframe_milliseconds(timeframe: str) -> int:
    match = re.fullmatch(r'([1-9][0-9]*)([mhdw])', timeframe)
    if not match:
        raise ValueError('INVALID_PARAMS:time layer requires a fixed m/h/d/w timeframe')
    return int(match[1]) * {'m': 60000, 'h': 3600000, 'd': 86400000, 'w': 604800000}[match[2]]


def utc_datetime(value: datetime) -> datetime:
    # Provider indices are naive UTC; aware inputs retain their absolute instant.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def tzdata_version() -> str:
    # ZoneInfo searches system data before the optional Python wheel.
    for root in TZPATH:
        source = Path(root) / 'tzdata.zi'
        if source.is_file():
            with source.open() as handle:
                first = handle.readline().strip()
            if first.startswith('# version '):
                return first.removeprefix('# version ')
    try:
        return metadata.version('tzdata')
    except metadata.PackageNotFoundError:
        return 'system:version-unavailable'


@dataclass(frozen=True)
class TimeContext:
    config: TimeConfig
    zone: ZoneInfo
    period: timedelta
    last_open_utc: datetime

    @classmethod
    def build(cls, config: TimeConfig, timeframe: str, opens: Sequence[datetime]) -> TimeContext:
        if not config.enabled:
            raise ValueError('TimeContext requires an enabled time layer')
        period = timedelta(milliseconds=fixed_timeframe_milliseconds(timeframe))
        timestamps = [utc_datetime(value) for value in opens]
        if not timestamps:
            raise TimeDataGapError('TIME_DATA_GAP:time layer requires bar timestamps')
        for current, following in zip(timestamps, timestamps[1:]):
            if following != current + period:
                raise TimeDataGapError(
                    f'TIME_DATA_GAP:expected next open {(current + period).isoformat()}, got {following.isoformat()}')
        return cls(config, ZoneInfo(config.timezone_name), period, timestamps[-1])

    def decision_utc(self, bar_open: datetime) -> datetime:
        return utc_datetime(bar_open) + self.period

    def allow_entry(self, decision_utc: datetime) -> bool:
        local = utc_datetime(decision_utc).astimezone(self.zone)
        minute = local.hour * 60 + local.minute
        weekday = local.weekday()
        if self.config.session_start:
            start, end = (int(value[:2]) * 60 + int(value[3:])
                          for value in (self.config.session_start, self.config.session_end))
            if start < end:
                if not start <= minute < end:
                    return False
            else:
                if not (minute >= start or minute < end):
                    return False
                if minute < end:
                    weekday = (local.date() - timedelta(days=1)).weekday()
        return bool(self.config.weekdays & (1 << weekday))

    def assumptions(self) -> dict[str, Any]:
        return {'time_layer': dict(timezone=self.config.timezone_name, tzdata_version=tzdata_version(),
            session_start=self.config.session_start, session_end=self.config.session_end,
            weekdays=self.config.weekdays, decision_time='bar_close', gate='entry_only', fill='next_bar_open')}
