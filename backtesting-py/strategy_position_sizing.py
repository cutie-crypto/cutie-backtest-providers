"""Opt-in single-position sizing, evaluated by the broker at the actual fill.

No future open is read by Strategy.next. Unconfigured runs install no hook.
"""
from decimal import Decimal, ROUND_FLOOR
import math

POSITION_SIZE_SCHEMA = {
    'position_size_risk_pct': {'type': 'number', 'minimum': 0, 'maximum': 100},
    'compound': {'type': 'boolean', 'default': False},
    'position_size_qty_step': {'type': 'number', 'minimum': 0.00000001, 'default': 0.00000001},
}
POSITION_SIZE_KEYS = frozenset(POSITION_SIZE_SCHEMA)


def parse_position_sizing(params, *, template_initial_stop=False):
    """template_initial_stop: the template freezes its own initial stop at the signal
    (10-B2), so the user-stop requirement is checked at the fill hook instead."""
    if not POSITION_SIZE_KEYS.intersection(params):
        return {}
    sizes = [k for k in ('position_size_risk_pct', 'position_size_pct', 'position_size_notional')
             if k in params]
    if len(sizes) != 1:
        raise ValueError('INVALID_PARAMS:exactly one position_size mode is required')
    if 'compound' in params and type(params['compound']) is not bool:
        raise ValueError('INVALID_PARAMS:compound must be a boolean')
    for key in ('position_size_risk_pct', 'position_size_qty_step'):
        if key in params:
            value = params[key]
            try:
                valid = type(value) in (float, int) and math.isfinite(value) and value > 0
            except OverflowError:
                valid = False
            if not valid or (key == 'position_size_risk_pct' and value > 100) or (
                key == 'position_size_qty_step' and value < 0.00000001):
                raise ValueError(f'INVALID_PARAMS:{key} must be finite and positive (risk <= 100; qty_step >= 1e-8)')
    if 'position_size_risk_pct' in params and not template_initial_stop and not any(params.get(k) for k in (
        'stop_loss_pct', 'atr_stop_multiplier', 'trailing_stop_pct'
    )):
        raise ValueError('INVALID_PARAMS:missing_initial_stop')
    out = dict(position_sizing_enabled=True, compound=params.get('compound', False),
               position_size_qty_step=params.get('position_size_qty_step', 0.00000001))
    if 'position_size_risk_pct' in params:
        out['position_size_risk_pct'] = Decimal(str(params['position_size_risk_pct'])) / 100
    return out


class SizingRejected(ValueError):
    pass


def risk_quantity(*, capital, risk_pct, fill_price, initial_stop, qty_step):
    """Inputs and output are user units, never multiplied by leverage."""
    if initial_stop is None:
        raise SizingRejected('missing_initial_stop')
    if not initial_stop.is_finite() or initial_stop <= 0:
        raise SizingRejected('invalid_initial_stop')
    distance = abs(fill_price - initial_stop)
    if not distance.is_finite() or distance <= 0:
        raise SizingRejected('non_positive_stop_distance')
    qty = (capital * risk_pct / distance / qty_step).to_integral_value(rounding=ROUND_FLOOR) * qty_step
    if qty < qty_step:
        raise SizingRejected('quantity_below_step')
    return qty


