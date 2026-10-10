"""Streaming Chan third-buy / third-sell recognition and frozen next-open execution.

Historical decisions never rebuild from future merged bars. Centers use three
completed strokes with no reuse of an existing center's strokes (no extension
or expansion). Indices refer to warmup + main original bars. direction='short'
is the strict mirror of the long third buy (up/down, ZG/ZD, top/bottom,
low/high swapped); the merged-bar, fractal, stroke and center facts are shared.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import NamedTuple
import math

import numpy as np
from backtesting import Strategy


@dataclass(frozen=True)
class ThirdBuy:
    stop: float
    center: int
    zg: float


@dataclass(frozen=True)
class ThirdSell:
    stop: float
    center: int
    zd: float


class ChanEntry(NamedTuple):
    signal_bar: int
    stop: float
    center: int
    level: float  # long: center ZG; short: center ZD


class ChanRecognizer:
    def __init__(self, *, bi_mode='new', direction='long'):
        if bi_mode not in ('new', 'old'):
            raise ValueError('INVALID_PARAMS:bi_mode must be new or old')
        if direction not in ('long', 'short'):
            raise ValueError('INVALID_PARAMS:Chan direction must be long or short')
        self.short = direction == 'short'
        self.facts_key = 'third_sells' if self.short else 'third_buys'
        self.gap = 2 if bi_mode == 'new' else 6
        self.bars = []
        self.direction = None
        self.seed = []
        self.endpoint = None
        self.center = None
        self.departure = None
        self.report = dict(merged_bars=self.bars, merges=[], fractals=[],
                           replacements=[], rejected_strokes=[], strokes=[], centers=[], **{self.facts_key: []})

    def push(self, high, low, raw_index):
        if not (math.isfinite(high) and math.isfinite(low) and 0 < low <= high):
            raise ValueError('INVALID_PARAMS:invalid Chan high/low')
        bar = dict(high=high, low=low, first_raw=raw_index, last_raw=raw_index)
        if not self.bars:
            self.bars.append(bar)
            return None
        last = self.bars[-1]
        included = ((high <= last['high'] and low >= last['low'])
                    or (high >= last['high'] and low <= last['low']))
        if included:
            if self.direction is None:
                # No prior independent bar: postpone seed direction until it is
                # observable. Union only buffers, never participates in a fractal.
                if not self.seed:
                    self.seed.append(last.copy())
                self.seed.append(bar)
                last.update(high=max(high, last['high']), low=min(low, last['low']), last_raw=raw_index)
                return None
            self._merge(last, bar, raw_index)
            return None
        self.direction = 'up' if high > last['high'] else 'down'
        if self.seed:
            # Resolve the seed only when direction is known; no past signal is
            # synthesized. Before then the union is just a pending buffer.
            choose = max if self.direction == 'up' else min
            last.update(high=choose(b['high'] for b in self.seed),
                        low=choose(b['low'] for b in self.seed))
            self.report['merges'].append(dict(at=raw_index, direction=self.direction,
                reason='initial_direction_resolved', merged_index=0,
                high=last['high'], low=last['low']))
            self.seed.clear()
            if ((high <= last['high'] and low >= last['low'])
                    or (high >= last['high'] and low <= last['low'])):
                self._merge(last, bar, raw_index)
                return None
        self.bars.append(bar)
        if len(self.bars) < 3:
            return None
        left, mid, right = self.bars[-3:]
        top = mid['high'] > max(left['high'], right['high']) and mid['low'] > max(left['low'], right['low'])
        bottom = mid['high'] < min(left['high'], right['high']) and mid['low'] < min(left['low'], right['low'])
        if not (top or bottom):
            return None
        point = dict(kind='top' if top else 'bottom', merged_index=len(self.bars)-2,
            raw_index=mid['last_raw'], price=mid['high'] if top else mid['low'], confirmed_at=raw_index)
        self.report['fractals'].append(point.copy())
        return self._point(point)

    def _merge(self, last, bar, at):
        choose = max if self.direction == 'up' else min
        last.update(high=choose(last['high'], bar['high']), low=choose(last['low'], bar['low']), last_raw=at)
        self.report['merges'].append(dict(at=at, direction=self.direction,
            merged_index=len(self.bars)-1, high=last['high'], low=last['low']))

    def _point(self, point):
        previous = self.endpoint
        if previous is None:
            self.endpoint = point
            return None
        if point['kind'] == previous['kind']:
            extreme = point['price'] > previous['price'] if point['kind'] == 'top' else point['price'] < previous['price']
            if extreme:
                self.report['replacements'].append(dict(at=point['confirmed_at'], old=previous.copy(), new=point.copy()))
                self.endpoint = point
                # Historical center and decision facts remain frozen. Replacement
                # updates only the last stroke for future center construction.
                if self.report['strokes']:
                    self.report['strokes'][-1] = self._stroke(self.report['strokes'][-1]['start'], point)
                if self.departure is not None and point['kind'] == ('bottom' if self.short else 'top'):
                    self.departure = self.report['strokes'][-1]
            return None
        if point['merged_index'] - previous['merged_index'] < self.gap:
            self.report['rejected_strokes'].append(dict(start=previous.copy(), end=point.copy(), reason='independent_bar_gap'))
            return None
        if not (point['price'] > previous['price'] if point['kind'] == 'top' else point['price'] < previous['price']):
            self.report['rejected_strokes'].append(dict(start=previous.copy(), end=point.copy(), reason='non_directional_price'))
            return None
        stroke = self._stroke(previous, point)
        self.endpoint = point
        strokes = self.report['strokes']
        strokes.append(stroke)
        signal = None
        if self.center is not None:
            center = self.center
            if self.short:
                # Mirror: a down stroke leaves below ZD; the up pullback's high
                # must stay strictly below ZD; stop sits 0.1% above that high.
                if stroke['direction'] == 'down' and stroke['low'] < center['zd']:
                    self.departure = stroke
                elif stroke['direction'] == 'up' and self.departure is not None:
                    eligible = stroke['high'] < center['zd']
                    fact = dict(center=center['id'], departure=self.departure.copy(), pullback=stroke.copy(),
                        confirmed_at=point['confirmed_at'], zd=center['zd'], pullback_high=stroke['high'],
                        status='signal' if eligible and not center['used'] else 'invalid_or_consumed')
                    self.report['third_sells'].append(fact)
                    if eligible and not center['used']:
                        center['used'] = True
                        signal = ThirdSell(stroke['high'] * 1.001, center['id'], center['zd'])
                        fact['frozen_stop'] = signal.stop
                    self.departure = None
            elif stroke['direction'] == 'up' and stroke['high'] > center['zg']:
                self.departure = stroke
            elif stroke['direction'] == 'down' and self.departure is not None:
                eligible = stroke['low'] > center['zg']
                fact = dict(center=center['id'], departure=self.departure.copy(), pullback=stroke.copy(),
                    confirmed_at=point['confirmed_at'], zg=center['zg'], pullback_low=stroke['low'],
                    status='signal' if eligible and not center['used'] else 'invalid_or_consumed')
                self.report['third_buys'].append(fact)
                if eligible and not center['used']:
                    center['used'] = True
                    signal = ThirdBuy(stroke['low'] * .999, center['id'], center['zg'])
                    fact['frozen_stop'] = signal.stop
                self.departure = None
        if len(strokes) >= 3:
            triple = strokes[-3:]
            # No reuse or extension of the previous center's completed strokes.
            if self.center is None or len(strokes)-3 > self.center['end_stroke']:
                zg = min(b['high'] for b in triple)
                zd = max(b['low'] for b in triple)
                if zg > zd:
                    self.center = dict(id=len(self.report['centers']), zg=zg, zd=zd,
                        start_stroke=len(strokes)-3, end_stroke=len(strokes)-1,
                        confirmed_at=point['confirmed_at'], used=False,
                        strokes=[b.copy() for b in triple])
                    self.report['centers'].append(self.center)
                    self.departure = None
        return signal

    @staticmethod
    def _stroke(start, end):
        return dict(start=start.copy(), end=end.copy(),
            direction='up' if end['kind']=='top' else 'down',
            high=max(start['price'], end['price']), low=min(start['price'], end['price']),
            confirmed_at=end['confirmed_at'])


def make_chan_strategy(mixin, *, bi_mode, risk, initial_capital, direction='long'):
    short = direction == 'short'
    class ChanStrategy(mixin, Strategy):
        _risk = risk
        _initial_capital = initial_capital

        def init(self):
            self._risk_init()
            arrays = []
            for column in ('High', 'Low'):
                prefix = self._warmup_cols[column] if self._warmup_bars else []
                arrays.append(np.concatenate([prefix, np.asarray(getattr(self.data, column))]))
            high, low = arrays
            recognizer = ChanRecognizer(bi_mode=bi_mode, direction=direction)
            signals = []
            for i, (h, l) in enumerate(zip(high, low)):
                signals.append(recognizer.push(float(h), float(l), i))
            self._signals = tuple(signals)
            self.chan_report = dict(index_basis='warmup_plus_main_zero_based',
                **recognizer.report, skipped_entries=[], entries=[], exits=[])
            if self._filter_config is not None and self._filter_config.pattern_confirm_enabled:
                self.chan_report['skipped_entry_count'] = 0
            self._targets = {}
            self._main_bars = len(self.data)
            process_orders = self._broker._process_orders

            def guarded_process_orders():
                for order in list(self.orders):
                    if order.parent_trade is None and isinstance(order.tag, ChanEntry):
                        opening = float(self.data.Open[-1])
                        # Use the broker's effective entry price (including spread)
                        # for gap protection, without peeking at future bars.
                        fill = self._broker._adjusted_price(order.size, opening)
                        if fill >= order.tag.stop if short else fill <= order.tag.stop:
                            self.chan_report['skipped_entries'].append(dict(
                                reason=('entry_open_at_or_above_frozen_stop' if short
                                        else 'entry_open_at_or_below_frozen_stop'),
                                signal_bar=order.tag.signal_bar, entry_bar=len(self.data)-1,
                                entry_open=opening, frozen_stop=order.tag.stop))
                            if self._pattern_confirm_queue is not None:
                                self.chan_report['skipped_entries'][-1]['adjusted_open'] = fill
                                self.chan_report['skipped_entry_count'] += 1
                            order.cancel()
                process_orders()
                for trade in self.trades:
                    tag = trade.tag
                    if isinstance(tag, ChanEntry) and tag not in self._targets:
                        self._targets[tag] = (trade.entry_price - 2 * (tag.stop - trade.entry_price) if short
                                              else trade.entry_price + 2 * (trade.entry_price - tag.stop))
                        self.chan_report['entries'].append(dict(
                            center=tag.center, entry_bar=trade.entry_bar, entry_price=trade.entry_price,
                            frozen_stop=tag.stop, frozen_target=self._targets[tag]))
            self._broker._process_orders = guarded_process_orders

        def _risk_check_exit(self):
            trade = self.trades[-1]
            if any(order.parent_trade is trade for order in self.orders):
                return True
            tag = trade.tag
            if self._risk.get('leverage', 1) > 1 and self._risk_isolated_exit(Decimal(str(tag.stop))):
                return True
            if self.data.High[-1] >= tag.stop if short else self.data.Low[-1] <= tag.stop:
                reason = 'stop_loss'
            elif (self._time_config is not None or self._risk.get('max_holding_bars')) and (fact := self._holding_expiry()).due:
                self._record_holding_expiry(fact)
                reason = 'time_expiry'
            elif self.data.Low[-1] <= self._targets[tag] if short else self.data.High[-1] >= self._targets[tag]:
                reason = 'take_profit'
            elif self.data.Close[-1] > tag.level if short else self.data.Close[-1] < tag.level:
                reason = 'close_above_zd' if short else 'close_below_zg'
            else:
                return False
            self._risk_exit_reason = reason
            self.chan_report['exits'].append(dict(reason=reason, decision_bar=len(self.data)-1))
            self.position.close()
            return True

        def next(self):
            if self.position:
                self._risk_check_exit()
                return
            queue = self._pattern_confirm_queue
            # P-PATCONF-2b2: with confirmation on, the signal bar s only registers; the entry gates
            # are judged at the confirming bar k (_pattern_confirm_open_tagged).
            if queue is None and (self.orders or len(self.data) >= self._main_bars
                                  or not self._time_allow_entry() or not self._filter_allow_entry()):
                return
            signal = self._signals[self._warmup_bars + len(self.data) - 1]
            if signal is None:
                return
            tag = ChanEntry(len(self.data)-1, signal.stop, signal.center, signal.zd if short else signal.zg)
            if queue is not None:
                # P-PATCONF-2b2: the stop (and the center zd / zg) stay frozen at s and travel with the signal.
                queue.register(len(self.data) - 1, "short" if short else "long",
                               self.data.Low[-1] if short else self.data.High[-1], tag)
                return
            size = self._risk_entry_size()
            entry = self.sell if short else self.buy
            order = entry(tag=tag) if size is None else entry(size=size, tag=tag)
            if self._risk.get("position_sizing_enabled"):
                order._sizing_signal_bar = len(self.data) - 1

        def _pattern_confirm_open(self, is_long, payload):
            return self._pattern_confirm_open_tagged(is_long, payload)

    return ChanStrategy
