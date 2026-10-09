"""Causal long-only divergence facts and next-open single-position execution."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import NamedTuple
import math

import numpy as np
import pandas as pd
from backtesting import Strategy

from strategy_swing_points import swing_points


@dataclass(frozen=True)
class Divergence:
    l1: int
    l2: int
    stop: float
    setup: int


class DivergenceEntry(NamedTuple):
    signal_bar: int
    stop: float
    setup: int


def macd_values(close, fast, slow, signal):
    values = pd.Series(close, dtype='float64')
    dif = values.ewm(span=fast, adjust=False).mean() - values.ewm(span=slow, adjust=False).mean()
    dea = dif.ewm(span=signal, adjust=False).mean()
    return dif.to_numpy(), dea.to_numpy(), (2 * (dif - dea)).to_numpy()


def divergence_signals(high, low, close, values, *, kind, n=5, min_gap=5, max_gap=60,
                       first_below=35, dif=None, dea=None, valid_from=0):
    """Slots are decision closes; swing events are read only at confirmation.

    MACD may cross at the L2 confirmation close. New confirmed lows replace the
    pending pair before considering that close's cross. Consumption is independent
    of position, session, filter or next-open gap, so each pair has one opportunity.
    """
    swings = swing_points(high, low, n=n)
    signals, setups = [None] * len(close), []
    previous, armed = None, None
    for i in range(len(close)):
        point = swings.lows[i]
        if point is not None:
            if armed is not None:
                setups[armed.setup].update(status='invalidated_new_low', invalidated_at=i)
                armed = None
            if previous is not None:
                left = previous
                a, b = values[left.index], values[point.index]
                if (left.index >= valid_from and min_gap <= point.index - left.index <= max_gap
                        and math.isfinite(a) and math.isfinite(b)
                        and point.price < left.price and b > a
                        and (kind != 'rsi' or a < first_below)):
                    setup = dict(l1_index=left.index, l2_index=point.index,
                                 l1_price=left.price, l2_price=point.price,
                                 l1_indicator=float(a), l2_indicator=float(b),
                                 confirmed_at=i, status='waiting')
                    setups.append(setup)
                    armed = Divergence(left.index, point.index, point.price * .999, len(setups)-1)
            previous = point
        if armed is None:
            continue
        if close[i] < armed.stop:
            setups[armed.setup].update(status='invalidated_close', invalidated_at=i)
            armed = None
            continue
        if kind == 'rsi' or (i > 0 and dif[i-1] <= dea[i-1] and dif[i] > dea[i]):
            signals[i] = armed
            setups[armed.setup].update(status='signal', signal_bar=i, frozen_stop=armed.stop)
            armed = None
    return tuple(signals), setups


def make_divergence_strategy(mixin, *, kind, config, risk, initial_capital, rsi_series):
    class DivergenceStrategy(mixin, Strategy):
        _risk = risk
        _initial_capital = initial_capital

        def init(self):
            self._risk_init()
            arrays = []
            for column in ('High', 'Low', 'Close'):
                prefix = self._warmup_cols[column] if self._warmup_bars else []
                arrays.append(np.concatenate([prefix, np.asarray(getattr(self.data, column))]))
            high, low, close = arrays
            if kind == 'macd':
                self._dif, self._dea, hist = macd_values(close, config['fast'], config['slow'], config['signal'])
                values = self._dif if config['compare'] == 'dif' else hist
                cross = dict(dif=self._dif, dea=self._dea)
            else:
                values = rsi_series(close, config['rsi_period'])
                self._rsi = values
                cross = {}
            self._signals, setups = divergence_signals(high, low, close, values, kind=kind,
                n=config['n'], min_gap=config['min_gap'], max_gap=config['max_gap'],
                valid_from=config['indicator_bars']-1, first_below=config.get('first_below', 35), **cross)
            self.divergence_report = dict(kind=kind, compare=config.get('compare', 'rsi'),
                index_basis='warmup_plus_main_zero_based', setups=setups,
                skipped_entries=[], entries=[], exits=[])
            self._targets = {}
            self._main_bars = len(self.data)
            process_orders = self._broker._process_orders

            def guarded_process_orders():
                for order in list(self.orders):
                    if order.parent_trade is None and isinstance(order.tag, DivergenceEntry):
                        opening = float(self.data.Open[-1])
                        # Use the broker's effective entry price (including spread)
                        # for gap protection, without peeking at future bars.
                        fill = self._broker._adjusted_price(order.size, opening)
                        if fill <= order.tag.stop:
                            self.divergence_report['skipped_entries'].append(dict(
                                reason='entry_open_at_or_below_frozen_stop',
                                signal_bar=order.tag.signal_bar, entry_bar=len(self.data)-1,
                                entry_open=opening, frozen_stop=order.tag.stop))
                            order.cancel()
                process_orders()
                for trade in self.trades:
                    tag = trade.tag
                    if isinstance(tag, DivergenceEntry) and tag not in self._targets:
                        self._targets[tag] = trade.entry_price + 2 * (trade.entry_price - tag.stop)
                        self.divergence_report['entries'].append(dict(
                            setup=tag.setup, entry_bar=trade.entry_bar, entry_price=trade.entry_price,
                            frozen_stop=tag.stop, frozen_target=self._targets[tag]))
            self._broker._process_orders = guarded_process_orders

        def _risk_check_exit(self):
            trade = self.trades[-1]
            if any(order.parent_trade is trade for order in self.orders):
                return True
            tag = trade.tag
            if self._risk.get('leverage', 1) > 1 and self._risk_isolated_exit(Decimal(str(tag.stop))):
                return True
            index = self._warmup_bars + len(self.data) - 1
            if self.data.Low[-1] <= tag.stop:
                reason = 'stop_loss'
            elif (self._time_config is not None or self._risk.get('max_holding_bars')) and (fact := self._holding_expiry()).due:
                self._record_holding_expiry(fact)
                reason = 'time_expiry'
            elif self.data.High[-1] >= self._targets[tag]:
                reason = 'take_profit'
            elif kind == 'macd' and self._dif[index-1] >= self._dea[index-1] and self._dif[index] < self._dea[index]:
                reason = 'macd_cross_down'
            elif kind == 'rsi' and self._rsi[index] > config['exit_above']:
                reason = 'rsi_exit'
            else:
                return False
            self._risk_exit_reason = reason
            self.divergence_report['exits'].append(dict(reason=reason, decision_bar=len(self.data)-1))
            self.position.close()
            return True

        def next(self):
            if self.position:
                self._risk_check_exit()
                return
            if (self.orders or len(self.data) >= self._main_bars
                    or self._warmup_bars + len(self.data) < config['indicator_bars']
                    or not self._time_allow_entry() or not self._filter_allow_entry()):
                return
            signal = self._signals[self._warmup_bars + len(self.data) - 1]
            if signal is None:
                return
            tag = DivergenceEntry(len(self.data)-1, signal.stop, signal.setup)
            size = self._risk_entry_size()
            self.buy(tag=tag) if size is None else self.buy(size=size, tag=tag)

    return DivergenceStrategy
