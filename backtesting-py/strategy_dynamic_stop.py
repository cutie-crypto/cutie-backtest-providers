"""Shared closed-bar trailing/breakeven ratchet for F108.

The caller checks the bar against the previously effective stop BEFORE calling
advance_stop. The returned stop is effective only for the following bar.
Entry-bar extremes are excluded: OHLC cannot separate pre-fill from post-fill
prices. The entry price seeds the extreme until a full post-entry bar closes.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal


@dataclass(frozen=True)
class StopRules:
    trailing_pct: Decimal | None = None
    breakeven: bool = False
    price_tick: Decimal | None = None

    def __post_init__(self):
        if type(self.breakeven) is not bool:
            raise ValueError("breakeven must be boolean")
        if self.trailing_pct is not None and (
            not isinstance(self.trailing_pct, Decimal)
            or not self.trailing_pct.is_finite()
            or not Decimal(0) < self.trailing_pct < Decimal(100)
        ):
            raise ValueError("trailing percentage must be between zero and 100")
        if self.trailing_pct is None and not self.breakeven:
            raise ValueError("at least one stop management rule is required")
        if self.price_tick is not None and (
            not isinstance(self.price_tick, Decimal) or not self.price_tick.is_finite() or self.price_tick <= 0
        ):
            raise ValueError("price tick must be a finite positive decimal")


@dataclass(frozen=True)
class StopState:
    direction: str
    entry_price: Decimal
    initial_stop: Decimal
    entry_at: int
    effective_stop: Decimal
    extreme: Decimal
    breakeven_active: bool = False
    last_bar_close: int | None = None


def initial_stop_state(*, direction: str, entry_price: Decimal, initial_stop: Decimal, entry_at: int) -> StopState:
    if direction not in {"long", "short"} or type(entry_at) is not int or entry_at < 0:
        raise ValueError("invalid stop position identity")
    if any(not isinstance(v, Decimal) or not v.is_finite() or v <= 0 for v in (entry_price, initial_stop)):
        raise ValueError("stop prices must be finite positive decimals")
    if not (initial_stop < entry_price if direction == "long" else initial_stop > entry_price):
        raise ValueError("initial stop must be on the loss side")
    return StopState(direction, entry_price, initial_stop, entry_at, initial_stop, entry_price)


def advance_stop(
    state: StopState, rules: StopRules, *, open_at: int, close_at: int, high: Decimal, low: Decimal, close: Decimal
) -> StopState:
    """Return one combined stop, monotonically tightening after complete bars.

    No entry/exit orders or notifications are emitted here. Already processed or
    pre-entry bars are no-ops, so catch-up and duplicate delivery cannot rewind
    the ratchet. 1R is frozen from entry and INITIAL stop, never the moving stop.
    """
    if type(open_at) is not int or type(close_at) is not int or open_at < 0 or close_at < open_at:
        raise ValueError("invalid closed bar times")
    if any(not isinstance(v, Decimal) or not v.is_finite() or v <= 0 for v in (high, low, close)):
        raise ValueError("bar prices must be finite positive decimals")
    if not low <= close <= high:
        raise ValueError("invalid closed bar prices")
    if open_at <= state.entry_at or (state.last_bar_close is not None and close_at <= state.last_bar_close):
        return state
    if state.last_bar_close is not None and open_at <= state.last_bar_close:
        raise ValueError("overlapping stop management bars")
    long = state.direction == "long"
    tighten = max if long else min
    extreme = tighten(state.extreme, high if long else low)
    candidate = state.effective_stop
    if rules.trailing_pct is not None:
        factor = Decimal(1) + (-1 if long else 1) * rules.trailing_pct / 100
        candidate = tighten(candidate, extreme * factor)
    risk = abs(state.entry_price - state.initial_stop)
    reached = close >= state.entry_price + risk if long else close <= state.entry_price - risk
    active = state.breakeven_active or (rules.breakeven and reached)
    if active:
        candidate = tighten(candidate, state.entry_price)
    if rules.price_tick is not None and candidate != state.effective_stop:
        # Tick conversion is part of the replay rule, never an App-only change.
        # Long rounds up and short down, preserving the shared tightening rule.
        rounded = (candidate / rules.price_tick).to_integral_value(
            rounding=ROUND_CEILING if long else ROUND_FLOOR
        ) * rules.price_tick
        if rounded <= 0:
            raise ValueError("price tick cannot represent a positive stop")
        candidate = tighten(state.effective_stop, rounded)
    return replace(state, effective_stop=candidate, extreme=extreme, breakeven_active=active, last_bar_close=close_at)
