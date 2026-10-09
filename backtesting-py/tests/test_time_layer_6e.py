"""6e-1 regular calendars: independent scalar UTC expectations, no runner wiring."""
from datetime import date, datetime, timedelta, timezone
import pytest

from test_risk_layer import p  # establishes this checkout's import paths
from strategy_time_calendar import (
    RegularCalendar, validate_calendar_timezone, calendar_week_bounds, REGULAR_CALENDAR_ASSUMPTION,
)
from strategy_time_series import PeriodBounds

US = RegularCalendar('us_equity_regular')
CME = RegularCalendar('cme_btc_regular')


def dt(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


@pytest.mark.parametrize('instant,opened', [
    ('2026-01-05T14:29', False), ('2026-01-05T14:30', True),
    ('2026-01-05T20:59', True), ('2026-01-05T21:00', False),
    ('2026-01-10T14:30', False), ('2026-01-11T14:30', False),
])
def test_us_session_boundaries(instant, opened):
    assert US.is_open(dt(instant)) is opened


@pytest.mark.parametrize('instant,timeframe,expected', [
    ('2026-01-05T20:45', '15m', True), ('2026-01-05T21:00', '15m', False),
    ('2026-01-05T20:50', '15m', False), ('2026-01-05T14:15', '15m', False),
    ('2026-01-05T14:30', '6h', True), ('2026-01-05T14:30', '7h', False),
])
def test_us_whole_candle(instant, timeframe, expected):
    assert US.contains_bar(dt(instant), timeframe) is expected


@pytest.mark.parametrize('instant,expected', [
    ('2026-01-04T22:59', False), ('2026-01-04T23:00', True),
    ('2026-01-07T21:59', True), ('2026-01-07T22:00', False),
    ('2026-01-07T22:30', False), ('2026-01-07T22:59', False), ('2026-01-07T23:00', True),
    ('2026-01-09T21:59', True), ('2026-01-09T22:00', False), ('2026-01-09T23:00', False),
    ('2026-01-10T12:00', False), ('2026-01-11T22:59', False), ('2026-01-11T23:00', True),
])
def test_cme_sessions_and_pause(instant, expected):
    assert CME.is_open(dt(instant)) is expected


@pytest.mark.parametrize('instant,timeframe,expected', [
    ('2026-01-07T21:45', '15m', True), ('2026-01-07T21:50', '15m', False),
    ('2026-01-07T22:00', '15m', False), ('2026-01-07T23:00', '15m', True),
    ('2026-01-04T22:45', '15m', False), ('2026-01-04T23:00', '23h', True),
    ('2026-01-04T23:00', '24h', False), ('2026-01-09T21:45', '15m', True),
])
def test_cme_whole_candle(instant, timeframe, expected):
    assert CME.contains_bar(dt(instant), timeframe) is expected


@pytest.mark.parametrize('instant,expected', [
    ('2026-01-04T23:30', date(2026, 1, 5)),
    ('2026-01-07T00:00', date(2026, 1, 7)),  # Tuesday 18:00 Chicago => Wednesday
    ('2026-01-06T21:00', date(2026, 1, 6)),
    ('2026-01-07T22:30', None), ('2026-01-10T12:00', None), ('2026-01-09T22:00', None),
])
def test_cme_trading_day(instant, expected):
    assert CME.trading_day(dt(instant)) == expected


@pytest.mark.parametrize('instant,expected', [('2026-01-05T15:00', date(2026, 1, 5)),
    ('2026-01-05T14:29', None), ('2026-01-05T21:00', None), ('2026-01-10T15:00', None)])
def test_us_trading_day(instant, expected):
    assert US.trading_day(dt(instant)) == expected


@pytest.mark.parametrize('calendar,instant,inclusive,expected', [
    (US, '2026-01-05T14:29', False, '2026-01-05T14:30'),
    (US, '2026-01-05T14:30', True, '2026-01-05T14:30'),
    (US, '2026-01-05T14:30', False, '2026-01-06T14:30'),
    (US, '2026-01-05T18:00', False, '2026-01-06T14:30'),
    (US, '2026-01-09T21:00', False, '2026-01-12T14:30'),
    (CME, '2026-01-04T22:59', False, '2026-01-04T23:00'),
    (CME, '2026-01-04T23:00', True, '2026-01-04T23:00'),
    (CME, '2026-01-04T23:00', False, '2026-01-05T23:00'),
    (CME, '2026-01-07T22:30', False, '2026-01-07T23:00'),
    (CME, '2026-01-09T22:00', False, '2026-01-11T23:00'),
])
def test_next_session_open(calendar, instant, inclusive, expected):
    assert calendar.next_open(dt(instant), inclusive=inclusive) == dt(expected)


@pytest.mark.parametrize('opening', ['2026-03-06T14:30', '2026-03-09T13:30',
                                     '2026-10-30T13:30', '2026-11-02T14:30'])
def test_us_dst_wall_open(opening):
    instant = dt(opening)
    assert not US.is_open(instant-timedelta(seconds=1))
    assert US.is_open(instant)
    assert US.next_open(instant-timedelta(seconds=1)) == instant


@pytest.mark.parametrize('sunday,cme_open,us_open', [
    ('2026-03-08T07:00', '2026-03-08T22:00', '2026-03-09T13:30'),
    ('2026-11-01T06:00', '2026-11-01T23:00', '2026-11-02T14:30'),
])
def test_dst_sunday_open_utc(sunday, cme_open, us_open):
    # US has no Sunday session: the next open is Monday at local 09:30.
    assert not US.is_open(dt(sunday)) and not CME.is_open(dt(sunday))
    assert CME.next_open(dt(sunday)) == dt(cme_open)
    assert US.next_open(dt(sunday)) == dt(us_open)
    assert not CME.is_open(dt(cme_open)-timedelta(seconds=1))
    assert CME.is_open(dt(cme_open))
    assert CME.trading_day(dt(cme_open)) == date.fromisoformat(us_open[:10])


@pytest.mark.parametrize('calendar,instant,start,end', [
    (US, '2026-03-09T14:00', '2026-03-09T13:30', '2026-03-13T20:00'),
    (US, '2026-11-02T15:00', '2026-11-02T14:30', '2026-11-06T21:00'),
    (CME, '2026-03-08T22:00', '2026-03-08T22:00', '2026-03-13T21:00'),
    (CME, '2026-11-01T23:00', '2026-11-01T23:00', '2026-11-06T22:00'),
    (CME, '2026-03-01T23:00', '2026-03-01T23:00', '2026-03-06T22:00'),
    (CME, '2026-10-25T22:00', '2026-10-25T22:00', '2026-10-30T21:00'),
])
def test_dst_week_bounds(calendar, instant, start, end):
    assert calendar_week_bounds(dt(instant), calendar) == PeriodBounds(dt(start), dt(end))


@pytest.mark.parametrize('calendar,sessions,expected_cycle,expected_bars', [
    (US, [('2026-01-05T14:30', '2026-01-05T21:00'), ('2026-01-06T14:30', '2026-01-06T21:00'),
          ('2026-01-07T14:30', '2026-01-07T21:00'), ('2026-01-08T14:30', '2026-01-08T21:00'),
          ('2026-01-09T14:30', '2026-01-09T21:00')], ('2026-01-05T14:30', '2026-01-09T21:00'), 130),
    (CME, [('2026-01-04T23:00', '2026-01-05T22:00'), ('2026-01-05T23:00', '2026-01-06T22:00'),
           ('2026-01-06T23:00', '2026-01-07T22:00'), ('2026-01-07T23:00', '2026-01-08T22:00'),
           ('2026-01-08T23:00', '2026-01-09T22:00')], ('2026-01-04T23:00', '2026-01-09T22:00'), 460),
])
def test_complete_week_quarters_membership(calendar, sessions, expected_cycle, expected_bars):
    sessions = [(dt(start), dt(end)) for start, end in sessions]
    cycle = PeriodBounds(*(dt(value) for value in expected_cycle))
    step = timedelta(minutes=15)
    count = 0
    for i in range(7*24*4):
        opened = dt('2026-01-04T00:00')+i*step
        matches = sum(start <= opened and opened+step <= end for start, end in sessions)
        assert matches in (0, 1)
        assert calendar.contains_bar(opened, '15m') is bool(matches)
        assert calendar_week_bounds(opened, calendar) == (cycle if matches else None)
        count += matches
    assert count == expected_bars


@pytest.mark.parametrize('calendar,instant', [(US, '2026-01-10T15:00'), (US, '2026-01-11T15:00'),
    (CME, '2026-01-09T22:00'), (CME, '2026-01-10T15:00'), (CME, '2026-01-11T22:59')])
def test_weekend_has_no_cycle(calendar, instant):
    assert calendar_week_bounds(dt(instant), calendar) is None


@pytest.mark.parametrize('calendar,instant,start,end', [
    (US, '2027-01-01T15:00', '2026-12-28T14:30', '2027-01-01T21:00'),
    (CME, '2026-12-27T23:30', '2026-12-27T23:00', '2027-01-01T22:00'),
    (CME, '2027-01-01T21:00', '2026-12-27T23:00', '2027-01-01T22:00'),
])
def test_cross_year_week(calendar, instant, start, end):
    assert calendar_week_bounds(dt(instant), calendar) == PeriodBounds(dt(start), dt(end))


@pytest.mark.parametrize('calendar,instant', [(US, '2026-01-05T14:29'), (US, '2026-01-05T21:00'),
                                          (CME, '2026-01-07T22:30')])
def test_internal_closures_have_no_cycle(calendar, instant):
    assert calendar.trading_day(dt(instant)) is None
    assert calendar_week_bounds(dt(instant), calendar) is None


@pytest.mark.parametrize('name,zone,accepted', [
    ('none', 'UTC', True), ('none', 'America/Chicago', True),
    ('us_equity_regular', 'America/New_York', True), ('us_equity_regular', 'UTC', False),
    ('cme_btc_regular', 'America/Chicago', True), ('cme_btc_regular', 'America/New_York', False),
])
def test_calendar_timezone_consistency(name, zone, accepted):
    if accepted:
        assert validate_calendar_timezone(name, zone) is None
    else:
        with pytest.raises(ValueError, match='INVALID_PARAMS:'):
            validate_calendar_timezone(name, zone)


@pytest.mark.parametrize('name', ['unknown', '', None, {}, 'none'])
def test_model_unknown_name_rejected(name):
    with pytest.raises(ValueError, match='INVALID_PARAMS:'): RegularCalendar(name)


@pytest.mark.parametrize('name', ['unknown', '', None, {}])
def test_timezone_unknown_calendar_rejected(name):
    with pytest.raises(ValueError, match='INVALID_PARAMS:'): validate_calendar_timezone(name, 'UTC')


@pytest.mark.parametrize('timeframe', ['0m', '1M', '15min'])
def test_nonfixed_bar_rejected(timeframe):
    with pytest.raises(ValueError, match='INVALID_PARAMS:'): US.contains_bar(dt('2026-01-05T15:00'), timeframe)


def test_naive_inputs_mean_utc():
    assert US.is_open(datetime(2026, 1, 5, 14, 30))
    assert CME.trading_day(datetime(2026, 1, 4, 23, 30)) == date(2026, 1, 5)


def test_aware_inputs_preserve_the_instant():
    instant = datetime.fromisoformat('2026-01-05T09:30-05:00')
    assert US.is_open(instant)
    assert US.next_open(instant, inclusive=True) == dt('2026-01-05T14:30')


def test_regular_model_disclosure_and_no_holiday_exclusion():
    assert '不含节假日与半日市' in REGULAR_CALENDAR_ASSUMPTION
    assert 'BTC 现货行情代理 CME' in REGULAR_CALENDAR_ASSUMPTION
    assert US.is_open(dt('2027-01-01T15:00'))
    assert CME.is_open(dt('2027-01-01T15:00'))
