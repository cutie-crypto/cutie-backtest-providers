"""Regular-session calendar foundations. No schema, runner or entry wiring.

Naive datetimes mean UTC, as in the existing time layer. All schedules are
constructed in the local calendar; closed instants have no trading day.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from strategy_time_layer import fixed_timeframe_milliseconds, utc_datetime
from strategy_time_series import PeriodBounds

CALENDAR_TIMEZONES = {
    'us_equity_regular': 'America/New_York',
    'cme_btc_regular': 'America/Chicago',
}
REGULAR_CALENDAR_ASSUMPTION = '常规日历，不含节假日与半日市；BTC 现货行情代理 CME，不代表完整交易所日历。'


def validate_calendar_timezone(calendar_name: str, timezone_name: str) -> None:
    """Validate consistency only; the existing TimeConfig validates IANA names.

    ``none`` imposes no calendar timezone. Unknown calendars or a mismatched
    timezone fail with INVALID_PARAMS, suitable for a future pre-fetch adapter.
    """
    if calendar_name == 'none':
        return
    if not isinstance(calendar_name, str) or calendar_name not in CALENDAR_TIMEZONES:
        raise ValueError('INVALID_PARAMS:unknown time_calendar')
    required = CALENDAR_TIMEZONES[calendar_name]
    if timezone_name != required:
        raise ValueError(f'INVALID_PARAMS:time_calendar={calendar_name} requires time_timezone={required}')


@dataclass(frozen=True)
class RegularCalendar:
    """Named regular model; ``none`` is handled by the caller bypassing it.

    Session bounds are [open, close). A bar closing exactly at session close is
    fully inside. CME uses the next date for an evening session's trading day.
    """
    name: str

    def __post_init__(self):
        if not isinstance(self.name, str) or self.name not in CALENDAR_TIMEZONES:
            raise ValueError('INVALID_PARAMS:unknown regular calendar')

    @property
    def timezone_name(self) -> str:
        return CALENDAR_TIMEZONES[self.name]

    @property
    def _zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    def _session_for_day(self, trading_day: date) -> PeriodBounds | None:
        if trading_day.weekday() >= 5:
            return None
        if self.name == 'us_equity_regular':
            start_day, start_clock, end_clock = trading_day, time(9, 30), time(16)
        else:
            start_day, start_clock, end_clock = trading_day - timedelta(days=1), time(17), time(16)
        start = datetime.combine(start_day, start_clock, self._zone).astimezone(timezone.utc)
        end = datetime.combine(trading_day, end_clock, self._zone).astimezone(timezone.utc)
        return PeriodBounds(start, end)

    def _session_at(self, instant: datetime) -> tuple[date, PeriodBounds] | None:
        opened = utc_datetime(instant)
        local = opened.astimezone(self._zone)
        trading_day = local.date()
        if self.name == 'cme_btc_regular' and local.time() >= time(17):
            trading_day += timedelta(days=1)
        bounds = self._session_for_day(trading_day)
        return (trading_day, bounds) if bounds is not None and bounds.start_utc <= opened < bounds.end_utc else None

    def is_open(self, instant: datetime) -> bool:
        """Whether a UTC instant lies in a regular session (close excluded)."""
        return self._session_at(instant) is not None

    def contains_bar(self, bar_open: datetime, timeframe: str) -> bool:
        """Check the ENTIRE fixed candle, including crossing a daily pause."""
        period = timedelta(milliseconds=fixed_timeframe_milliseconds(timeframe))
        session = self._session_at(bar_open)
        return session is not None and utc_datetime(bar_open) + period <= session[1].end_utc

    def trading_day(self, instant: datetime) -> date | None:
        """An open instant's session date; weekends/overnights/pauses => None."""
        session = self._session_at(instant)
        return session[0] if session is not None else None

    def next_open(self, instant: datetime, *, inclusive: bool = False) -> datetime:
        """Next SESSION open, strictly after instant unless inclusive=True.

        Inside a session this returns the following session's open, rather than
        the current instant. No holidays are skipped in this regular model.
        """
        instant = utc_datetime(instant)
        day = instant.astimezone(self._zone).date()
        for offset in range(8):
            session = self._session_for_day(day + timedelta(days=offset))
            if session is not None and (session.start_utc > instant or
                                        (inclusive and session.start_utc == instant)):
                return session.start_utc
        raise AssertionError('regular weekly schedule must have an opening within eight days')


def calendar_week_bounds(instant: datetime, calendar: RegularCalendar) -> PeriodBounds | None:
    """Calendar-aware 6d-compatible week bounds, or None for CLOSED instants.

    US: Monday 09:30 through Friday 16:00, New York.
    CME: Sunday 17:00 through Friday 16:00, Chicago.
    Bounds encompass internal closures; membership still requires an open
    session. Neither weekends nor intraday pauses get assigned to a week.
    """
    trading_day = calendar.trading_day(instant)
    if trading_day is None:
        return None
    monday = trading_day - timedelta(days=trading_day.weekday())
    first = calendar._session_for_day(monday)
    last = calendar._session_for_day(monday + timedelta(days=4))
    return PeriodBounds(first.start_utc, last.end_utc)
