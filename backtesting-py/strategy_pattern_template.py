"""Long-only pattern orders. Only these strategies guard the next-open fill.

The broker guard reads the currently arriving Open before calling the engine's
order processor. It does not inspect future bars or change engine fill pricing.
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd
from backtesting import Strategy

from strategy_candle_patterns import candle_geometry, candle_patterns


@dataclass(frozen=True)
class PatternEntry:
    signal_bar: int
    stop: float


def make_pattern_strategy(mixin, *, kind, position_filter, reward_r, risk, initial_capital):
    class LongPatternStrategy(mixin, Strategy):
        _risk = risk
        _initial_capital = initial_capital

        def init(self):
            self._risk_init()
            self.pattern_report = {"kind": kind, "skipped_entry_count": 0, "skipped_entries": [],
                                   "position_filter": position_filter, "reward_r": reward_r}
            arrays = []
            for column in ("Open", "High", "Low", "Close"):
                prefix = self._warmup_cols[column] if self._warmup_bars else []
                arrays.append(np.concatenate([prefix, np.asarray(getattr(self.data, column))]))
            geometry = candle_geometry(*arrays)
            close = pd.Series(arrays[3])
            if kind == "engulfing":
                # Population standard deviation, including the confirmation close.
                mean = close.rolling(20).mean()
                indicators = {"bb_lower": (mean - 2 * close.rolling(20).std(ddof=0)).to_numpy()}
            else:
                indicators = {f"ema{period}": close.ewm(span=period, adjust=False, min_periods=period).mean().to_numpy()
                              for period in (20, 60)}
            self._patterns = candle_patterns(kind, geometry, indicators=indicators)
            self._geometry = geometry
            self._main_bars = len(self.data)
            process_orders = self._broker._process_orders

            def guarded_process_orders():
                for order in list(self.orders):
                    if order.parent_trade is None and isinstance(order.tag, PatternEntry):
                        opening = float(self.data.Open[-1])
                        if opening <= order.tag.stop:
                            self.pattern_report["skipped_entry_count"] += 1
                            self.pattern_report["skipped_entries"].append({
                                "reason": "entry_open_at_or_below_frozen_stop",
                                "signal_bar": order.tag.signal_bar, "entry_bar": len(self.data) - 1,
                                "entry_open": opening, "frozen_stop": order.tag.stop})
                            order.cancel()
                process_orders()

            # Per-backtest instance only; other templates retain their exact broker.
            self._broker._process_orders = guarded_process_orders

        def next(self):
            if self.position:
                trade = self.trades[-1]
                if any(order.parent_trade is trade for order in self.orders):
                    return
                stop = trade.tag.stop
                target = trade.entry_price + (trade.entry_price - stop) * reward_r
                # A trigger queues a market close; the engine fills at the next Open.
                if self.data.Low[-1] <= stop:
                    self._risk_exit_reason = "stop_loss"
                elif (self._time_config is not None or self._risk.get("max_holding_bars")) and (fact := self._holding_expiry()).due:
                    self._record_holding_expiry(fact)
                elif self.data.High[-1] >= target:
                    self._risk_exit_reason = "take_profit"
                else:
                    return
                self.position.close()
                return
            if self.orders or len(self.data) >= self._main_bars or not self._time_allow_entry():
                return
            index = self._warmup_bars + len(self.data) - 1
            if position_filter:
                if index < 20 or not self._patterns.bullish[index]:
                    return
            elif not self._patterns.bullish_shape[index]:
                return
            anchor = (min(self._geometry.low[index - 1:index + 1]) if kind == "engulfing"
                      else self._geometry.low[index])
            tag = PatternEntry(signal_bar=len(self.data) - 1, stop=anchor * 0.999)
            size = self._risk_entry_size()
            self.buy(tag=tag) if size is None else self.buy(size=size, tag=tag)

    return LongPatternStrategy
