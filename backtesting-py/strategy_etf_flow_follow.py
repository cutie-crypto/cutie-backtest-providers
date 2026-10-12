"""C38: spot BTC/ETH ETF flow, previous NYSE day, exact NY session opens.

Calendar sources (no new dependency):
https://s2.q4cdn.com/154085107/files/doc_news/NYSE-Group-Announces-2024-2025-and-2026-Holiday-and-Early-Closings-Calendar-2023.pdf
https://www.nyse.com/trade/hours-calendars
https://ir.theice.com/press/news-details/2024/The-New-York-Stock-Exchange-Will-Close-Markets-on-January-9-to-Honor-the-Passing-of-Former-President-Jimmy-Carter-on-National-Day-of-Mourning/default.aspx
The existing RegularCalendar intentionally excludes holidays/half days, so it cannot supply this calendar.
The module never fetches data. Decisions are logged, fills are derived from result.v2 after the run.
"""
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

TOOL_NAME = 'etf_flow_follow'
CENTRAL_EXCHANGE = 'AGGREGATED'
CENTRAL_METRIC = 'etf_flow'
CENTRAL_INTERVAL = '1d'
GAP_REASON = 'etf_flow_data_gap'
HISTORY_REASON = 'etf_flow_history_unavailable'
CALENDAR_REASON = 'etf_calendar_unavailable'
EXIT_REASON = 'us_session_close'
DAY = 86400
BAR_SECONDS = 900
ZONE = ZoneInfo('America/New_York')
CALENDAR_FIRST = date(2024, 1, 1)
CALENDAR_LAST = date(2026, 12, 31)
HOLIDAYS = frozenset(date.fromisoformat(d) for d in (
    '2024-01-01', '2024-01-15', '2024-02-19', '2024-03-29', '2024-05-27',
    '2024-06-19', '2024-07-04', '2024-09-02', '2024-11-28', '2024-12-25',
    '2025-01-01', '2025-01-09', '2025-01-20', '2025-02-17', '2025-04-18',
    '2025-05-26', '2025-06-19', '2025-07-04', '2025-09-01', '2025-11-27', '2025-12-25',
    '2026-01-01', '2026-01-19', '2026-02-16', '2026-04-03', '2026-05-25',
    '2026-06-19', '2026-07-03', '2026-09-07', '2026-11-26', '2026-12-25',
))
HALF_DAYS = frozenset(date.fromisoformat(d) for d in (
    '2024-07-03', '2024-11-29', '2024-12-24',
    '2025-07-03', '2025-11-28', '2025-12-24', '2026-11-27', '2026-12-24',
))
SCHEMA = {
    'flow_threshold_usd': dict(type='number', default=300000000, minimum=1000000, maximum=10000000000),
    'stop_loss_pct': dict(type='number', default=2, minimum=0.1, maximum=50),
}


class CalendarUnavailable(ValueError):
    pass


def require_calendar(day):
    if not CALENDAR_FIRST <= day <= CALENDAR_LAST:
        raise CalendarUnavailable('INSUFFICIENT_DATA:etf_calendar_unavailable')


def is_trading_day(day):
    require_calendar(day)
    return day.weekday() < 5 and day not in HOLIDAYS


def previous_trading_day(day):
    day -= timedelta(days=1)
    while not is_trading_day(day):
        day -= timedelta(days=1)
    return day


def day_ts(day):
    return int(datetime.combine(day, time(), timezone.utc).timestamp())


def session_open_at(day):
    return int(datetime.combine(day, time(9, 30), ZONE).timestamp())


def session(day):
    if not is_trading_day(day):
        return None
    return dict(day=day.isoformat(),
                open_at=session_open_at(day),
                close_at=int(datetime.combine(day, time(13 if day in HALF_DAYS else 16), ZONE).timestamp()),
                flow_ts=day_ts(previous_trading_day(day)))


