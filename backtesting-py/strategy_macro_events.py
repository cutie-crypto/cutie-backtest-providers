"""Q18: causal inline macro releases, sharing EVENT0 parsing and settlement.

Only complete pre-release candles feed history. No indicator/history prefix is fetched.
Release decisions at a candle boundary may queue a market order for that boundary's
open; off-grid releases wait for the next open. No post-release OHLC feeds H2/H3 signals.
"""
from bisect import bisect_left, bisect_right
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
import math

from backtesting import Strategy
from strategy_event_window import EventWindowConfig, MAX_EVENTS, _parse_ts, SCHEMA as EVENT_SCHEMA
from strategy_risk_overlay import RiskState
from strategy_time_layer import HoldingExpiry, utc_datetime

KINDS = ('macro_release_breakout', 'macro_surprise_direction', 'fomc_reversal')
# P2: funding_settlement_reversal reuses the surprise engine (actual - expected vs threshold, direction map,
# timed exit, percent stop). Its events come from settled funding rows, not inline params, and carry a 5th
# element (extra record fields). It is deliberately not in KINDS/SCHEMAS (those are the inline macro catalog).
FUNDING_KIND = 'funding_settlement_reversal'
SURPRISE_KINDS = (KINDS[1], FUNDING_KIND)


def integer(default, minimum=1):
    return dict(type='integer', default=default, minimum=minimum, maximum=1000000)


def number(default, minimum, maximum=1000000):
    return dict(type='number', default=default, minimum=minimum, maximum=maximum)


SCHEMAS = {
    KINDS[0]: dict(events=deepcopy(EVENT_SCHEMA['events']), pre_window_minutes=integer(30),
                   max_hold_minutes=integer(240), take_profit_r=number(2, 0.01, 100),
                   direction=dict(type='string', default='both', enum=['long', 'short', 'both'])),
    KINDS[1]: dict(events=deepcopy(EVENT_SCHEMA['events']), surprise_threshold=number(0.1, 0),
                   direction_map=dict(type='object', additionalProperties=False, required=['below', 'above'],
                                      properties={key: dict(type='string', enum=['long', 'short', 'none'])
                                                  for key in ('below', 'above')},
                                      default=dict(below='long', above='short')),
                   hold_minutes=integer(1440), stop_loss_pct=number(3, 0.01, 99)),
    KINDS[2]: dict(events=deepcopy(EVENT_SCHEMA['events']), lookback_hours=integer(72),
                   move_threshold_pct=number(5, 0.01), entry_delay_minutes=integer(30, 0),
                   hold_minutes=integer(2880), stop_loss_pct=number(3, 0.01, 99)),
}
_surprise_items = SCHEMAS[KINDS[1]]['events']['items']
_surprise_items['required'] += ['expected', 'actual']
_surprise_items['properties'].update({key: {'type': ['number', 'null']} for key in ('expected', 'actual')})
del _surprise_items


@dataclass(frozen=True)
class MacroConfig:
    kind: str
    events: tuple
    values: dict

    @classmethod
    def parse(cls, params, kind):
        events = params.get('events')
        if type(events) is not list or not 1 <= len(events) <= MAX_EVENTS:
            raise ValueError(f'INVALID_PARAMS:events must be a list of 1-{MAX_EVENTS} items')
        surprise = kind == KINDS[1]
        extras = {}
        if surprise:
            plain = []
            for item in events:
                if type(item) is not dict or set(item) != {'ts_utc', 'label', 'expected', 'actual'}:
                    raise ValueError('INVALID_PARAMS:events[] items must have exactly ts_utc, label, expected and actual')
                for key in ('expected', 'actual'):
                    value = item[key]
                    try:
                        valid = value is None or (type(value) in (int, float) and math.isfinite(value))
                    except OverflowError:
                        valid = False
                    if not valid:
                        raise ValueError(f'INVALID_PARAMS:events[].{key} must be a finite number or null')
                plain.append({key: item[key] for key in ('ts_utc', 'label')})
                # Validate before using the instant as a key: JSON arrays/objects must
                # return EVENT0's INVALID_PARAMS, never an unhashable-key engine error.
                extras[_parse_ts(item['ts_utc'])] = (item['expected'], item['actual'])
            parsed = EventWindowConfig.parse(dict(events=plain)).events
        else:
            parsed = EventWindowConfig.parse(dict(events=events)).events
        values = {}
        for key, spec in SCHEMAS[kind].items():
            if key == 'events':
                continue
            value = params.get(key, deepcopy(spec['default']))
            if spec['type'] == 'integer' and type(value) is not int:
                raise ValueError(f'INVALID_PARAMS:{key} must be an integer')
            if key == 'direction_map':
                if (type(value) is not dict or set(value) != {'below', 'above'}
                        or any(v not in ('long', 'short', 'none') for v in value.values())):
                    raise ValueError('INVALID_PARAMS:direction_map must have exactly below and above, each long, short or none')
            values[key] = value
        return cls(kind, tuple((ts, label, *extras.get(ts, ())) for ts, label in parsed), values)

    @property
    def hold_minutes(self):
        return self.values.get('max_hold_minutes', self.values.get('hold_minutes'))


