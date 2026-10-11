"""C39: causal inline unlock schedules, short before unlock and long afterwards."""
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from backtesting import Strategy
from strategy_event_window import _parse_ts, settle_event_window_exits
from strategy_time_layer import utc_datetime

TOOL_ID = 'local.backtesting_py.token_unlock_event'
ASSUMPTIONS = {
    'data_source': 'inline_params_only',
    'schedule_notice': '解锁排期按上市日起视为已公开，改期事件按我方首次抓到时间',
    'history_notice': '回补前上游已改过的历史排期无法还原，按最新排期计',
    'fill': 'previous_close_order_next_open; UTC_midnight; leverage_1',
    'overlap': 'skip_later_event_for_entire_planned_window',
    'known_at': 'known_at_utc <= short_entry_open; otherwise_skip_entire_event_without_delay',
}
SCHEMA = {
    'events': {'type': 'array', 'minItems': 1, 'maxItems': 50, 'items': {
        'type': 'object', 'additionalProperties': False,
        'required': ['ts_utc', 'label', 'pct_of_supply', 'known_at_utc'],
        'properties': {'ts_utc': {'type': 'string', 'format': 'date-time'},
                       'label': {'type': 'string', 'enum': ['token_unlock']},
                       'pct_of_supply': {'type': 'string'},
                       'known_at_utc': {'type': 'string', 'format': 'date-time'}}}},
    'pre_days': {'type': 'integer', 'default': 3, 'minimum': 1, 'maximum': 14},
    'post_days': {'type': 'integer', 'default': 3, 'minimum': 1, 'maximum': 14},
    'min_pct_of_supply': {'type': 'number', 'default': 1, 'minimum': 0.01, 'maximum': 100},
    'stop_loss_pct': {'type': 'number', 'minimum': 0.1, 'maximum': 20},
    'leverage': {'type': 'integer', 'default': 1, 'enum': [1]},
}


@dataclass(frozen=True)
class UnlockConfig:
    events: tuple
    pre_days: int
    post_days: int
    minimum: Decimal

    @classmethod
    def parse(cls, params):
        events = params.get('events')
        if type(events) is not list or not 1 <= len(events) <= 50:
            raise ValueError('INVALID_PARAMS:events must contain 1-50 items')
        parsed, seen = [], set()
        for item in events:
            if type(item) is not dict or set(item) != {'ts_utc', 'label', 'pct_of_supply', 'known_at_utc'}:
                raise ValueError('INVALID_PARAMS:unlock event keys are invalid')
            if item['label'] != 'token_unlock' or type(item['pct_of_supply']) is not str:
                raise ValueError('INVALID_PARAMS:unlock label or decimal string is invalid')
            ts, known = _parse_ts(item['ts_utc']), _parse_ts(item['known_at_utc'])
            if ts.hour or ts.minute or ts.second or ts.microsecond or ts in seen:
                raise ValueError('INVALID_PARAMS:unlock dates must be unique UTC midnights')
            try:
                pct = Decimal(item['pct_of_supply'])
            except InvalidOperation:
                raise ValueError('INVALID_PARAMS:pct_of_supply must be a decimal string')
            if not pct.is_finite() or not Decimal(0) <= pct <= Decimal(100):
                raise ValueError('INVALID_PARAMS:pct_of_supply must be within 0-100')
            seen.add(ts)
            parsed.append((ts, known, pct))
        return cls(tuple(sorted(parsed)), int(params.get('pre_days', 3)),
                   int(params.get('post_days', 3)), Decimal(str(params.get('min_pct_of_supply', 1))))


