"""Shared ORB/Asia engine; intrinsic range clocks are independent of optional gates."""
import bisect
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from backtesting import Strategy
from strategy_time_layer import TimeConfig, TimeContext, HoldingExpiry, expiry_due, utc_datetime, fixed_timeframe_milliseconds
from strategy_time_series import PeriodDefinition, SeriesBar, period_bounds, range_window, freeze_range, TimeHistoryError
from strategy_risk_overlay import RiskState
from strategy_position_sizing import SizingRejected


def minute(clock):
    PeriodDefinition(reset_at=clock)  # share strict HH:MM validation
    hour, value = map(int, clock.split(':'))
    return hour * 60 + value


@dataclass(frozen=True)
class RangeConfig:
    start: str
    end: str
    breakout: str
    flatten: str
    multiple: float
    direction: str
    definition: PeriodDefinition

    @classmethod
    def parse(cls, params, profile):
        start = params.get('range_start', '00:00')
        start_min = minute(start)
        if profile == 'orb':
            length = params.get('range_minutes', 60)
            if type(length) is not int or not 15 <= length <= 240:
                raise ValueError('INVALID_PARAMS:range_minutes must be an integer within 15-240')
            end_min = (start_min + length) % 1440
            end = f'{end_min//60:02d}:{end_min%60:02d}'
            breakout = end
        else:
            end = params.get('range_end', '07:00')
            length = (minute(end) - start_min) % 1440 or 1440
            breakout = params.get('breakout_start', '07:00')
        flatten = params.get('flatten_at', '23:45' if profile == 'orb' else '20:00')
        breakout_offset = (minute(breakout) - start_min) % 1440
        flatten_offset = (minute(flatten) - start_min) % 1440 or 1440
        if not length <= breakout_offset < flatten_offset:
            raise ValueError('INVALID_PARAMS:range end <= breakout start < flatten cutoff must fit one local cycle')
        multiple = params.get('take_profit_multiple', 2.0 if profile == 'orb' else 1.5)
        if type(multiple) not in (int, float) or not 0.01 <= multiple <= 100:
            raise ValueError('INVALID_PARAMS:take_profit_multiple must be finite within [0.01,100]')
        direction = params.get('direction', 'long')
        if direction not in ('long', 'short', 'both'):
            raise ValueError('INVALID_PARAMS:direction must be long, short or both')
        config = TimeConfig.parse(params)
        # Mandatory range prices cannot be silently replaced by generic price rules.
        for key in ('stop_loss_pct', 'take_profit_pct', 'atr_stop_multiplier', 'take_profit_r',
                    'trailing_stop_pct', 'breakeven_stop', 'tp1_r', 'tp2_r', 'tp3_r'):
            if params.get(key):
                raise ValueError(f'INVALID_PARAMS:{key} conflicts with frozen range exits')
        if config.flatten_at or config.flatten_weekdays != 127:
            raise ValueError('INVALID_PARAMS:use flatten_at for the intrinsic range cutoff')
        return cls(start, end, breakout, flatten, float(multiple), direction,
                   PeriodDefinition(config.timezone_name, reset_at=start))

    def window(self, cycle):
        return range_window(cycle, self.definition, self.start, self.end)

    def entry_window(self, cycle):
        return range_window(cycle, self.definition, self.breakout, self.flatten)

    def validate_grid(self, timeframe, start, end):
        step = timedelta(milliseconds=fixed_timeframe_milliseconds(timeframe))
        first = utc_datetime(start)
        cycle = period_bounds(first, self.definition)
        while cycle.start_utc < utc_datetime(end):
            window = self.window(cycle)
            if (window.start_utc-first) % step or (window.end_utc-first) % step:
                raise ValueError('INVALID_PARAMS:range endpoint cuts through a candle')
            entry_window = self.entry_window(cycle)  # reject nonexistent local endpoints
            if (entry_window.end_utc-first) % step:
                raise ValueError('INVALID_PARAMS:flatten endpoint cuts through a candle')
            cycle = period_bounds(cycle.end_utc, self.definition)


