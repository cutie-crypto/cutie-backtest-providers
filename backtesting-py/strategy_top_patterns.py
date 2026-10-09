"""Confirmed-swing bearish mirror; keeps the historical bottom engine untouched."""
from dataclasses import asdict, dataclass
from decimal import Decimal

import numpy as np
from backtesting import Strategy
from strategy_swing_points import swing_points


@dataclass(frozen=True)
class TopEntry:
    signal_bar: int
    stop: float
    target: float


def top_signals(high, low, close, *, kind, n=5, min_gap=10, max_gap=60,
                tolerance=0.01, decline=0.03, head_height=0.02, shoulder_tolerance=0.03):
    if kind not in ('double_top', 'head_shoulders'):
        raise ValueError('INVALID_PARAMS:unknown top pattern')
    events = swing_points(high, low, n=n)
    signals, details, points = [None] * len(close), [], []
    active = None
    count = 2 if kind == 'double_top' else 3

    def trough(left, right):
        # Endpoints excluded; equal Low chooses the earliest candle (codex-3 定).
        index = min(range(left.index + 1, right.index), key=lambda i: low[i])
        return index, float(low[index])

    def neckline(setup, t):
        i1, p1 = setup['neckline_points'][0]
        i2, p2 = setup['neckline_points'][-1]
        return p1 if i1 == i2 else p1 + (p2 - p1) * (t - i1) / (i2 - i1)

    for t in range(len(close)):
        point = events.highs[t]
        if point is not None:
            if active is not None:
                active.update(status='invalidated_new_high', invalidated_at=t)
                active = None
            points.append(point)
            if len(points) >= count:
                used = points[-count:]
                if kind == 'double_top':
                    h1, h2 = used
                    gap = h2.index - h1.index
                    if not (min_gap <= gap <= max_gap and
                            abs(Decimal(str(h2.price)) - Decimal(str(h1.price))) / min(Decimal(str(h1.price)), Decimal(str(h2.price))) <= Decimal(str(tolerance))):
                        continue
                    neck = trough(h1, h2)
                    if Decimal(str(neck[1])) > min(Decimal(str(h1.price)), Decimal(str(h2.price))) * (1 - Decimal(str(decline))):
                        continue
                    necks = [neck]
                else:
                    s1, head, s2 = used
                    if not (Decimal(str(head.price)) >= Decimal(str(s1.price)) * (1 + Decimal(str(head_height))) and
                            Decimal(str(head.price)) >= Decimal(str(s2.price)) * (1 + Decimal(str(head_height))) and
                            abs(Decimal(str(s2.price)) - Decimal(str(s1.price))) / min(Decimal(str(s1.price)), Decimal(str(s2.price))) <= Decimal(str(shoulder_tolerance))):
                        continue
                    necks = [trough(s1, head), trough(head, s2)]
                active = dict(points=[asdict(p) for p in used], neckline_points=necks,
                              confirmed_at=t, stop=float(Decimal(str(used[-1].price)) * Decimal('1.001')), status='armed')
                details.append(active)
        if active is None:
            continue
        if close[t] > active['stop']:
            active.update(status='invalidated_close', invalidated_at=t)
            active = None
            continue
        neck = neckline(active, t)
        if close[t] < neck:
            target = (2 * neck - max(p['price'] for p in active['points']) if kind == 'double_top'
                      else neck - (active['points'][1]['price'] - neckline(active, active['points'][1]['index'])))
            if target <= 0 or target >= neck or active['stop'] <= neck:
                active.update(status='invalidated_geometry', invalidated_at=t)
                active = None
                continue
            signals[t] = TopEntry(t, active['stop'], target)
            active.update(status='breakout', breakout_bar=t, neckline_at_breakout=neck, target=target)
            active = None  # Consume even if a session gate or an existing position blocks entry.
    return signals, details


def make_top_strategy(mixin, *, kind, risk, initial_capital, config):
    class TopStrategy(mixin, Strategy):
        _risk = risk
        _initial_capital = initial_capital

        def init(self):
            self._risk_init()
            arrays = []
            for column in ('High', 'Low', 'Close'):
                prefix = self._warmup_cols[column] if self._warmup_bars else []
                arrays.append(np.concatenate([prefix, np.asarray(getattr(self.data, column))]))
            self._signals, setups = top_signals(*arrays, kind=kind, **config)
            self.top_pattern_report = dict(kind=kind, config=config, setups=setups,
                index_origin='warmup_plus_main', warmup_bars=self._warmup_bars,
                skipped_entry_count=0, skipped_entries=[])
            self._main_bars = len(self.data)
            process_orders = self._broker._process_orders

            def guarded_process_orders():
                for order in list(self.orders):
                    if order.parent_trade is None and isinstance(order.tag, TopEntry):
                        opening = float(self.data.Open[-1])
                        if opening >= order.tag.stop:
                            self.top_pattern_report['skipped_entry_count'] += 1
                            self.top_pattern_report['skipped_entries'].append(dict(
                                reason='entry_open_at_or_above_frozen_stop', signal_bar=order.tag.signal_bar,
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
            if self.data.High[-1] >= trade.tag.stop:
                self._risk_exit_reason = 'stop_loss'
            elif (self._time_config is not None or self._risk.get('max_holding_bars')) and (fact := self._holding_expiry()).due:
                self._record_holding_expiry(fact)
            elif self.data.Low[-1] <= trade.tag.target:
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
            if self.orders or len(self.data) >= self._main_bars or not self._time_allow_entry():
                return
            signal = self._signals[self._warmup_bars + len(self.data) - 1]
            if signal is None:
                return
            tag = TopEntry(len(self.data)-1, signal.stop, signal.target)
            size = self._risk_entry_size()
            self.sell(tag=tag) if size is None else self.sell(size=size, tag=tag)
    return TopStrategy
