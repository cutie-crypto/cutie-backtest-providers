"""Opt-in provider risk prices; touch decisions do not model protective fills.

ATR reuses the runtime kernel's first-TR seed and Wilder alpha=1/period.
Only CLOSED bars preceding the entry fill belong to the frozen entry history.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_FLOOR

from strategy_dynamic_stop import StopRules, StopState, initial_stop_state, advance_stop
from strategy_entry_evaluators import Bar
from strategy_sl_tp_kernel import atr_series, compute_leg


@dataclass(frozen=True)
class RiskState:
    entry_price: Decimal
    initial_stop: Decimal | None
    take_price: Decimal | None
    initial_distance: Decimal | None
    direction: str
    stop_state: StopState | None = None
    original_units: int = 0
    levels: tuple[tuple[Decimal, Decimal], ...] = ()
    reached_levels: int = 0


def risk_atr_series(highs, lows, closes, period: int) -> list[float | None]:
    """Precompute once; callers may only read the last closed signal-bar index."""
    bars = [Bar(i, i, close, high, low, close)
            for i, (high, low, close) in enumerate(zip(highs, lows, closes))]
    return atr_series(bars, period)


def initial_stop_price(*, risk: dict, entry: Decimal, direction: str,
                       atr_value: float | None = None) -> Decimal | None:
    """Shared frozen stop calculation for fill-time sizing and initial R."""
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
    elif risk.get('trailing_stop_pct'):
        offset = entry * Decimal(str(risk['trailing_stop_pct'])) / 100
        stop = entry - offset if direction == 'long' else entry + offset
    if (risk.get('atr_stop_multiplier') or 'stop_loss_pct' in risk or risk.get('trailing_stop_pct')) and (
        stop is None or not stop.is_finite() or stop <= 0
        or not (stop < entry if direction == 'long' else stop > entry)
    ):
        raise ValueError('INVALID_PARAMS:initial stop unavailable or non-positive')
    return stop


def initial_risk_state(*, risk: dict, entry_price: float, direction: str,
                       atr_value: float | None = None, entry_at: int = 0, original_units: int = 0) -> RiskState:
    entry = Decimal(str(entry_price))
    if not entry.is_finite() or entry <= 0 or direction not in {'long', 'short'}:
        raise ValueError('INVALID_PARAMS:invalid risk entry price or direction')
    stop = initial_stop_price(risk=risk, entry=entry, direction=direction, atr_value=atr_value)
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
    dynamic = bool(risk.get('trailing_stop_pct') or risk.get('breakeven_stop'))
    managed = initial_stop_state(direction=direction, entry_price=entry, initial_stop=stop,
                                 entry_at=entry_at) if dynamic else None
    levels = []
    for n in (1, 2, 3):
        if not risk.get(f'tp{n}_r'):
            break
        offset = distance * Decimal(str(risk[f'tp{n}_r']))
        target = entry + offset if direction == 'long' else entry - offset
        if target <= 0 or not target.is_finite() or target == entry:
            raise ValueError('INVALID_PARAMS:initial take-profit level unavailable or non-positive')
        levels.append((target, Decimal(str(risk[f'tp{n}_close_pct']))))
    return RiskState(entry, stop, take, distance, direction, managed, original_units, tuple(levels))


def decide_exit(state: RiskState, *, high: float, low: float, holding_due: bool = False) -> str | None:
    """Stop wins a real simultaneous High/Low touch. Caller queues next-open close."""
    high, low = Decimal(str(high)), Decimal(str(low))
    if not high.is_finite() or not low.is_finite() or low <= 0 or high < low:
        raise ValueError('INVALID_PARAMS:invalid risk bar High/Low')
    long = state.direction == 'long'
    stop = state.stop_state.effective_stop if state.stop_state is not None else state.initial_stop
    if stop is not None and (
        low <= stop if long else high >= stop
    ):
        return 'stop_loss'
    if holding_due:
        return 'time_expiry'
    if state.take_price is not None and (
        high >= state.take_price if long else low <= state.take_price
    ):
        return 'take_profit'
    return None


def advance_risk_state(state: RiskState, risk: dict, *, open_at: int, close_at: int,
                       high: float, low: float, close: float) -> RiskState:
    if state.stop_state is None:
        return state
    rules = StopRules(trailing_pct=Decimal(str(risk['trailing_stop_pct']))
                      if risk.get('trailing_stop_pct') else None,
                      breakeven=bool(risk.get('breakeven_stop')))
    managed = advance_stop(state.stop_state, rules, open_at=open_at, close_at=close_at,
                           high=Decimal(str(high)), low=Decimal(str(low)), close=Decimal(str(close)))
    return replace(state, stop_state=managed)


def level_exit(state: RiskState, *, high: float, low: float, remaining_units: int) -> tuple[RiskState, int]:
    """Cumulative allocation uses ORIGINAL integer units; submission advances milestones.

    The adapter suppresses repeated decisions while a parent close order is pending.
    Actual filled units are reconciled from the surviving trade's size.
    """
    reached = state.reached_levels
    while reached < len(state.levels):
        target = state.levels[reached][0]
        if not (Decimal(str(high)) >= target if state.direction == 'long' else Decimal(str(low)) <= target):
            break
        reached += 1
    if reached == state.reached_levels:
        return state, 0
    cumulative = sum(pct for _, pct in state.levels[:reached])
    already_closed = state.original_units - remaining_units
    target_units = int((Decimal(state.original_units) * cumulative / 100).to_integral_value(rounding=ROUND_FLOOR))
    units = remaining_units if cumulative == 100 else max(0, target_units - already_closed)
    return replace(state, reached_levels=reached), min(units, remaining_units)


def risk_assumptions(risk: dict) -> dict:
    if not risk.get('risk_layer_enabled'):
        return {}
    layer = {
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
    }
    if risk.get('trailing_stop_pct'):
        layer.update(trailing_basis='post_entry_highest_high_or_lowest_low',
                     dynamic_stop_effective='following_bar', dynamic_stop_bar_end='next_open_minus_1ns')
    if risk.get('breakeven_stop'):
        layer.update(breakeven_trigger='close_at_frozen_initial_1R', dynamic_stop_effective='following_bar',
                     dynamic_stop_bar_end='next_open_minus_1ns')
    if risk.get('max_holding_bars'):
        layer.update(holding_bar_count_from='entry_fill_bar_is_1',
                     same_bar_priority='stop_loss_before_time_expiry_before_take_profit')
    if risk.get('tp1_r'):
        layer.update(take_profit_levels_basis='original_units_cumulative_floor_frozen_initial_R',
                     take_profit_final_100='close_all_remaining', take_profit_zero_units='mark_reached_without_order')
    return {'risk_layer': layer}
