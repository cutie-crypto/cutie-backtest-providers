"""Intrinsic wall-clock events, causal close decisions and next-open market fills."""
import bisect
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import re
from zoneinfo import ZoneInfo

from backtesting import Strategy
from strategy_time_layer import (TimeConfig, TimeContext, expiry_due, utc_datetime, fixed_timeframe_milliseconds, tzdata_version,
                                 gap_tolerant_context)
from strategy_risk_overlay import RiskState

ENTRY_SCHEMA = {
    'time_entry_at': {'type': 'string', 'default': ''},
    'time_entry_weekday': {'type': 'integer', 'default': -1, 'minimum': -1, 'maximum': 6},
    'time_entry_monthday': {'type': 'integer', 'default': 0, 'minimum': 0, 'maximum': 31},
}
INTRINSIC_KEYS = frozenset((*ENTRY_SCHEMA, 'time_timezone', 'time_max_holding_minutes',
                          'time_flatten_at', 'time_flatten_weekdays'))


@dataclass(frozen=True)
class CalendarConfig:
    at: str
    weekday: int
    monthday: int
    direction: str
    clock: TimeConfig

    @classmethod
    def parse(cls, params):
        at = params.get('time_entry_at', '')
        weekday = params.get('time_entry_weekday', -1)
        monthday = params.get('time_entry_monthday', 0)
        if type(at) is not str or type(weekday) is not int or type(monthday) is not int:
            raise ValueError('INVALID_PARAMS:calendar entry types must be string/integer/integer')
        if at and not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', at):
            raise ValueError('INVALID_PARAMS:time_entry_at must be HH:MM')
        clock = TimeConfig.parse({'time_layer_enabled': True,
            **{k: v for k, v in params.items() if k in INTRINSIC_KEYS and k not in ENTRY_SCHEMA}})
        if not -1 <= weekday <= 6 or not 0 <= monthday <= 31 or (weekday != -1 and monthday):
            raise ValueError('INVALID_PARAMS:calendar weekday/monthday must be in range and mutually exclusive')
        if not at and (weekday != -1 or monthday or clock.max_holding_minutes or clock.flatten_at):
            raise ValueError('INVALID_PARAMS:calendar timing requires time_entry_at')
        if at and not (clock.max_holding_minutes or clock.flatten_at):
            raise ValueError('INVALID_PARAMS:calendar requires holding minutes or flatten time')
        direction = params.get('direction', 'long')
        if direction not in ('long', 'short'):
            raise ValueError('INVALID_PARAMS:calendar direction must be long or short')
        return cls(at, weekday, monthday, direction, clock)

    def event(self, day):
        if not self.at or (self.weekday != -1 and day.weekday() != self.weekday) or (self.monthday and day.day != self.monthday):
            return None
        hour, minute = map(int, self.at.split(':'))
        wall = datetime(day.year, day.month, day.day, hour, minute)
        zone = ZoneInfo(self.clock.timezone_name)
        instant = wall.replace(tzinfo=zone, fold=0).astimezone(timezone.utc)
        return instant if instant.astimezone(zone).replace(tzinfo=None) == wall else None

    def validate_grid(self, timeframe, start, end):
        step = timedelta(milliseconds=fixed_timeframe_milliseconds(timeframe))
        if self.clock.max_holding_minutes and timedelta(minutes=self.clock.max_holding_minutes) % step:
            raise ValueError('INVALID_PARAMS:calendar holding deadline cuts through a candle')
        start, end = utc_datetime(start), utc_datetime(end)
        zone = ZoneInfo(self.clock.timezone_name)
        day, last = start.astimezone(zone).date(), end.astimezone(zone).date()
        while day <= last:
            event = self.event(day)
            if event is not None and start <= event < end and (event-start) % step:
                raise ValueError('INVALID_PARAMS:calendar entry cuts through a candle')
            if self.clock.flatten_at and self.clock.flatten_weekdays & (1 << day.weekday()):
                hour, minute = map(int, self.clock.flatten_at.split(':'))
                wall = datetime(day.year, day.month, day.day, hour, minute)
                cutoff = wall.replace(tzinfo=zone, fold=0).astimezone(timezone.utc)
                if cutoff.astimezone(zone).replace(tzinfo=None) == wall and start <= cutoff < end and (cutoff-start) % step:
                    raise ValueError('INVALID_PARAMS:calendar flatten cuts through a candle')
            day += timedelta(days=1)

    def latest_exit_fill(self, entry, period, holding_bars):
        """P-LOW5-F2: the latest open an entry filled at `entry` can exit-fill at (expiry_due's clocks).

        Each configured clock is checked from the fill bar's close on; the first due decision fills at the
        next open. Price exits (stop / take) only fill earlier. None: no clock bounds the holding.
        """
        first = entry + period  # the fill bar's own close is the first exit decision
        candidates = []
        if self.clock.max_holding_minutes:
            candidates.append(max(entry + timedelta(minutes=self.clock.max_holding_minutes), first))
        if holding_bars:
            candidates.append(entry + holding_bars * period)
        if self.clock.flatten_at:
            zone = ZoneInfo(self.clock.timezone_name)
            hour, minute = map(int, self.clock.flatten_at.split(':'))
            day = entry.astimezone(zone).date()
            for _ in range(15):
                if self.clock.flatten_weekdays & (1 << day.weekday()):
                    wall = datetime(day.year, day.month, day.day, hour, minute)
                    cutoff = wall.replace(tzinfo=zone, fold=0).astimezone(timezone.utc)
                    if cutoff.astimezone(zone).replace(tzinfo=None) == wall and entry <= cutoff:
                        candidates.append(max(cutoff, first))
                        break
                day += timedelta(days=1)
        return min(candidates) if candidates else None