def make_strategy(mixin, config, risk, initial_capital):
    class TokenUnlockStrategy(mixin, Strategy):
        _risk = risk
        _initial_capital = initial_capital

        def init(self):
            self._risk_init()
            self.unlock_events, self._actions = [], {}
            self._decisions, self._next_bar = {}, -1
            opens = [utc_datetime(t) for t in self.data.index]
            bars = {t: i for i, t in enumerate(opens)}
            reserved_end = None
            for ts, known, pct in config.events:
                record = dict(ts_utc=ts.isoformat(), label='token_unlock', pct_of_supply=str(pct),
                              known_at_utc=known.isoformat(), legs=[])
                self.unlock_events.append(record)
                entry, exit_at = ts - timedelta(days=config.pre_days), ts + timedelta(days=config.post_days)
                if pct < config.minimum:
                    record['status'] = 'skipped_below_threshold'
                    continue
                if not known <= entry:
                    record['status'] = 'skipped_not_known'
                    continue
                if reserved_end is not None and entry < reserved_end:
                    record['status'] = 'skipped_overlap'
                    continue
                if any(t not in bars for t in (entry, ts, exit_at)) or bars.get(entry, -1) < 2:
                    record['status'] = 'skipped_out_of_range'
                    continue
                # A missing decision candle must never shift an order to another opening.
                step = getattr(self, '_risk_timeframe_ns', None) or int((opens[1] - opens[0]).total_seconds() * 10**9)
                if any(int((opens[bars[t]] - opens[bars[t] - 1]).total_seconds() * 10**9) != step
                       for t in (entry, ts, exit_at)):
                    record['status'] = 'skipped_data_gap'
                    continue
                reserved_end = exit_at
                record['status'] = 'scheduled'
                for side, start, end in (('short', entry, ts), ('long', ts, exit_at)):
                    leg = dict(ts_utc=ts.isoformat(), label='token_unlock', side=side,
                               entry_bar_utc=start.isoformat(), planned_exit_utc=end.isoformat())
                    record['legs'].append(leg)
                    self._actions.setdefault(bars[start] - 1, []).append(('entry', leg))
                    self._actions.setdefault(bars[end] - 1, []).append(('exit', leg))

        @property
        def event_window_log(self):
            opens = [int(utc_datetime(t).timestamp()) for t in self.data.index]
            stop = self._next_bar + 1
            return dict(decisions=dict(self._decisions), bar_opens=opens,
                        stop_bar=stop if stop < len(opens) else None)

        def next(self):
            bar = self._next_bar = len(self.data) - 1
            if self.position:
                self._risk_exit_reason = None
                if self._risk_check_exit():
                    self._decisions[bar] = self._risk_exit_reason or 'risk_exit'
            actions = self._actions.get(bar, ())
            # Close first, then queue the other direction: both settle at the same next open.
            for action, leg in actions:
                if action == 'exit' and self.position:
                    trade = self.trades[-1]
                    if not any(order.parent_trade is trade for order in self.orders):
                        self._decisions[bar] = 'unlock_day' if leg['side'] == 'short' else 'post_days'
                        self.__dict__['_exit_kind_pending'] = 'time_exit'
                        self.position.close()
            for action, leg in actions:
                if action != 'entry':
                    continue
                closing = self.position and any(o.parent_trade is self.trades[-1] for o in self.orders)
                if (self.position and not closing) or any(o.parent_trade is None for o in self.orders):
                    leg['status'] = 'skipped_in_position'
                    continue
                count = len(self.orders)
                self._risk_sell() if leg['side'] == 'short' else self._risk_buy()
                leg['status'] = 'submitted' if len(self.orders) > count else 'blocked_by_time_or_filter'
    return TokenUnlockStrategy


def settle_report(strategy, trades, isolated_risk):
    legs = [leg for event in strategy.unlock_events for leg in event['legs']]
    settle_event_window_exits(legs, trades, isolated_risk, **strategy.event_window_log)
    for event in strategy.unlock_events:
        if event['status'] == 'scheduled':
            event['status'] = 'entered' if any(l['status'] == 'entered' for l in event['legs']) else 'not_entered'
    return {'events': strategy.unlock_events,
            'skipped_overlap': sum(e['status'] == 'skipped_overlap' for e in strategy.unlock_events)}