class EtfFlowConfig:
    def __init__(self, values):
        self.values = values
        self.sessions = {}
        self.rows = {}
        self.required_ts = ()
        self.series_report = {}

    @classmethod
    def parse(cls, params):
        values = {}
        for key, spec in SCHEMA.items():
            value = params.get(key, spec['default'])
            if type(value) not in (int, float) or not spec['minimum'] <= value <= spec['maximum']:
                raise ValueError(f"INVALID_PARAMS:{key} must be a number in [{spec['minimum']}, {spec['maximum']}]")
            values[key] = value
        return cls(values)

    def plan(self, start_at, end_at):
        # Request bounds are UTC dates, matching the metric date and exclusive end of the provider.
        first = datetime.fromtimestamp(start_at, timezone.utc).date()
        last = datetime.fromtimestamp(end_at - 1, timezone.utc).date()
        require_calendar(first)
        require_calendar(last)
        required = set()
        sessions = {}
        day = first
        while day <= last:
            if is_trading_day(day):
                opening = session_open_at(day)
                # Only the predecessor P(T) of each in-window session T is consumed. Include a session
                # opening exactly at end_at per worker-2's ruling; its own flow is never required.
                if start_at <= opening <= end_at:
                    event = session(day)
                    required.add(event['flow_ts'])
                    sessions[opening] = event
            day += timedelta(days=1)
        self.required_ts = tuple(sorted(required))
        self.sessions = sessions
        return self.required_ts

    def bind(self, rows, *, symbol, ignored_rows_count=0):
        self.rows = {row['ts']: row for row in rows}
        self.series_report = dict(symbol=symbol, exchange=CENTRAL_EXCHANGE, metric=CENTRAL_METRIC,
                                  interval=CENTRAL_INTERVAL, unit='usd', count=len(rows), ignored_rows_count=ignored_rows_count,
                                  revision=rows[0]['revision'] if rows else None,
                                  required_dates=[datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
                                                  for ts in self.required_ts],
                                  available_at_rule='flow_date D is readable from D+1 00:00 UTC; fetched_at is ignored',
                                  gap_policy='fail_closed_on_missing_previous_NYSE_trading_day')

    def readable_flow(self, event, decision_at):
        row = self.rows.get(event['flow_ts'])
        if row is None or row['available_at'] > decision_at:
            return None
        return row

    def report(self, trades, decisions):
        # Evidence is joined to actual result.v2 fills; next() records only queued decisions.
        exits = {d['fill_at']: d['reason'] for d in decisions if d['action'] == 'exit'}
        fills = []
        for trade in trades:
            event = self.sessions.get(trade['opened_at'])
            fills.append(dict(seq=trade['seq'], opened_at=trade['opened_at'], closed_at=trade['closed_at'],
                              entry_price=trade['entry_price'], exit_price=trade['exit_price'],
                              flow_ts=event['flow_ts'] if event else None,
                              reason=exits.get(trade['closed_at'], 'period_end')))
        return dict(series=self.series_report, calendar=dict(name='NYSE', timezone='America/New_York',
                    first_date=CALENDAR_FIRST.isoformat(), last_date=CALENDAR_LAST.isoformat(),
                    sessions=list(self.sessions.values())), parameters=dict(self.values),
                    rules=dict(entry='previous NYSE trading day flow >= flow_threshold_usd; buy at 09:30 ET open',
                               exit='16:00 ET open (13:00 on half days), or stop touched by low then next open',
                               stop='percent frozen at actual fill; stop takes precedence on the same bar',
                               direction='spot long only; 做空镜像未开放'),
                    decisions=list(decisions), fills=fills)


def make_strategy(mixin, config, risk, initial_capital):
    from backtesting import Strategy
    import pandas as pd

    class EtfFlowFollowStrategy(mixin, Strategy):
        _risk = risk
        _initial_capital = initial_capital

        def init(self):
            self._risk_init()
            self.etf_decisions = []
            self._etf_opens = [int(pd.Timestamp(t).value // 10**9) for t in self.data.index]
            self._etf_attempted = set()

        def _sizing_template_stop(self, order):
            fill = Decimal(str(self._broker._adjusted_price(order.size, self.data.Open[-1])))
            return fill * (1 - Decimal(str(config.values['stop_loss_pct'])) / 100)

        def _decision(self, action, reason, event=None):
            self.etf_decisions.append(dict(decision_at=self._etf_opens[len(self.data) - 1] + BAR_SECONDS,
                                          fill_at=self._etf_opens[len(self.data) - 1] + BAR_SECONDS,
                                          action=action, reason=reason,
                                          flow_ts=event['flow_ts'] if event else None))

        def _record_risk_exit(self, native=False):
            self._decision('exit', self._risk_exit_reason or 'stop_loss')
            if native:
                self.etf_decisions[-1]['fill_at'] = self._etf_opens[len(self.data) - 1]

        def _risk_native_record_exit(self):
            self._record_risk_exit(native=True)

        def next(self):
            bar = len(self.data) - 1
            # Require an actual next open: period-end settlement cannot fabricate an entry at the tail.
            if bar + 1 >= len(self._etf_opens):
                return
            decision_at = self._etf_opens[bar] + BAR_SECONDS
            if self.position:
                self._risk_exit_reason = None
                if self._risk_check_exit():
                    self._record_risk_exit()
                    return
                trade_event = config.sessions.get(int(pd.Timestamp(self.trades[-1].entry_time).value // 10**9))
                if trade_event is not None and decision_at == trade_event['close_at']:
                    self._decision('exit', EXIT_REASON, trade_event)
                    self.position.close()
                return
            event = config.sessions.get(decision_at)
            if event is None or self.orders or decision_at in self._etf_attempted:
                return
            self._etf_attempted.add(decision_at)  # No catch-up or re-entry after a stop.
            row = config.readable_flow(event, decision_at)
            if row is None:
                self._decision('skip', 'flow_not_available', event)
                return
            if Decimal(row['value']) >= Decimal(str(config.values['flow_threshold_usd'])):
                before = len(self.orders)
                self._risk_buy()
                self._decision('entry' if len(self.orders) > before else 'skip',
                               'flow_at_or_above_threshold' if len(self.orders) > before else 'entry_gate', event)

    return EtfFlowFollowStrategy


def schema():
    return deepcopy(SCHEMA)
