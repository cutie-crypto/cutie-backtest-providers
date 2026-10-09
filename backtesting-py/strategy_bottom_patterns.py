"""Causal confirmed-swing bottom setups and next-open orders."""
from dataclasses import asdict, dataclass
from decimal import Decimal

import numpy as np
from backtesting import Strategy
from strategy_swing_points import swing_points


@dataclass(frozen=True)
class BottomEntry:
    signal_bar: int
    stop: float
    target: float


def bottom_signals(high, low, close, *, kind, n=5, min_gap=10, max_gap=60,
                   tolerance=0.01, rebound=0.03, head_depth=0.02, shoulder_tolerance=0.03):
    events = swing_points(high, low, n=n)
    signals, details, points = [None] * len(close), [], []
    active = None
    count = 2 if kind == 'double_bottom' else 3

    def peak(left, right):
        # Exclude endpoint candles; ties choose the earliest High (codex-2 定).
        index = max(range(left.index + 1, right.index), key=lambda i: high[i])
        return index, float(high[index])

    def neckline(setup, t):
        i1, p1 = setup['neckline_points'][0]
        i2, p2 = setup['neckline_points'][-1]
        return p1 if i1 == i2 else p1 + (p2 - p1) * (t - i1) / (i2 - i1)

    for t in range(len(close)):
        point = events.lows[t]
        if point is not None:
            if active is not None:
                active.update(status='invalidated_new_low', invalidated_at=t)
                active = None
            points.append(point)
            if len(points) >= count:
                used = points[-count:]
                if kind == 'double_bottom':
                    l1, l2 = used
                    gap = l2.index - l1.index
                    if not (min_gap <= gap <= max_gap and
                            abs(l2.price - l1.price) / min(l1.price, l2.price) <= tolerance):
                        continue
                    neck = peak(l1, l2)
                    if neck[1] < max(l1.price, l2.price) * (1 + rebound):
                        continue
                    necks = [neck]
                else:
                    s1, head, s2 = used
                    if not (head.price <= s1.price * (1 - head_depth) and
                            head.price <= s2.price * (1 - head_depth) and
                            abs(s2.price - s1.price) / min(s1.price, s2.price) <= shoulder_tolerance):
                        continue
                    necks = [peak(s1, head), peak(head, s2)]
                active = dict(points=[asdict(p) for p in used], neckline_points=necks,
                              confirmed_at=t, stop=used[-1].price * 0.999, status='armed')
                details.append(active)
        if active is None:
            continue
        if close[t] < active['stop']:
            active.update(status='invalidated_close', invalidated_at=t)
            active = None
            continue
        neck = neckline(active, t)
        if close[t] > neck:
            target = (2 * neck - min(p['price'] for p in active['points']) if kind == 'double_bottom'
                      else neck + neckline(active, active['points'][1]['index']) - active['points'][1]['price'])
            # A falling neckline must still yield a positive measured move.
            if target <= neck or active['stop'] >= neck:
                active.update(status='invalidated_geometry', invalidated_at=t)
                active = None
                continue
            signals[t] = BottomEntry(t, active['stop'], target)
            active.update(status='breakout', breakout_bar=t, neckline_at_breakout=neck, target=target)
            active = None  # consume once, independently of positions/session gates
    return signals, details


def make_bottom_strategy(mixin, *, kind, risk, initial_capital, config):
    class BottomStrategy(mixin, Strategy):
        _risk = risk
        _initial_capital = initial_capital

        def init(self):
            self._risk_init()
            arrays = []
            for column in ('High', 'Low', 'Close'):
                prefix = self._warmup_cols[column] if self._warmup_bars else []
                arrays.append(np.concatenate([prefix, np.asarray(getattr(self.data, column))]))
            self._signals, setups = bottom_signals(*arrays, kind=kind, **config)
            self.bottom_pattern_report = dict(kind=kind, config=config, setups=setups,
                index_origin='warmup_plus_main', warmup_bars=self._warmup_bars,
                skipped_entry_count=0, skipped_entries=[])
            self._main_bars = len(self.data)
            process_orders = self._broker._process_orders

            def guarded_process_orders():
                for order in list(self.orders):
                    if order.parent_trade is None and isinstance(order.tag, BottomEntry):
                        opening = float(self.data.Open[-1])
                        if opening <= order.tag.stop:
                            self.bottom_pattern_report['skipped_entry_count'] += 1
                            self.bottom_pattern_report['skipped_entries'].append(dict(
                                reason='entry_open_at_or_below_frozen_stop', signal_bar=order.tag.signal_bar,
                                entry_bar=len(self.data)-1, entry_open=opening, frozen_stop=order.tag.stop))
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
            if self._risk.get('leverage', 1) > 1 and self._risk_isolated_exit(Decimal(str(trade.tag.stop))):
                return True
            if self.data.Low[-1] <= trade.tag.stop:
                self._risk_exit_reason = 'stop_loss'
            elif (self._time_config is not None or self._risk.get('max_holding_bars')) and (fact := self._holding_expiry()).due:
                self._record_holding_expiry(fact)
            elif self.data.High[-1] >= trade.tag.target:
                self._risk_exit_reason = 'take_profit'
            else:
                return False
            self.position.close()
            return True

        def next(self):
            if self.position:
                self._risk_check_exit()
                return
            if self._risk.get('leverage', 1) > 1 and self._isolated_blocked_bar == len(self.data)-1:
                return
            # Judgment bar = the breakout close; a filtered signal is discarded (signals fire once), not delayed.
            if self.orders or len(self.data) >= self._main_bars or not self._time_allow_entry() or not self._filter_allow_entry():
                return
            signal = self._signals[self._warmup_bars + len(self.data) - 1]
            if signal is None:
                return
            tag = BottomEntry(len(self.data)-1, signal.stop, signal.target)
            size = self._risk_entry_size()
            order = self.buy(tag=tag) if size is None else self.buy(size=size, tag=tag)
            if self._risk.get("position_sizing_enabled"):
                order._sizing_signal_bar = len(self.data) - 1
    return BottomStrategy