def make_macro_strategy(mixin, config, risk, initial_capital):
    class MacroEventStrategy(mixin, Strategy):
        _risk = risk
        _initial_capital = initial_capital

        def init(self):
            self._risk_init()
            self._opens = [utc_datetime(t) for t in self.data.index]
            self._period = timedelta(seconds=(getattr(self, '_risk_timeframe_ns', None) or
                                     int((self.data.index[1] - self.data.index[0]).total_seconds() * 10**9)) / 10**9)
            self.macro_events = []
            self._by_event, self._by_entry = {}, {}
            self._watching = []
            self._decisions, self._next_bar = {}, -1
            self._entry_record = None
            for event in config.events:
                ts, label = event[:2]
                record = dict(ts_utc=ts.isoformat(), label=label)
                self.macro_events.append(record)
                event_bar = bisect_right(self._opens, ts) - 1
                if event_bar < 0:
                    self._skip(record, 'event_before_data_start', 'skipped_out_of_range')
                    continue
                if ts >= self._opens[event_bar] + self._period:
                    self._skip(record, 'event_after_data_end' if event_bar == len(self._opens)-1
                               else 'event_in_data_gap', 'skipped_out_of_range')
                    continue
                record['event_bar_utc'] = self._opens[event_bar].isoformat()
                activation = bisect_left(self._opens, ts) - 1
                if activation < 1:
                    self._skip(record, 'entry_before_first_decision', 'skipped_out_of_range')
                    continue
                if config.kind in SURPRISE_KINDS:
                    record.update(expected=event[2], actual=event[3])
                    if len(event) > 4:
                        record.update(event[4])
                else:
                    duration = timedelta(minutes=config.values['pre_window_minutes']) if config.kind == KINDS[0] else timedelta(hours=config.values['lookback_hours'])
                    start = ts - duration
                    left = bisect_left(self._opens, start)
                    indices = [i for i in range(left, event_bar + 1) if self._opens[i] + self._period <= ts]
                    if start < self._opens[0] or not indices:
                        self._skip(record, 'insufficient_pre_event_history')
                        continue
                    if (self._opens[indices[0]] >= start + self._period
                            or self._opens[indices[-1]] + self._period <= ts - self._period
                            or any(self._opens[b] - self._opens[a] != self._period for a, b in zip(indices, indices[1:]))):
                        self._skip(record, 'pre_event_history_gap')
                        continue
                    if config.kind == KINDS[0]:
                        record.update(range_high=float(max(self.data.High[i] for i in indices)),
                                      range_low=float(min(self.data.Low[i] for i in indices)))
                        if record['range_high'] <= record['range_low']:
                            self._skip(record, 'empty_pre_event_range')
                            continue
                    else:
                        first = Decimal(str(self.data.Open[indices[0]]))
                        if first <= 0:
                            self._skip(record, 'invalid_pre_event_price')
                            continue
                        last = Decimal(str(self.data.Close[indices[-1]]))
                        record['move_pct'] = str((last - first) / first * 100)
                self._by_event.setdefault(activation, []).append(record)
            # Guard all sizing modes against a next-open gap through the frozen H1 stop.
            original = self._broker._process_orders
            def guarded_orders():
                if config.kind == KINDS[0] and self._entry_record is not None:
                    for order in list(self.orders):
                        if order.parent_trade is not None:
                            continue
                        stop = self._stop(order.is_long, self._broker._adjusted_price(order.size, self.data.Open[-1]))
                        fill = self._broker._adjusted_price(order.size, self.data.Open[-1])
                        if (fill <= stop if order.is_long else fill >= stop):
                            order.cancel()
                            self._entry_record['reason'] = 'stop_wrong_side_of_fill'
                original()
            self._broker._process_orders = guarded_orders

        @staticmethod
        def _skip(record, reason, status='skipped'):
            record.update(status=status, reason=reason)

        @property
        def event_window_log(self):
            stop = self._next_bar + 1
            return dict(decisions=dict(self._decisions), bar_opens=[int(t.timestamp()) for t in self._opens],
                        stop_bar=stop if stop < len(self._opens) else None)

        def _stop(self, is_long, fill):
            if config.kind == KINDS[0]:
                return Decimal(str(self._entry_record['range_low' if is_long else 'range_high']))
            return Decimal(str(fill)) * (1 + Decimal(str(config.values['stop_loss_pct'])) / 100 * (-1 if is_long else 1))

        def _sizing_template_stop(self, order):
            return self._stop(order.is_long, self._broker._adjusted_price(order.size, self.data.Open[-1]))

        def _holding_expiry(self):
            trade = self.trades[-1]
            return HoldingExpiry(self._opens[len(self.data)-1] + self._period >=
                                 utc_datetime(trade.entry_time) + timedelta(minutes=config.hold_minutes), None)

        def _risk_layer_check_exit(self):
            trade = self.trades[-1]
            if self._risk_trade is not trade:
                self._risk_trade = trade
                entry = Decimal(str(trade.entry_price))
                stop = self._stop(trade.is_long, entry)
                distance = abs(entry - stop)
                take = (entry + distance * Decimal(str(config.values['take_profit_r'])) * (1 if trade.is_long else -1)
                        if config.kind == KINDS[0] else None)
                self._risk_state = RiskState(entry, stop, take, distance, 'long' if trade.is_long else 'short')
                self._entry_record.update(stop_price=str(stop), take_price=str(take) if take is not None else None)
            return super()._risk_layer_check_exit()

        def _submit(self, record, direction, bar):
            if self.position or self.orders:
                self._skip(record, 'previous_event_still_open', 'skipped_in_position')
                return
            entry = bar + 1
            if entry >= len(self._opens):
                self._skip(record, 'entry_past_data_end', 'skipped_out_of_range')
                return
            due = self._opens[entry] + timedelta(minutes=config.hold_minutes)
            # Match _holding_expiry even across gaps: a real candle must close at
            # or after the deadline, AND a subsequent open must exist for its exit.
            exit_fill = bisect_left(self._opens, due - self._period) + 1
            if exit_fill >= len(self._opens):
                self._skip(record, 'window_past_data_end', 'skipped_out_of_range')
                return
            self._entry_record = record
            count = len(self.orders)
            self._risk_buy() if direction == 'long' else self._risk_sell()
            if len(self.orders) == count:
                self._skip(record, 'entry_gate', 'blocked_by_time_or_filter')
            else:
                record.update(status='submitted', direction=direction, entry_bar_utc=self._opens[entry].isoformat(),
                              planned_exit_decision_bar_utc=self._opens[exit_fill-1].isoformat())

        def next(self):
            bar = self._next_bar = len(self.data) - 1
            # Judge overlap at release, before a due close is queued. A delayed entry is checked again.
            for record in self._by_event.get(bar, ()):
                if config.kind in SURPRISE_KINDS and (record['expected'] is None or record['actual'] is None):
                    self._skip(record, 'missing_expected_or_actual', 'skipped_null')
                    continue
                if self.position or self.orders:
                    self._skip(record, 'previous_event_still_open', 'skipped_in_position')
                    continue
                if config.kind == KINDS[0]:
                    self._watching.append(record)
                    continue
                if config.kind in SURPRISE_KINDS:
                    delta = Decimal(str(record['actual'])) - Decimal(str(record['expected']))
                    record['surprise'] = str(delta)
                    if delta == 0 or abs(delta) < Decimal(str(config.values['surprise_threshold'])):
                        self._skip(record, 'rate_below_threshold' if config.kind == FUNDING_KIND
                                   else 'surprise_below_threshold')
                        continue
                    direction = config.values['direction_map']['above' if delta > 0 else 'below']
                    delay = 0
                else:
                    move = Decimal(record['move_pct'])
                    if abs(move) < Decimal(str(config.values['move_threshold_pct'])):
                        self._skip(record, 'move_below_threshold')
                        continue
                    direction = 'short' if move > 0 else 'long'
                    delay = config.values['entry_delay_minutes']
                if direction == 'none':
                    self._skip(record, 'direction_map_none')
                    continue
                due = _parse_ts(record['ts_utc']) + timedelta(minutes=delay)
                decision = bisect_left(self._opens, due) - 1
                if decision >= len(self._opens)-1:
                    self._skip(record, 'entry_past_data_end', 'skipped_out_of_range')
                    continue
                self._by_entry.setdefault(decision, []).append((record, direction))
            if self.position:
                self._risk_exit_reason = None
                if self._risk_check_exit():
                    self._decisions[bar] = self._risk_exit_reason or 'risk_exit'
            for record, direction in self._by_entry.get(bar, ()):
                self._submit(record, direction, bar)
            for record in self._watching:
                if 'status' in record:
                    continue
                if self.position or self.orders:
                    self._skip(record, 'previous_event_still_open', 'skipped_in_position')
                    continue
                # A close at the release boundary still describes the pre-release candle.
                if self._opens[bar] + self._period <= _parse_ts(record['ts_utc']):
                    continue
                direction = config.values['direction']
                close = self.data.Close[-1]
                if direction in ('long', 'both') and close > record['range_high']:
                    self._submit(record, 'long', bar)
                elif direction in ('short', 'both') and close < record['range_low']:
                    self._submit(record, 'short', bar)
                elif bar == len(self._opens)-1:
                    self._skip(record, 'no_breakout_before_data_end')
    return MacroEventStrategy