def make_strategy(mixin, config, risk, initial_capital):
    class RangeBreakoutStrategy(mixin, Strategy):
        _risk = risk
        _initial_capital = initial_capital
        _range_config = config
        _range_prefix = ()
        _range_timeframe = None
        _range_backtest_start = None

        def init(self):
            timeframe = self._range_timeframe
            if timeframe is None:
                milliseconds = int((self.data.index[1] - self.data.index[0]).total_seconds()*1000)
                if milliseconds <= 0 or milliseconds % 60000:
                    raise ValueError('INVALID_PARAMS:range requires fixed minute candles')
                timeframe = f'{milliseconds//60000}m'
            intrinsic = TimeConfig(enabled=True, timezone_name=config.definition.timezone_name,
                                   flatten_at=config.flatten)
            self._series = list(self._range_prefix) + [SeriesBar(t, float(h), float(l), float(c), float(v))
                for t, h, l, c, v in zip(self.data.index, self.data.High, self.data.Low, self.data.Close, self.data.Volume)]
            self._series_opens = [utc_datetime(bar.open_utc) for bar in self._series]
            self._range_clock = TimeContext.build(intrinsic, timeframe, [b.open_utc for b in self._series])
            self._backtest_start = self._range_backtest_start or utc_datetime(self.data.index[0])
            config.validate_grid(timeframe, self._backtest_start, self._range_clock.decision_utc(self.data.index[-1]))
            self._snapshots = {}
            self.daily_ranges = {}
            self._used_days = set()
            self._entry_range = None
            self._entry_day = None
            self._risk_init()
            self._risk_trade = self._risk_state = self._risk_exit_reason = None

        def _record_holding_expiry(self, fact):
            self._risk_exit_reason = 'time_expiry'
            if fact.flatten_delay_bars is not None:
                self._range_clock.flatten_delays.append(fact.flatten_delay_bars)

        def _holding_expiry(self):
            generic = super()._holding_expiry()
            trade = self.trades[-1]
            intrinsic = expiry_due(holding_bars=0, entry_bar=trade.entry_bar, bar=len(self.data)-1,
                entry_utc=trade.entry_time, bar_open=self.data.index[-1], context=self._range_clock)
            return intrinsic if intrinsic.due else generic

        def _risk_check_exit(self):
            return self._risk_layer_check_exit() if self.position and self.trades else False

        def _sizing_template_stop(self, order):
            # 10-B2d: only consulted by position_size_risk_pct. The stop is the opposite side of the
            # range frozen before the signal (the same value _risk_layer_check_exit installs after the
            # fill); distance = |actual fill - stop|. This engine has no gap guard, so a fill on the
            # wrong side of the frozen stop cannot carry a risk distance: reject (fail-closed).
            frozen = self._entry_range
            stop = Decimal(str(frozen.low if order.is_long else frozen.high))
            fill = Decimal(str(self._broker._adjusted_price(order.size, self.data.Open[-1])))
            if fill <= stop if order.is_long else fill >= stop:
                raise SizingRejected('stop_wrong_side_of_fill')
            return stop

        def _risk_layer_check_exit(self):
            trade = self.trades[-1]
            if self._risk_trade is not trade:
                self._risk_trade = trade
                frozen = self._entry_range
                entry = Decimal(str(trade.entry_price))
                height = Decimal(str(frozen.high)) - Decimal(str(frozen.low))
                side = 'long' if trade.is_long else 'short'
                stop = Decimal(str(frozen.low if trade.is_long else frozen.high))
                take = entry + height*Decimal(str(config.multiple))*(1 if trade.is_long else -1)
                if stop <= 0 or take <= 0:
                    raise ValueError('INVALID_PARAMS:nonpositive frozen range exit price')
                self._risk_state = RiskState(entry, stop, take, abs(entry-stop), side)
                self.daily_ranges[self._entry_day]['triggered'] = True
                self.daily_ranges[self._entry_day]['entry_price'] = trade.entry_price
                self.daily_ranges[self._entry_day]['stop_price'] = float(stop)
                self.daily_ranges[self._entry_day]['take_price'] = float(take)
            closed = super()._risk_layer_check_exit()
            if closed and self._risk_exit_reason:
                self.daily_ranges[self._entry_day]['exit_reason'] = self._risk_exit_reason
            return closed

        def next(self):
            opened = self.data.index[-1]
            decision = self._range_clock.decision_utc(opened)
            cycle = period_bounds(opened, config.definition)
            key = cycle.start_utc.isoformat()
            window = config.window(cycle)
            if key not in self.daily_ranges:
                self.daily_ranges[key] = dict(date=cycle.start_utc.astimezone(self._range_clock.zone).date().isoformat(),
                    high=None, low=None, freeze_utc=window.end_utc.isoformat(), available=False,
                    triggered=False, exit_reason=None)
            if key not in self._snapshots and decision >= window.end_utc:
                left = bisect.bisect_left(self._series_opens, window.start_utc)
                right = bisect.bisect_left(self._series_opens, window.end_utc)
                snapshot = freeze_range(self._series[left:right], self._range_clock, cycle, config.definition, config.start, config.end,
                    as_of=decision, backtest_start=self._backtest_start)
                self._snapshots[key] = snapshot
                self.daily_ranges[key].update(high=snapshot.high, low=snapshot.low, available=snapshot.available)
            # Exits always run first; an existing close order blocks re-entry.
            if self.position:
                self._risk_check_exit()
                return
            if self.orders or key in self._used_days or key not in self._snapshots:
                return
            if utc_datetime(opened) >= self._range_clock.last_open_utc:
                return
            entry_window = config.entry_window(cycle)
            if not entry_window.start_utc <= decision < entry_window.end_utc:
                return
            frozen = self._snapshots[key]
            if not frozen.available or frozen.high <= frozen.low or not self._time_allow_entry():
                return
            close = self.data.Close[-1]
            if config.direction in ('long', 'both') and close > frozen.high:
                if not self._filter_allow_entry():
                    # Judgment bar = the first closed breakout; a filtered breakout consumes the cycle, not delayed.
                    self._used_days.add(key)
                    return
                self._risk_buy()
            elif config.direction in ('short', 'both') and close < frozen.low:
                self._risk_sell()
            else:
                return
            if self.orders:
                self._used_days.add(key)
                self._entry_range, self._entry_day = frozen, key
    return RangeBreakoutStrategy


def range_assumptions(config):
    return {'range_breakout': dict(timezone=config.definition.timezone_name,
        range_start=config.start, range_end=config.end, breakout_start=config.breakout, flatten_at=config.flatten,
        take_profit_multiple=config.multiple, direction=config.direction, max_trades_per_cycle=1,
        trigger='closed_bar_high_low', fill='next_bar_open_market',
        note='止损和止盈触发后下一根开盘市价平仓，不保证按价位成交；截止越界首个收盘决策提交强平。',
        stop='opposite_frozen_range_side', take='actual_entry_fill_plus_minus_frozen_range_height',
        priority='stop_loss_before_time_expiry_before_take_profit', warmup='strict_prefix_calculation_only')}
