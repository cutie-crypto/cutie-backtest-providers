"""Strict parsing and absolute UTC session rules, including both DST transitions."""
from datetime import datetime, timezone
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from strategy_time_layer import (TimeConfig, TimeContext, TimeDataGapError,
    _TIME_PARAM_SCHEMA_PROPERTIES, fixed_timeframe_milliseconds)

DEFAULTS = {key: spec['default'] for key, spec in _TIME_PARAM_SCHEMA_PROPERTIES.items()}


def context(**params):
    return TimeContext.build(TimeConfig.parse(dict(time_layer_enabled=True, **params)), '1h',
                             [datetime(2026, 1, 1), datetime(2026, 1, 1, 1)])


def instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


@pytest.mark.parametrize('key', DEFAULTS)
def test_defaults(key):
    assert TimeConfig.parse({key: DEFAULTS[key]}) == TimeConfig()


@pytest.mark.parametrize('params', [
    {'time_layer_enabled': True}, {'time_weekdays': 1}, {'time_weekdays': 127},
    {'time_timezone': 'America/New_York'}, {'time_timezone': 'UTC'},
    {'time_session_start': '00:00', 'time_session_end': '23:59'},
    {'time_session_start': '23:59', 'time_session_end': '00:00'},
])
def test_valid_boundaries(params):
    assert TimeConfig.parse({'time_layer_enabled': True, **params}).enabled


BAD_PARAMS = [
    *[{'time_layer_enabled': value} for value in (0, 1, 'true', None, 1.0)],
    *[{'time_weekdays': value} for value in (0, 128, -1, True, False, 1.0, 127.0, 1.5, '1', None)],
    *[{'time_timezone': value} for value in ('', 'Not/A_Zone', '/etc/passwd', '../UTC', 1, None, True)],
    *[{key: value, ('time_session_end' if key.endswith('start') else 'time_session_start'): '10:00'}
      for key in ('time_session_start', 'time_session_end')
      for value in ('9:30', '24:00', '12:60', ' 09:30', '09:30 ', '09:30:00', 'ab:cd', None, 930, True)],
    {'time_session_start': '09:00'}, {'time_session_end': '10:00'},
    {'time_session_start': '09:00', 'time_session_end': '09:00'},
    {'time_calendar': 'none'},
    *[{'time_max_holding_minutes': value} for value in (-1, 525601, True, 1.0, '60')],
]


@pytest.mark.parametrize('params', BAD_PARAMS)
def test_invalid_parameters(params):
    with pytest.raises(ValueError, match='INVALID_PARAMS:'):
        TimeConfig.parse({'time_layer_enabled': True, **params})


@pytest.mark.parametrize('params', [
    {'time_timezone': 'America/New_York'}, {'time_weekdays': 1},
    {'time_session_start': '09:00', 'time_session_end': '10:00'},
])
@pytest.mark.parametrize('explicit', [False, True])
def test_disabled_nondefaults_rejected(params, explicit):
    with pytest.raises(ValueError, match='require time_layer_enabled=true'):
        TimeConfig.parse({**params, **({'time_layer_enabled': False} if explicit else {})})


@pytest.mark.parametrize('timestamp,allowed', [
    ('2026-10-09T08:59:59Z', False), ('2026-10-09T09:00:00Z', True),
    ('2026-10-09T09:59:59Z', True), ('2026-10-09T10:00:00Z', False),
    ('2026-10-10T09:30:00Z', False),
])
def test_normal_session(timestamp, allowed):
    ctx = context(time_session_start='09:00', time_session_end='10:00', time_weekdays=16)
    assert ctx.allow_entry(instant(timestamp)) is allowed


@pytest.mark.parametrize('timestamp,allowed', [
    ('2026-10-09T21:59:59Z', False), ('2026-10-09T22:00:00Z', True),
    ('2026-10-10T01:00:00Z', True), ('2026-10-10T02:00:00Z', False),
    ('2026-10-10T22:00:00Z', False), ('2026-10-09T01:00:00Z', False),
])
def test_overnight_weekday_belongs_to_session_start(timestamp, allowed):
    ctx = context(time_session_start='22:00', time_session_end='02:00', time_weekdays=16)
    assert ctx.allow_entry(instant(timestamp)) is allowed


@pytest.mark.parametrize('weekday', range(7))
def test_weekday_bits_without_session(weekday):
    ctx = context(time_weekdays=1 << weekday)
    for day in range(7):
        assert ctx.allow_entry(datetime(2026, 10, 5 + day, tzinfo=timezone.utc)) is (day == weekday)


@pytest.mark.parametrize('timestamp,start,end,allowed', [
    ('2026-03-08T06:59:00Z', '01:00', '02:00', True),
    ('2026-03-08T07:00:00Z', '01:00', '02:00', False),
    ('2026-03-08T07:00:00Z', '03:00', '04:00', True),
    ('2026-11-01T05:30:00Z', '01:00', '02:00', True),
    ('2026-11-01T06:30:00Z', '01:00', '02:00', True),
    ('2026-11-01T07:00:00Z', '01:00', '02:00', False),
])
def test_dst_real_utc(timestamp, start, end, allowed):
    ctx = context(time_timezone='America/New_York', time_session_start=start, time_session_end=end,
                  time_weekdays=64)
    assert ctx.allow_entry(instant(timestamp)) is allowed


@pytest.mark.parametrize('timeframe', ['1M', '0h', 'hour', '', '1.5h'])
def test_unsupported_period(timeframe):
    with pytest.raises(ValueError, match='INVALID_PARAMS:'):
        fixed_timeframe_milliseconds(timeframe)


@pytest.mark.parametrize('timeframe,milliseconds', [('1m', 60000), ('2h', 7200000),
    ('1d', 86400000), ('1w', 604800000)])
def test_fixed_period(timeframe, milliseconds):
    assert fixed_timeframe_milliseconds(timeframe) == milliseconds


def test_gap_rejected_and_decision_is_close_boundary():
    ctx = context()
    assert ctx.decision_utc(datetime(2026, 1, 1)) == instant('2026-01-01T01:00:00Z')
    with pytest.raises(TimeDataGapError, match='TIME_DATA_GAP:'):
        TimeContext.build(ctx.config, '1h', [datetime(2026, 1, 1), datetime(2026, 1, 1, 2)])
