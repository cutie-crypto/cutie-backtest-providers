"""Opt-in provider risk prices; touch decisions do not model protective fills.

ATR reuses the runtime kernel's first-TR seed and Wilder alpha=1/period.
Only CLOSED bars preceding the entry fill belong to the frozen entry history.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from strategy_entry_evaluators import Bar
from strategy_sl_tp_kernel import atr_series, compute_leg


@dataclass(frozen=True)
class RiskState:
    entry_price: Decimal
    initial_stop: Decimal | None
    take_price: Decimal | None
    initial_distance: Decimal | None
    direction: str


def risk_atr_series(highs, lows, closes, period: int) -> list[float | None]:
    """Precompute once; callers may only read the last closed signal-bar index."""
    bars = [Bar(i, i, close, high, low, close)
            for i, (high, low, close) in enumerate(zip(highs, lows, closes))]
    return atr_series(bars, period)


def initial_risk_state(*, risk: dict, entry_price: float, direction: str,
                       atr_value: float | None = None) -> RiskState:
    entry = Decimal(str(entry_price))
    if not entry.is_finite() or entry <= 0 or direction not in {'long', 'short'}:
        raise ValueError('INVALID_PARAMS:invalid risk entry price or direction')
    stop = None
    if risk.get('atr_stop_multiplier'):
        if atr_value is None:
            raise ValueError('INVALID_PARAMS:ATR entry value unavailable')
        distance = Decimal(str(atr_value)) * Decimal(str(risk['atr_stop_multiplier']))
        stop = entry - distance if direction == 'long' else entry + distance
        if not distance.is_finite() or distance <= 0:
            raise ValueError('INVALID_PARAMS:initial ATR distance must be finite and positive')
    elif 'stop_loss_pct' in risk:
        stop = compute_leg(bars=[], direction=direction, entry_price=entry,
                           rule=dict(type='fixed_pct', pct=Decimal(str(risk['stop_loss_pct'])) * 100),
                           is_stop_loss=True)
    if (risk.get('atr_stop_multiplier') or 'stop_loss_pct' in risk) and (
        stop is None or not stop.is_finite() or stop <= 0
        or not (stop < entry if direction == 'long' else stop > entry)
    ):
        raise ValueError('INVALID_PARAMS:initial stop unavailable or non-positive')
    distance = None if stop is None else abs(entry - stop)
    take = None
    if risk.get('take_profit_r'):
        if distance is None or distance <= 0:
            raise ValueError('INVALID_PARAMS:take_profit_r requires a positive initial risk distance')
        offset = distance * Decimal(str(risk['take_profit_r']))
        take = entry + offset if direction == 'long' else entry - offset
    elif 'take_profit_pct' in risk:
        take = compute_leg(bars=[], direction=direction, entry_price=entry,
                           rule=dict(type='fixed_pct', pct=Decimal(str(risk['take_profit_pct'])) * 100),
                           is_stop_loss=False)
    if (risk.get('take_profit_r') or 'take_profit_pct' in risk) and (
        take is None or not take.is_finite() or take <= 0
        or not (take > entry if direction == 'long' else take < entry)
    ):
        raise ValueError('INVALID_PARAMS:initial take-profit unavailable or non-positive')
    return RiskState(entry, stop, take, distance, direction)


def decide_exit(state: RiskState, *, high: float, low: float) -> str | None:
    """Stop wins a real simultaneous High/Low touch. Caller queues next-open close."""
    high, low = Decimal(str(high)), Decimal(str(low))
    if not high.is_finite() or not low.is_finite() or low <= 0 or high < low:
        raise ValueError('INVALID_PARAMS:invalid risk bar High/Low')
    long = state.direction == 'long'
    if state.initial_stop is not None and (
        low <= state.initial_stop if long else high >= state.initial_stop
    ):
        return 'stop_loss'
    if state.take_price is not None and (
        high >= state.take_price if long else low <= state.take_price
    ):
        return 'take_profit'
    return None


def risk_assumptions(risk: dict) -> dict:
    if not risk.get('risk_layer_enabled'):
        return {}
    return {'risk_layer': {
        'trigger': 'current_bar_high_low',
        'same_bar_priority': 'stop_loss_before_take_profit',
        'fill': 'next_bar_open_market',
        'fill_price_is_stop_price': False,
        'final_bar': 'engine_finalize_trades_settlement',
        'atr_seed': 'first_true_range',
        'atr_smoothing': 'alpha=1/period, adjust=False',
        'atr_history': 'available_warmup_plus_closed_bars_through_entry_signal',
        'atr_frozen_at': 'entry_signal_close',
        'initial_levels_based_on': 'actual_entry_fill_price',
        'live_execution_parity': False,
    }}
