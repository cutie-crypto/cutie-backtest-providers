"""P-EVENT0: inline event windows (no data table is read).

Entry = the open of the bar ``bars_before`` bars before the event bar (the bar with
open <= ts < next open); the order is queued at the close of the bar before it and fills
at that open. Exit = the close of the ``bars_after``-th held bar (entry bar counts as 1,
same count as max_holding_bars), filled at the next open like calendar_schedule's timed
exit, or an earlier risk-layer exit. Everything that cannot be placed is skipped
(fail-closed) and every event's outcome is reported.
"""
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timezone
import re

from backtesting import Strategy
from strategy_time_layer import utc_datetime

MAX_EVENTS = 50
MAX_LABEL = 64
SCHEMA = {
    'events': {
        'type': 'array', 'minItems': 1, 'maxItems': MAX_EVENTS,
        'items': {
            'type': 'object', 'additionalProperties': False, 'required': ['ts_utc', 'label'],
            'properties': {
                'ts_utc': {'type': 'string', 'format': 'date-time'},
                'label': {'type': 'string', 'minLength': 1, 'maxLength': MAX_LABEL},
            },
        },
    },
    'bars_before': {'type': 'integer', 'default': 0, 'minimum': 0, 'maximum': 48},
    'bars_after': {'type': 'integer', 'default': 12, 'minimum': 1, 'maximum': 96},
    'direction': {'type': 'string', 'default': 'long', 'enum': ['long', 'short']},
}
# Explicit offset required: a naive timestamp has no unambiguous instant.
_TS = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})')


def _parse_ts(value):
    if type(value) is not str or not _TS.fullmatch(value):
        raise ValueError('INVALID_PARAMS:events[].ts_utc must be ISO8601 with Z or +hh:mm offset')
    try:
        parsed = datetime.fromisoformat(value[:-1] + '+00:00' if value.endswith('Z') else value)
    except ValueError:
        raise ValueError('INVALID_PARAMS:events[].ts_utc is not a valid ISO8601 instant')
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class EventWindowConfig:
    events: tuple  # ((utc datetime, label), ...) sorted by instant
    bars_before: int
    bars_after: int
    direction: str

    @classmethod
    def parse(cls, params):
        events = params.get('events')
        if type(events) is not list or not 1 <= len(events) <= MAX_EVENTS:
            raise ValueError(f'INVALID_PARAMS:events must be a list of 1-{MAX_EVENTS} items')
        parsed, seen = [], set()
        for item in events:
            if type(item) is not dict or set(item) != {'ts_utc', 'label'}:
                raise ValueError('INVALID_PARAMS:events[] items must have exactly ts_utc and label')
            label = item['label']
            if type(label) is not str or not label.strip() or len(label) > MAX_LABEL:
                raise ValueError(f'INVALID_PARAMS:events[].label must be a non-empty string of at most {MAX_LABEL} chars')
            ts = _parse_ts(item['ts_utc'])
            if ts in seen:
                raise ValueError('INVALID_PARAMS:events[].ts_utc must not repeat')
            seen.add(ts)
            parsed.append((ts, label))
        bars_before = params.get('bars_before', 0)
        bars_after = params.get('bars_after', 12)
        direction = params.get('direction', 'long')
        if type(bars_before) is not int or not 0 <= bars_before <= 48:
            raise ValueError('INVALID_PARAMS:bars_before must be an integer within 0-48')
        if type(bars_after) is not int or not 1 <= bars_after <= 96:
            raise ValueError('INVALID_PARAMS:bars_after must be an integer within 1-96')
        if direction not in ('long', 'short'):
            raise ValueError('INVALID_PARAMS:event_window direction must be long or short')
        return cls(tuple(sorted(parsed, key=lambda pair: pair[0])), bars_before, bars_after, direction)


