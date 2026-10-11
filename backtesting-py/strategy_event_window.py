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
from backtesting._util import _indicator_warmup_nbars
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
            # Scheduling and break detection require next() to start at main bar 1.
            # This is engine indicator warmup, not the external filter-history prefix.
            if _indicator_warmup_nbars(self) != 0:
                raise ValueError("event_window requires zero engine indicator warmup")
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
                record.update(entry_bar_utc=opens[entry].isoformat(), planned_exit_decision_bar_utc=opens[exit_bar].isoformat())
                self._by_decision.setdefault(entry - 1, []).append(record)
            self._decisions = {}   # decision bar -> reason of the close order queued at its close
            self._next_bar = -1    # last bar next() ran on; short of the last bar => the run broke there

        @property
        def event_window_log(self):
            """What settle_event_window_exits needs besides result.v2: decisions, bar opens, break bar."""
            opens = [int(utc_datetime(t).timestamp()) for t in self.data.index]
            stop = self._next_bar + 1
            return dict(decisions=dict(self._decisions), bar_opens=opens,
                        stop_bar=stop if stop < len(opens) else None)

        def next(self):
            bar = self._next_bar = len(self.data) - 1
            if self.position:
                trade = self.trades[-1]
                self._risk_exit_reason = None
                if self._risk_check_exit():
                    self._decisions[bar] = self._risk_exit_reason or 'risk_exit'
                elif bar >= trade.entry_bar + config.bars_after - 1 and not any(
                        order.parent_trade is trade for order in self.orders):
                    self._decisions[bar] = 'window_end'
                    # P6: the event window's bars_after ran out -- a time exit, not a signal.
                    self.__dict__['_exit_kind_pending'] = 'time_exit'
                    self.position.close()
            for record in self._by_decision.get(bar, ()):
                if self.position or self.orders:
                    record.update(status='skipped_in_position')
                    continue
                count = len(self.orders)
                self._risk_buy() if config.direction == 'long' else self._risk_sell()
                if len(self.orders) == count:
                    record.update(status='blocked_by_time_or_filter')
                    continue
                record.update(status='submitted')
    return EventWindowStrategy


def _utc(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def settle_event_window_exits(events, trades_v2, isolated_risk, decisions, bar_opens, stop_bar):
    """After the run: derive every submitted event's outcome from the settled result.v2 rows.

    The strategy only recorded its decisions (which events it submitted, and the reason of each close
    order by decision bar). Fills happen inside the broker, also where next() never runs again
    (insolvency break, finalize), so they are read back here and never tracked live.
    A row belongs to the submitted event whose entry bar opened it (opened_at); no row => the order
    was dropped at the fill (rejected_at_fill). A row owned by no submitted event or by several breaks
    the construction (every fill comes from an event order) and raises.
    Reason per row: isolated liquidation (row listed in isolated_risk) > the decision whose close order
    filled at the row's closed_at > insolvency (row closed on the bar the run broke on) >
    engine_finalize_trades_settlement. exit_reason / exit_utc / exit_price follow the last exit.
    Prices (entry_price, exits[].price, exit_price) are the decimal strings of the result.v2 rows, never floats.
    Event status values: skipped_out_of_range, skipped_in_position, blocked_by_time_or_filter,
    rejected_at_fill, entered, and skipped_run_ended (reason insolvency_break: the run broke on
    insolvency before the event's decision bar, so no decision was ever made for it). Any event still
    without a status at the end gets skipped_run_ended; with no early break that is impossible and raises.
    planned_exit_decision_bar_utc is the plan fixed at init: a due close that meets an order still in
    flight is postponed by one bar, the actual exits are in exits.
    A decision's close order fills at the next bar's open; one queued on the last bar is filled by the
    engine's finalize re-run at that bar's open, ahead of finalize's own close. Every decision is one
    close order on the single open trade, so it accounts for at most one row: rows sharing a closed_at
    take the decisions in bar order, and the rows left over are the engine's own closes (the
    insolvency close at that bar's close, or finalize).
    """
    liquidated = {item['seq'] for item in (isolated_risk or {}).get('liquidations', ())}
    by_fill = {}
    for bar in sorted(decisions):
        by_fill.setdefault(bar_opens[min(bar + 1, len(bar_opens) - 1)], []).append(decisions[bar])
    insolvent_at = bar_opens[stop_bar] if stop_bar is not None else None
    submitted = [record for record in events if record.get('status') == 'submitted']
    rows = {id(record): [] for record in submitted}
    for row in sorted(trades_v2, key=lambda trade: trade['seq']):
        owners = [record for record in submitted
                  if int(datetime.fromisoformat(record['entry_bar_utc']).timestamp()) == row['opened_at']]
        if len(owners) != 1:
            raise ValueError(f"event_window: result.v2 row seq {row['seq']} matches {len(owners)} submitted events")
        rows[id(owners[0])].append(row)

    def reason_of(row):
        if row['seq'] in liquidated:
            return 'liquidation'
        if by_fill.get(row['closed_at']):
            return by_fill[row['closed_at']].pop(0)
        if row['closed_at'] == insolvent_at:
            return 'insolvency'
        return 'engine_finalize_trades_settlement'

    for record in submitted:
        own = rows[id(record)]
        if not own:
            record.update(status='rejected_at_fill')
            continue
        exits = [dict(time=_utc(row['closed_at']), price=row['exit_price'], reason=reason_of(row))
                 for row in own]
        record.update(status='entered', entry_utc=_utc(own[0]['opened_at']),
                      entry_price=own[0]['entry_price'], exits=exits)
        last = exits[-1]
        record.update(exit_reason=last['reason'], exit_utc=last['time'], exit_price=last['price'])
    for record in events:
        if 'status' in record:
            continue
        if stop_bar is None:
            raise ValueError(f"event_window: event ts_utc={record.get('ts_utc')} has no status "
                             'although the run did not break early')
        record.update(status='skipped_run_ended', reason='insolvency_break')


def event_window_assumptions(config):
    return dict(bars_before=config.bars_before, bars_after=config.bars_after, direction=config.direction,
                event_bar='open_le_ts_lt_next_open', entry='open_of_bar_bars_before_earlier_than_event_bar',
                entry_fill='queued_at_previous_close_next_open_market',
                exit='close_of_bars_after_th_held_bar_entry_bar_counts_1_next_open_market_or_risk_exit_first',
                in_position='skip_next_event', out_of_range='skip', data_source='inline_params_only',
                note='事件根 = 开盘 ≤ ts < 下一根开盘；入场 = 事件根往前 bars_before 根的开盘价（前一根收盘下单）；'
                     '到期 = 持有第 bars_after 根收盘下单、下一根开盘成交；风控出场先到者优先。'
                     '一条 entered 事件 = 一次持仓生命周期，result.v2 可能有多行（分批止盈每次成交一行），'
                     '逐次出场见该事件的 exits；exit_reason / exit_price 取最后一次出场。'
                     '价格（entry_price、exits[].price、exit_price）为 result.v2 行上的十进制字符串。'
                     '事件 status：skipped_out_of_range / skipped_in_position / blocked_by_time_or_filter / '
                     'rejected_at_fill / entered / skipped_run_ended（资不抵债提前结束，该事件的决策根未到，'
                     'reason=insolvency_break）。'
                     'planned_exit_decision_bar_utc 为初始化时的计划值，到期平仓遇在途挂单会顺延一根，实际出场见 exits。')
