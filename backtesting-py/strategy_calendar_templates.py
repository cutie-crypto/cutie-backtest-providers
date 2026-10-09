"""Causal regular-calendar templates; intrinsic exits share the risk arbiter."""
from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pandas as pd

from strategy_time_calendar import RegularCalendar, REGULAR_CALENDAR_ASSUMPTION
from strategy_time_layer import (TimeConfig, TimeContext, HoldingExpiry, expiry_due,
                                 fixed_timeframe_milliseconds, utc_datetime)


def validate_grid(timeframe, window_minutes=None):
    step = fixed_timeframe_milliseconds(timeframe)
    clocks = (810, 870, 960) if window_minutes is not None else (1260, 1320, 0)
    if step > 3600000 or any(clock * 60000 % step for clock in clocks):
        raise ValueError('INVALID_PARAMS:calendar boundaries must lie on the UTC candle grid (<=1h)')
    if window_minutes is not None and window_minutes * 60000 % step:
        raise ValueError('INVALID_PARAMS:window_minutes must be an integer multiple of timeframe')


def build_us_open(p, params, initial_capital):
    window = params.get('window_minutes', 30)
    threshold = params.get('threshold_pct', .3)
    direction = params.get('direction', 'both')
    schema = p.TOOL_SPECS['local.backtesting_py.us_open_momentum']['param_schema_properties']
    error = p._validate_params_against_schema(params, schema)
    if error:
        raise ValueError('INVALID_PARAMS:' + error)
    risk = p._parse_fixed_risk_params(params)
    from backtesting import Strategy

    class UsOpenMomentum(p._FixedRiskMixin, Strategy):
        _risk = risk
        _initial_capital = initial_capital

        def init(self):
            # Mandatory template exits use High/Low even with the optional layer off.
            self._risk = {**risk, 'risk_layer_enabled': True}
            self._risk_init()
            self._calendar = RegularCalendar('us_equity_regular')
            step_ns = getattr(self, '_risk_timeframe_ns', None) or int(
                (self.data.index[1] - self.data.index[0]).total_seconds() * 1e9)
            self._period = timedelta(microseconds=step_ns // 1000)
            self._cutoff_context = TimeContext(TimeConfig(enabled=True,
                timezone_name='America/New_York', flatten_at='16:00', flatten_weekdays=31),
                self._calendar._zone, self._period, utc_datetime(self.data.index[-1]))
            self._attempted = set()
            self._window_stop = None
            self._template_report = {'skipped': [], 'entries': [], 'exits': []}
            process = self._broker._process_orders
            def guarded_fill():
                # Called at the actual fill open, before the broker executes entry.
                for order in list(self.orders):
                    if order.parent_trade is None and self._window_stop is not None:
                        opening = self.data.Open[-1]
                        if (opening <= self._window_stop if order.is_long else opening >= self._window_stop):
                            order.cancel()
                            self._template_report['skipped'].append({'reason': 'stop_wrong_side_of_fill',
                                'at': int(pd.Timestamp(self.data.index[-1]).timestamp())})
                process()
            self._broker._process_orders = guarded_fill

        def _holding_expiry(self):
            user = super()._holding_expiry()
            t = self.trades[-1]
            own = expiry_due(holding_bars=0, entry_bar=t.entry_bar, bar=len(self.data)-1,
                entry_utc=t.entry_time, bar_open=self.data.index[-1], context=self._cutoff_context)
            return HoldingExpiry(user.due or own.due, user.flatten_delay_bars)

        def next(self):
            now = utc_datetime(self.data.index[-1])
            decision = now + self._period
            if self.position:
                stop = Decimal(str(self._window_stop))
                if self._risk.get('leverage', 1) > 1 and self._risk_isolated_exit(stop):
                    return
                hit = self.data.Low[-1] <= self._window_stop if self.position.is_long else self.data.High[-1] >= self._window_stop
                if hit:
                    self._risk_exit_reason = 'window_stop'
                    self.position.close()
                elif self._risk_check_exit():
                    pass
                else:
                    return
                self._template_report['exits'].append({'at': int(decision.timestamp()),
                    'reason': self._risk_exit_reason})
                return
            day = self._calendar.trading_day(now)
            if day is None or day in self._attempted:
                return
            bounds = self._calendar._session_for_day(day)
            end = bounds.start_utc + timedelta(minutes=window)
            if decision != end:
                return
            self._attempted.add(day)
            indices = [i for i, t in enumerate(self.data.index) if bounds.start_utc <= utc_datetime(t) < end]
            if (len(indices) != timedelta(minutes=window) / self._period or
                    any(utc_datetime(self.data.index[i]) != bounds.start_utc + j*self._period
                        for j, i in enumerate(indices))):
                self._template_report['skipped'].append({'day': str(day), 'reason': 'window_bar_missing'})
                return
            move = Decimal(str(self.data.Close[indices[-1]])) / Decimal(str(self.data.Open[indices[0]])) - 1
            cutoff = Decimal(str(threshold)) / 100
            side = 'long' if move >= cutoff else 'short' if move <= -cutoff else None
            if side is None or direction not in ('both', side) or now >= self._cutoff_context.last_open_utc:
                return
            self._window_stop = min(self.data.Low[indices]) if side == 'long' else max(self.data.High[indices])
            (self._risk_buy if side == 'long' else self._risk_sell)()
            if self.orders:
                self._template_report['entries'].append({'day': str(day), 'decision_at': int(decision.timestamp()),
                    'window_start': int(bounds.start_utc.timestamp()), 'stop': float(self._window_stop)})

    return {'strategy': UsOpenMomentum, 'executed_name': f'US Open Momentum ({window}m/{threshold:g}%)',
        'min_bars': 2, 'validate_timeframe': lambda tf: validate_grid(tf, window),
        'template_assumptions': {'calendar_template': {'calendar': 'us_equity_regular',
            'regular_calendar': REGULAR_CALENDAR_ASSUMPTION, 'window_minutes': window,
            'decision': 'window_last_close', 'fill': 'next_bar_open', 'stop': 'frozen_window_opposite_extreme',
            'flatten': 'New_York_16:00', 'same_bar_priority': 'liquidation_stop_expiry_profit_signal'}},
        'template_report_key': 'us_open_momentum'}


def build_cme_gap(p, params, initial_capital):
    from strategy_risk_overlay import RiskState, decide_exit
    gap = params.get('gap_pct', 1)
    direction = params.get('direction', 'both')
    schema = p.TOOL_SPECS['local.backtesting_py.cme_weekend_gap']['param_schema_properties']
    error = p._validate_params_against_schema(params, schema)
    if error:
        raise ValueError('INVALID_PARAMS:' + error)
    risk = p._parse_fixed_risk_params(params)
    # An explicitly chosen stop mechanism overrides the intrinsic 2% stop.
    if not any(risk.get(k) for k in ('stop_loss_pct', 'atr_stop_multiplier', 'trailing_stop_pct')):
        risk['stop_loss_pct'] = .02
    from backtesting import Strategy

    class CmeWeekendGap(p._FixedRiskMixin, Strategy):
        _risk = risk
        _initial_capital = initial_capital

        def init(self):
            self._risk = {**risk, 'risk_layer_enabled': True}
            self._risk_init()
            self._calendar = RegularCalendar('cme_btc_regular')
            step_ns = getattr(self, '_risk_timeframe_ns', None) or int(
                (self.data.index[1] - self.data.index[0]).total_seconds() * 1e9)
            self._period = timedelta(microseconds=step_ns // 1000)
            self._cutoff_context = TimeContext(TimeConfig(enabled=True, flatten_at='00:00',
                flatten_weekdays=4), ZoneInfo('UTC'),
                self._period, utc_datetime(self.data.index[-1]))
            self._closed_prices = {}
            self._attempted_weeks = set()
            self._target = None
            self._template_report = {'skipped': [], 'entries': [], 'exits': []}
            self._entry_due = None
            process = self._broker._process_orders
            def guarded_fill():
                for order in list(self.orders):
                    if order.parent_trade is None and self._entry_due is not None and utc_datetime(self.data.index[-1]) != self._entry_due:
                        order.cancel()
                        self._template_report['skipped'].append({'reason': 'entry_fill_bar_missing',
                            'at': int(pd.Timestamp(self.data.index[-1]).timestamp())})
                process()
            self._broker._process_orders = guarded_fill

        def _holding_expiry(self):
            user = super()._holding_expiry()
            trade = self.trades[-1]
            own = expiry_due(holding_bars=0, entry_bar=trade.entry_bar, bar=len(self.data)-1,
                entry_utc=trade.entry_time, bar_open=self.data.index[-1], context=self._cutoff_context)
            return HoldingExpiry(user.due or own.due, user.flatten_delay_bars)

        def next(self):
            now = utc_datetime(self.data.index[-1])
            decision = now + self._period
            self._closed_prices[decision] = Decimal(str(self.data.Close[-1]))
            # Keep a bounded causal history sufficient for a complete weekend.
            self._closed_prices = {t: v for t, v in self._closed_prices.items() if t >= decision-timedelta(days=8)}
            if self.position:
                if self._risk_check_exit():
                    pass
                else:
                    state = RiskState(Decimal(str(self.trades[-1].entry_price)), None, self._target, None,
                        'long' if self.position.is_long else 'short')
                    if decide_exit(state, high=self.data.High[-1], low=self.data.Low[-1]) is None:
                        return
                    self._risk_exit_reason = 'gap_filled'
                    self.position.close()
                self._template_report['exits'].append({'at': int(decision.timestamp()), 'reason': self._risk_exit_reason})
                return
            local = decision.astimezone(self._calendar._zone)
            sunday = local.date() - timedelta(days=(local.weekday()+1)%7)
            monday = sunday + timedelta(days=1)
            sunday_open = self._calendar._session_for_day(monday).start_utc
            if decision < sunday_open or sunday in self._attempted_weeks:
                return
            self._attempted_weeks.add(sunday)
            friday_close = self._calendar._session_for_day(sunday-timedelta(days=2)).end_utc
            friday_price = self._closed_prices.get(friday_close)
            sunday_price = self._closed_prices.get(sunday_open)
            reason = ('friday_bar_missing' if friday_price is None else
                      'sunday_bar_missing' if sunday_price is None else
                      'sunday_decision_missed' if decision != sunday_open else None)
            if reason:
                self._template_report['skipped'].append({'week': str(sunday), 'reason': reason})
                return
            move = sunday_price / friday_price - 1
            threshold = Decimal(str(gap)) / 100
            side = 'short' if move >= threshold else 'long' if move <= -threshold else None
            if side is None or direction not in ('both', side) or now >= self._cutoff_context.last_open_utc:
                return
            self._target = friday_price
            self._entry_due = decision
            (self._risk_buy if side == 'long' else self._risk_sell)()
            if self.orders:
                self._template_report['entries'].append({'week': str(sunday), 'decision_at': int(decision.timestamp()),
                    'friday_close_at': int(friday_close.timestamp()), 'friday_price': str(friday_price),
                    'sunday_price': str(sunday_price), 'target': str(self._target)})

    return {'strategy': CmeWeekendGap, 'executed_name': f'CME Weekend Gap ({gap:g}%)', 'min_bars': 2,
        'validate_timeframe': validate_grid,
        'template_assumptions': {'calendar_template': {'calendar': 'cme_btc_regular',
            'regular_calendar': REGULAR_CALENDAR_ASSUMPTION, 'proxy': 'BTC_spot_OHLCV',
            'friday_price': 'Chicago_Friday_16:00_ending_bar_close',
            'sunday_price': 'Chicago_Sunday_17:00_ending_bar_close',
            'fill': 'next_bar_open', 'target': 'frozen_Friday_close_High_Low_touch',
            'flatten': 'Wednesday_00:00_UTC', 'same_bar_priority': 'liquidation_stop_expiry_profit_signal'}},
        'template_report_key': 'cme_weekend_gap'}