class PositionSizingMixin:
    def _sizing_install(self):
        from dataclasses import replace
        from strategy_risk_overlay import initial_risk_state
        broker = self._broker
        process = broker._process_orders
        open_trade = broker._open_trade
        scale = getattr(self, '_sizing_scale', Decimal(str(self._initial_capital)) / Decimal(str(self._start_equity)))
        step = Decimal(str(self._risk['position_size_qty_step']))
        initial = Decimal(str(self._initial_capital))
        fee = getattr(self, '_isolated_fee_bps', Decimal(str(broker._commission_relative)) * 10000)
        slip = getattr(self, '_isolated_slippage_bps', Decimal(0))
        self._sizing_report = dict(compound=self._risk['compound'], qty_step=str(step), fills=[], rejections=[])
        self._sizing_states = {}
        frozen = None

        def record_rejection(reason):
            self._sizing_report['rejections'].append(dict(at=int(self.data.index[-1].timestamp()), reason=reason))

        def opened(price, size, sl, tp, time_index, tag):
            open_trade(price, size, sl, tp, time_index, tag)
            if frozen is not None:
                trade = broker.trades[-1]
                self._sizing_states[trade] = replace(frozen, original_units=abs(size))

        def sized_process():
            nonlocal frozen
            # Settle queued exits first. Only then is cash/equity available for entry.
            entries = [o for o in broker.orders if not o.parent_trade]
            for order in entries:
                broker.orders.remove(order)
            process()
            for order in entries:
                broker.orders.append(order)
                frozen = stop_used = None
                try:
                    if broker._i <= getattr(order, '_sizing_signal_bar', -1):
                        raise SizingRejected('no_next_open')
                    if broker.trades or order.limit or order.stop:
                        raise SizingRejected('unsupported_entry_order')
                    price = Decimal(str(broker._adjusted_price(order.size, self.data.Open[-1])))
                    # No current close/high/low enters the sizing calculation.
                    equity = Decimal(str(broker._cash)) * scale
                    base = equity if self._risk['compound'] else initial
                    risk_pct = self._risk.get('position_size_risk_pct')
                    template_stop = getattr(self, '_sizing_template_stop', None)
                    if risk_pct is not None and template_stop is not None:
                        # 10-B2: the template's own stop, frozen at the signal; never re-derived
                        # from stop_loss_pct at the fill. Distance still uses the actual fill.
                        stop_used = template_stop(order)
                        qty = risk_quantity(capital=base, risk_pct=risk_pct, fill_price=price,
                                            initial_stop=stop_used, qty_step=step)
                    elif risk_pct is not None:
                        try:
                            frozen = initial_risk_state(risk=self._risk, entry_price=float(price),
                                direction='long' if order.is_long else 'short', entry_at=int(self.data.index[-1].value),
                                atr_value=self._risk_entry_atr
                                if self._risk.get('atr_stop_multiplier') else None)
                        except ValueError:
                            raise SizingRejected('invalid_initial_stop')
                        qty = risk_quantity(capital=base, risk_pct=risk_pct, fill_price=price,
                                            initial_stop=frozen.initial_stop, qty_step=step)
                    else:
                        notional = self._risk.get('position_size_notional')
                        if notional is None:
                            notional = base * Decimal(str(self._risk['position_size_pct'])) * self._risk.get('leverage', 1)
                        qty = (Decimal(str(notional)) / price / step).to_integral_value(rounding=ROUND_FLOOR) * step
                        if qty < step:
                            raise SizingRejected('quantity_below_step')
                    # Reserve entry fee/slippage in addition to margin. L affects only margin.
                    margin = qty * price / self._risk.get('leverage', 1)
                    costs = qty * price * (fee + slip) / 10000
                    if margin + costs > equity:
                        raise SizingRejected('insufficient_funds_after_fees')
                    units = (qty / scale).to_integral_value(rounding=ROUND_FLOOR)
                    if units < 1 or units * scale != qty:
                        raise SizingRejected('quantity_below_engine_step')
                    order._replace(size=int(units) if order.is_long else -int(units))
                    count = len(broker.trades)
                    process()
                    if len(broker.trades) <= count:
                        record_rejection('broker_rejected_entry')
                        continue
                    # Engine already deducts commission; reserve its report-only slippage too.
                    broker._cash -= float(qty * price * slip / 10000 / scale)
                    self._sizing_report['fills'].append(dict(at=int(self.data.index[-1].timestamp()),
                        capital_base=str(base), fill_price=str(price), qty=str(qty), margin=str(margin),
                        initial_stop=str(frozen.initial_stop) if frozen is not None else
                        str(stop_used) if stop_used is not None else None))
                except SizingRejected as exc:
                    if order in broker.orders:
                        broker.orders.remove(order)
                    record_rejection(str(exc))
                finally:
                    frozen = None

        broker._open_trade = opened
        broker._process_orders = sized_process
