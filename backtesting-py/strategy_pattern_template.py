"""Candle pattern orders: long templates and their futures short mirrors (SHORT-PAT-4 / SHORT-PAT-5).
Only these strategies guard the next-open fill.

The broker guard reads the currently arriving Open before calling the engine's
order processor. It does not inspect future bars or change engine fill pricing.
"""
from dataclasses import dataclass
from decimal import Decimal

import numpy as np
import pandas as pd
from backtesting import Strategy

from strategy_candle_patterns import candle_geometry, candle_patterns


@dataclass(frozen=True)
class PatternEntry:
    signal_bar: int
    stop: float


def make_pattern_strategy(mixin, *, kind, position_filter, reward_r, risk, initial_capital,
                          breakout_window=3, trend_filter=False, rsi_series=None):
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
            elif kind == "pin_bar":
                indicators = {f"ema{period}": close.ewm(span=period, adjust=False, min_periods=period).mean().to_numpy()
                              for period in (20, 60)}
            elif kind == "doji":
                indicators = {"rsi": rsi_series(arrays[3], 14)}
            else:
                indicators = {}
            self._patterns = candle_patterns(kind, geometry, indicators=indicators)
            candidates = self._patterns.bullish if position_filter else self._patterns.bullish_shape
            # Compute only causal confirmation facts over warmup + main bars.
            # Candidate consumption is independent of positions and session gates.
            self._signals = list(candidates)
            self._anchors = list(geometry.low)
            if kind == "star":
                self._anchors = [geometry.low[max(0, i - 1)] for i in range(len(candidates))]
            elif kind == "soldiers":
                self._anchors = [geometry.low[max(0, i - 2)] for i in range(len(candidates))]
            elif kind == "doji":
                self._signals = [i > 0 and candidates[i - 1] and geometry.close[i] > geometry.high[i - 1]
                                 for i in range(len(candidates))]
                self._anchors = [geometry.low[max(0, i - 1)] for i in range(len(candidates))]
            elif kind == "inside_bar":
                self.pattern_report.update(breakout_window=breakout_window, trend_filter=trend_filter)
                ema = close.ewm(span=20, adjust=False, min_periods=20).mean().to_numpy()
                self._signals = [False] * len(candidates)
                armed = None
                for i in range(len(candidates)):
                    if armed is not None:
                        inside, mother = armed
                        if i - inside > breakout_window:
                            armed = None
                        elif geometry.close[i] > geometry.high[mother]:
                            self._signals[i] = not trend_filter or geometry.close[i] > ema[i]
                            self._anchors[i] = geometry.low[mother]
                            armed = None  # one breakout consumes the candidate, even if filtered
                    if candidates[i]:
                        # A breakout candle can itself be inside a newer mother.
                        # Always arm it, regardless of breakout/trend-filter outcome.
                        armed = (i, i - 1)  # latest inside bar replaces an unbroken candidate
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

        def _risk_check_exit(self):
            # Also used by the shared broker insolvency bridge. Supply the frozen
            # pattern stop to T2-2b arbitration before checking expiry/target.
            if not self.position or not self.trades:
                return False
            trade = self.trades[-1]
            if any(order.parent_trade is trade for order in self.orders):
                return True
            stop = trade.tag.stop
            if self._risk.get("leverage", 1) > 1 and self._risk_isolated_exit(Decimal(str(stop))):
                return True
            target = trade.entry_price + (trade.entry_price - stop) * reward_r
            # A trigger queues a market close; the engine fills at the next Open.
            if self.data.Low[-1] <= stop:
                self._risk_exit_reason = "stop_loss"
            elif (self._time_config is not None or self._risk.get("max_holding_bars")) and (fact := self._holding_expiry()).due:
                self._record_holding_expiry(fact)
            elif self.data.High[-1] >= target:
                self._risk_exit_reason = "take_profit"
            else:
                return False
            self.position.close()
            return True

        def next(self):
            if self.position:
                self._risk_check_exit()
                return
            queue = self._pattern_confirm_queue
            # P-PATCONF-2b1: with confirmation on, the signal bar s only registers; the entry gates
            # are judged at the confirming bar k (_pattern_confirm_open_tagged).
            if queue is None and (self.orders or len(self.data) >= self._main_bars
                                  or not self._time_allow_entry() or not self._filter_allow_entry()):
                return
            index = self._warmup_bars + len(self.data) - 1
            if position_filter:
                if index < 20 or not self._signals[index]:
                    return
            elif not self._signals[index]:
                return
            anchor = (min(self._geometry.low[index - 1:index + 1]) if kind == "engulfing"
                      else self._anchors[index])
            tag = PatternEntry(signal_bar=len(self.data) - 1, stop=anchor * 0.999)
            if queue is not None:
                # The stop is frozen here at s and travels with the signal.
                queue.register(len(self.data) - 1, "long", self.data.High[-1], tag)
                return
            size = self._risk_entry_size()
            order = self.buy(tag=tag) if size is None else self.buy(size=size, tag=tag)
            if self._risk.get("position_sizing_enabled"):
                order._sizing_signal_bar = len(self.data) - 1

        def _pattern_confirm_open(self, is_long, payload):
            return self._pattern_confirm_open_tagged(is_long, payload)

    return LongPatternStrategy