def make_calendar_strategy(mixin, config, risk, initial_capital):
    class CalendarScheduleStrategy(mixin, Strategy):
        _risk = risk
        _initial_capital = initial_capital
        _calendar_timeframe = None
        _calendar_gap_opens = ()  # P-LOW5-F2: UTC opens of missing main-range candles (set by the run)

        def init(self):
            timeframe = self._calendar_timeframe
            if timeframe is None:
                milliseconds = int((self.data.index[1]-self.data.index[0]).total_seconds()*1000)
                if milliseconds <= 0 or milliseconds % 60000:
                    raise ValueError('INVALID_PARAMS:calendar requires fixed minute candles')
                timeframe = f'{milliseconds//60000}m'
            # P-LOW5-F2: holes only reach this engine after the run judged them within tolerance; every
            # entry they could reach is skipped in _submit_entry.
            build = gap_tolerant_context if self._calendar_gap_opens else TimeContext.build
            self._calendar_clock = build(config.clock, timeframe, self.data.index)
            self._gap_opens = sorted(map(utc_datetime, self._calendar_gap_opens))
            self._gap_days = {t.astimezone(self._calendar_clock.zone).date() for t in self._gap_opens}
            self.calendar_gap_masked_events = []
            config.validate_grid(timeframe, self.data.index[0], self._calendar_clock.decision_utc(self.data.index[-1]))
            self.calendar_events = []
            self._seen_events = set()
            self._pending_entry = None
            self._active_event = None
            self._risk_init()
            self._risk_trade = self._risk_state = self._risk_exit_reason = None
            # Fill-time validation sees only the current executable open, before the
            # broker creates a trade. No future price is read at signal submission.
            original = self._broker._process_orders
            def guarded_orders():
                # Backtest starts next() at bar 1. Process bar 0's closed event
                # here, before the first executable open; only Close[-2] is read.
                if self._broker._i == 1:
                    event = config.event(utc_datetime(self.data.index[-1]).astimezone(self._calendar_clock.zone).date())
                    if event is not None and event == utc_datetime(self.data.index[-1]) and event not in self._seen_events:
                        self._seen_events.add(event)
                        self._submit_entry(event, float(self.data.Close[-2]), has_next=True, bar=0)
                pending = self._pending_entry
                if pending and pending[0] in self.orders:
                    order, record, stop = pending
                    price = float(self.data.Open[-1])
                    if stop is not None and ((order.is_long and price <= stop) or (order.is_short and price >= stop)):
                        order.cancel()
                        record.update(status='skipped', reason='gap_stop_wrong_side', fill_open=price)
                        self._pending_entry = None
                trades_before = len(self._broker.trades)
                original()
                # P-LOW1: the sizing hook (installed under original) drops a rejected entry from the
                # queue without a fill; the reason itself is already in position_sizing.rejections.
                pending = self._pending_entry
                if (self._risk.get('position_sizing_enabled') and pending is not None
                        and pending[0] not in self.orders and len(self._broker.trades) <= trades_before):
                    pending[1].update(status='skipped', reason='sizing_rejected')
                    self._pending_entry = None
            self._broker._process_orders = guarded_orders

        def _sizing_template_stop(self, order):
            # 10-B2d: only consulted by position_size_risk_pct, after the gap guard above. The stop is
            # frozen at the signal close (the same value _risk_check_exit installs after the fill).
            # calendar_stop_enabled=false freezes no stop: None makes the fill hook reject the entry
            # with missing_initial_stop (fail-closed), never a zero or default distance.
            pending = self._pending_entry
            if not pending or pending[0] is not order or pending[2] is None:
                return None
            return Decimal(str(pending[2]))

        def _holding_expiry(self):
            trade = self.trades[-1]
            intrinsic = expiry_due(holding_bars=self._risk.get('max_holding_bars', 0),
                entry_bar=trade.entry_bar, bar=len(self.data)-1, entry_utc=trade.entry_time,
                bar_open=self.data.index[-1], context=self._calendar_clock)
            generic = super()._holding_expiry()
            return intrinsic if intrinsic.due else generic

        def _record_holding_expiry(self, fact):
            self._risk_exit_reason = 'time_expiry'
            if fact.flatten_delay_bars is not None:
                self._calendar_clock.flatten_delays.append(fact.flatten_delay_bars)

        def _risk_check_exit(self):
            trade = self.trades[-1]
            if self._risk_trade is not trade:
                _, record, stop = self._pending_entry
                self._pending_entry = None
                self._active_event = record
                record.update(status='filled', entry_utc=utc_datetime(trade.entry_time).isoformat(), entry_price=trade.entry_price)
                entry = Decimal(str(trade.entry_price))
                take_pct = self._risk.get('take_profit_pct')
                take = entry * Decimal(str(1 + take_pct * (1 if trade.is_long else -1))) if take_pct else None
                self._risk_trade = trade
                self._risk_state = RiskState(entry, Decimal(str(stop)) if stop is not None else None,
                    take, abs(entry-Decimal(str(stop))) if stop is not None else None, 'long' if trade.is_long else 'short')
            closed = self._risk_layer_check_exit()
            if closed:
                self._active_event['exit_reason'] = self._risk_exit_reason
            return closed

        def next(self):
            opened = self.data.index[-1]
            decision = self._calendar_clock.decision_utc(opened)
            day = decision.astimezone(self._calendar_clock.zone).date()
            event = config.event(day)
            due = event is not None and event == decision and event not in self._seen_events
            if due:
                self._seen_events.add(event)
            if self.position:
                self._risk_check_exit()
                if due:
                    self.calendar_events.append(dict(event_utc=event.isoformat(), status='skipped', reason='already_holding'))
                return
            if not due or self.orders:
                return
            self._submit_entry(event, float(self.data.Close[-1]),
                has_next=utc_datetime(opened) < self._calendar_clock.last_open_utc, bar=len(self.data)-1)

        def _gap_mask_reason(self, event):
            # P-LOW5-F2: no entry on a local day holding a missing candle, nor when the holding window
            # (signal bar .. latest exit fill) touches one -- holding bars would otherwise count across it.
            if event.astimezone(self._calendar_clock.zone).date() in self._gap_days:
                return 'main_range_gap_day'
            period = self._calendar_clock.period
            end = config.latest_exit_fill(event, period, self._risk.get('max_holding_bars', 0))
            k = bisect.bisect_left(self._gap_opens, event - period)
            if k < len(self._gap_opens) and (end is None or self._gap_opens[k] <= end):
                return 'main_range_gap_holding_window'
            return None

        def _submit_entry(self, event, signal_close, *, has_next, bar):
            record = dict(event_utc=event.isoformat(), status='skipped', reason=None)
            self.calendar_events.append(record)
            if not has_next:
                record['reason'] = 'no_next_open'
                return
            reason = self._gap_mask_reason(event) if self._gap_opens else None
            if reason:
                record['reason'] = reason
                self.calendar_gap_masked_events.append(dict(event_utc=event.isoformat(), reason=reason))
                return
            if self._time_config is not None and not self._time_context.allow_entry(event):
                record['reason'] = 'entry_gate'
                return
            # Judgment bar = the event's closed bar (bar 0's event is submitted at broker step 1).
            if not self._filter_allow_entry(bar):
                record['reason'] = 'entry_filter'
                return
            pct = self._risk.get('stop_loss_pct')
            stop = signal_close * (1 - pct if config.direction == 'long' else 1 + pct) if pct else None
            size = self._risk_entry_size()
            submit = self.buy if config.direction == 'long' else self.sell
            submit() if size is None else submit(size=size)
            if self.orders:
                record.update(status='submitted', stop_price=stop)
                self._pending_entry = (self.orders[-1], record, stop)
    return CalendarScheduleStrategy


def calendar_assumptions(config):
    return {'calendar_schedule': dict(timezone=config.clock.timezone_name, tzdata_version=tzdata_version(),
        entry_at=config.at, entry_weekday=config.weekday, entry_monthday=config.monthday, direction=config.direction,
        holding_minutes=config.clock.max_holding_minutes, flatten_at=config.clock.flatten_at,
        flatten_weekdays=config.clock.flatten_weekdays, decision_time='bar_open_plus_period', fill='next_bar_open_market',
        dst='nonexistent_skip_day_repeated_first_only', missing_monthday='skip', holding_repeat='skip',
        stop='frozen_signal_close_pct', stop_trigger='bar_high_low', gap_stop_wrong_side='cancel_before_fill',
        priority='stop_loss_before_time_expiry_before_take_profit',
        final_bar='engine_finalize_trades_settlement',
        note='到点收盘决策后下一根开盘市价成交；止损不保证按冻结价成交；回测末尾由引擎结算。')}