def make_event_window_strategy(mixin, config, risk, initial_capital):
    class EventWindowStrategy(mixin, Strategy):
        _risk = risk
        _initial_capital = initial_capital

        def init(self):
            self._risk_init()
            opens = [utc_datetime(t) for t in self.data.index]
            period_ns = getattr(self, '_risk_timeframe_ns', None) or int(
                (self.data.index[1] - self.data.index[0]).total_seconds() * 10**9)
            period = period_ns / 10**9
            stamps = [t.timestamp() for t in opens]
            last = len(stamps) - 1
            self.event_window_events = []
            self._by_decision = {}
            for ts, label in config.events:
                record = dict(ts_utc=ts.isoformat(), label=label)
                self.event_window_events.append(record)
                at = ts.timestamp()
                bar = bisect_right(stamps, at) - 1
                if bar < 0:
                    record.update(status='skipped_out_of_range', reason='event_before_data_start')
                    continue
                if at >= stamps[bar] + period:
                    record.update(status='skipped_out_of_range',
                                  reason='event_after_data_end' if bar == last else 'event_in_data_gap')
                    continue
                entry = bar - config.bars_before
                exit_bar = entry + config.bars_after - 1
                record['event_bar_utc'] = opens[bar].isoformat()
                if entry < 0:
                    record.update(status='skipped_out_of_range', reason='entry_before_data_start')
                    continue
                # next() first runs at bar 1, so the earliest queued entry fills at bar 2's open.
                if entry < 2:
                    record.update(status='skipped_out_of_range', reason='entry_before_first_decision')
                    continue
                # The window-end exit is queued at exit_bar's close and needs a next open to fill.
                if exit_bar + 1 > last:
                    record.update(status='skipped_out_of_range', reason='window_past_data_end')
                    continue
                record.update(entry_bar_utc=opens[entry].isoformat(), exit_decision_bar_utc=opens[exit_bar].isoformat())
                self._by_decision.setdefault(entry - 1, []).append(record)
            self._pending = None   # (order, record) queued, not yet filled
            self._active = None    # (trade, record) open
            self._exit_reason = None
            self._closed_seen = 0  # closed_trades already attributed to an event

        def _settle(self):
            if self._pending is not None:
                order, record = self._pending
                if self.trades and (self._active is None or self.trades[-1] is not self._active[0]):
                    trade = self.trades[-1]
                    record.update(status='entered', entry_utc=utc_datetime(trade.entry_time).isoformat(),
                                  entry_price=trade.entry_price, exits=[])
                    self._active, self._pending, self._exit_reason = (trade, record), None, None
                    self._closed_seen = len(self.closed_trades)
                elif order not in self.orders:
                    # Dropped at the fill (e.g. position sizing rejection) without a trade.
                    record.update(status='rejected_at_fill')
                    self._pending = None
            if self._active is None:
                return
            trade, record = self._active
            # Every fill that closed (part of) the held trade is one result.v2 row: partial take-profits
            # and the final close alike. Only one close order is in flight at a time, so the reason set
            # when it was queued belongs to the fill.
            for closed in self.closed_trades[self._closed_seen:]:
                if closed.entry_time == trade.entry_time:
                    record['exits'].append(dict(time=utc_datetime(closed.exit_time).isoformat(),
                                                price=closed.exit_price, reason=self._exit_reason or 'risk_exit'))
            self._closed_seen = len(self.closed_trades)
            if trade not in self.trades:
                last = record['exits'][-1]
                record.update(exit_reason=last['reason'], exit_utc=last['time'], exit_price=last['price'])
                self._active = None

        def event_window_finish(self, reason):
            """After bt.run(): attribute closes made by finalize_trades (reason given by the caller)."""
            if self._active is not None and self._active[0] not in self.trades:
                self._exit_reason = reason
                self._settle()
            for record in self.event_window_events:
                if record['status'] == 'entered' and 'exit_reason' not in record:
                    record['exit_reason'] = reason

        def next(self):
            bar = len(self.data) - 1
            self._settle()
            if self.position and self._active is not None:
                trade, record = self._active
                self._risk_exit_reason = None
                if self._risk_check_exit():
                    self._exit_reason = self._risk_exit_reason or 'risk_exit'
                elif bar >= trade.entry_bar + config.bars_after - 1 and not any(
                        order.parent_trade is trade for order in self.orders):
                    self._exit_reason = 'window_end'
                    self.position.close()
            for record in self._by_decision.get(bar, ()):
                if self.position or self.orders or self._pending is not None:
                    record.update(status='skipped_in_position')
                    continue
                count = len(self.orders)
                self._risk_buy() if config.direction == 'long' else self._risk_sell()
                if len(self.orders) == count:
                    record.update(status='blocked_by_time_or_filter')
                    continue
                record.update(status='submitted')
                self._pending = (self.orders[-1], record)
    return EventWindowStrategy


def event_window_assumptions(config):
    return dict(bars_before=config.bars_before, bars_after=config.bars_after, direction=config.direction,
                event_bar='open_le_ts_lt_next_open', entry='open_of_bar_bars_before_earlier_than_event_bar',
                entry_fill='queued_at_previous_close_next_open_market',
                exit='close_of_bars_after_th_held_bar_entry_bar_counts_1_next_open_market_or_risk_exit_first',
                in_position='skip_next_event', out_of_range='skip', data_source='inline_params_only',
                note='事件根 = 开盘 ≤ ts < 下一根开盘；入场 = 事件根往前 bars_before 根的开盘价（前一根收盘下单）；'
                     '到期 = 持有第 bars_after 根收盘下单、下一根开盘成交；风控出场先到者优先。'
                     '一条 entered 事件 = 一次持仓生命周期，result.v2 可能有多行（分批止盈每次成交一行），'
                     '逐次出场见该事件的 exits；exit_reason / exit_price 取最后一次出场。')