def make_short_pattern_strategy(mixin, *, kind, position_filter, reward_r, risk, initial_capital, rsi_series=None):
    """SHORT-PAT-4: bearish engulfing / shooting star / evening star; SHORT-PAT-5: three black crows /
    bearish doji reversal. The mirror of the long template.

    Stop = pattern anchor High * 1.001 frozen at the signal; target = entry - (stop - entry) * reward_r;
    an entry open at or above the frozen stop is skipped; the stop precedes holding expiry, which
    precedes the target; isolated liquidation arbitrates against the frozen stop (T2-2b).
    """
    class ShortPatternStrategy(mixin, Strategy):
        _risk = risk
        _initial_capital = initial_capital

        def init(self):
            self._risk_init()
            self.pattern_report = {"kind": kind, "direction": "short", "skipped_entry_count": 0,
                                   "skipped_entries": [], "position_filter": position_filter, "reward_r": reward_r}
            arrays = []
            for column in ("Open", "High", "Low", "Close"):
                prefix = self._warmup_cols[column] if self._warmup_bars else []
                arrays.append(np.concatenate([prefix, np.asarray(getattr(self.data, column))]))
            geometry = candle_geometry(*arrays)
            close = pd.Series(arrays[3])
            if kind == "engulfing":
                # Population standard deviation, including the confirmation close.
                mean = close.rolling(20).mean()
                indicators = {"bb_upper": (mean + 2 * close.rolling(20).std(ddof=0)).to_numpy()}
            elif kind == "pin_bar":
                indicators = {f"ema{period}": close.ewm(span=period, adjust=False, min_periods=period).mean().to_numpy()
                              for period in (20, 60)}
            elif kind == "doji":
                indicators = {"rsi": rsi_series(arrays[3], 14)}
            else:
                indicators = {}
            patterns = candle_patterns(kind, geometry, indicators=indicators)
            self._signals = list(patterns.bearish if position_filter else patterns.bearish_shape)
            n = len(self._signals)
            if kind == "engulfing":
                self._anchors = [max(geometry.high[max(0, i - 1):i + 1]) for i in range(n)]
            elif kind == "star":
                self._anchors = [geometry.high[max(0, i - 1)] for i in range(n)]
            elif kind == "soldiers":
                self._anchors = [geometry.high[max(0, i - 2)] for i in range(n)]
            elif kind == "doji":
                # Only the immediately following close below the doji low confirms; stop uses the doji high.
                candidates = self._signals
                self._signals = [i > 0 and candidates[i - 1] and geometry.close[i] < geometry.low[i - 1]
                                 for i in range(n)]
                self._anchors = [geometry.high[max(0, i - 1)] for i in range(n)]
            else:
                self._anchors = list(geometry.high)
            self._main_bars = len(self.data)
            process_orders = self._broker._process_orders

            def guarded_process_orders():
                for order in list(self.orders):
                    if order.parent_trade is None and isinstance(order.tag, PatternEntry):
                        opening = float(self.data.Open[-1])
                        if opening >= order.tag.stop:
                            self.pattern_report["skipped_entry_count"] += 1
                            self.pattern_report["skipped_entries"].append({
                                "reason": "entry_open_at_or_above_frozen_stop",
                                "signal_bar": order.tag.signal_bar, "entry_bar": len(self.data) - 1,
                                "entry_open": opening, "frozen_stop": order.tag.stop})
                            order.cancel()
                process_orders()

            self._broker._process_orders = guarded_process_orders

        def _risk_check_exit(self):
            # Also used by the shared broker insolvency bridge. Supply the frozen
            # pattern stop to T2-2b arbitration before checking expiry/target.
            if not self.position or not self.trades:
                return False
            trade = self.trades[-1]
            if any(order.parent_trade is trade for order in self.orders):
                return True
            stop = trade.tag.stop
            if self._risk.get("leverage", 1) > 1 and self._risk_isolated_exit(Decimal(str(stop))):
                return True
            target = trade.entry_price - (stop - trade.entry_price) * reward_r
            # A trigger queues a market close; the engine fills at the next Open.
            if self.data.High[-1] >= stop:
                self._risk_exit_reason = "stop_loss"
            elif (self._time_config is not None or self._risk.get("max_holding_bars")) and (fact := self._holding_expiry()).due:
                self._record_holding_expiry(fact)
            elif self.data.Low[-1] <= target:
                self._risk_exit_reason = "take_profit"
            else:
                return False
            self.position.close()
            return True

        def next(self):
            if self.position:
                self._risk_check_exit()
                return
            queue = self._pattern_confirm_queue
            # P-PATCONF-2b1: with confirmation on, the signal bar s only registers; the entry gates
            # are judged at the confirming bar k (_pattern_confirm_open_tagged).
            if queue is None and (self.orders or len(self.data) >= self._main_bars
                                  or not self._time_allow_entry() or not self._filter_allow_entry()):
                return
            index = self._warmup_bars + len(self.data) - 1
            if position_filter and index < 20 or not self._signals[index]:
                return
            tag = PatternEntry(signal_bar=len(self.data) - 1, stop=self._anchors[index] * 1.001)
            if queue is not None:
                queue.register(len(self.data) - 1, "short", self.data.Low[-1], tag)
                return
            size = self._risk_entry_size()
            order = self.sell(tag=tag) if size is None else self.sell(size=size, tag=tag)
            if self._risk.get("position_sizing_enabled"):
                order._sizing_signal_bar = len(self.data) - 1

        def _pattern_confirm_open(self, is_long, payload):
            return self._pattern_confirm_open_tagged(is_long, payload)

    return ShortPatternStrategy