def macro_assumptions(config):
    return dict(**config.values, data_source='inline_params_only', warmup_bars=0,
                event_bar='open_le_ts_lt_next_open', timezone='UTC_ts_utc_no_local_conversion',
                entry=('post_release_close_strictly_outside_pre_range_next_open' if config.kind == KINDS[0]
                       else 'first_open_at_or_after_release_plus_delay'),
                history='complete_candles_inside_pre_event_window_only_no_prefix_fetch',
                fill='next_open_market; stop/take detected by candle high/low, not guaranteed stop/take price',
                priority='stop_loss_then_holding_expiry_then_take_profit',
                hold='from_actual_entry_open; first_candle_close_at_or_after_deadline_then_next_open',
                in_position='skip_at_release_and_recheck_at_entry; pending_orders_also_block; one_trade_per_event',
                waiting_breakouts='oldest_event_first; other_waiting_events_skipped_when_position_or_order_exists',
                out_of_range='skip_before_first_data_open_or_at_or_after_last_open_plus_period; skip_data_gaps',
                incomplete_window='skip_missing_pre_history_or_entry_before_first_decision_or_hold_past_data_end',
                settlement='engine_insolvency_or_finalization_may_end_early; actual_exits_from_result_v2',
                **({'take_profit': 'actual_fill_plus_minus_take_profit_r_times_abs_actual_fill_minus_stop'}
                   if config.kind == KINDS[0] else
                   {'null_values': 'skip_missing_expected_or_actual_before_overlap_check; reported_in_raw_report',
                    'threshold': 'abs(actual_minus_expected)_gte_threshold; zero_difference_has_no_direction'}
                   if config.kind == KINDS[1] else
                   {'cumulative_move': '(last_pre_release_close / first_lookback_open - 1) * 100'}),
                precision='off_grid_release_or_delay_rounds_up_to_next_open; partial_history_candles_excluded; '
                          'stops_and_holding_deadlines_have_candle_resolution; boundary_release_known_at_that_open')


def macro_report(events):
    counts = dict(Counter(record['status'] for record in events))
    skipped = [dict(ts_utc=r['ts_utc'], reason=r.get('reason', r['status'])) for r in events
               if r['status'] != 'entered']
    return dict(events=events, counts=counts, skipped_count=len(skipped),
                skipped_null_count=counts.get('skipped_null', 0),
                skipped_in_position_count=counts.get('skipped_in_position', 0), skipped_events=skipped)
