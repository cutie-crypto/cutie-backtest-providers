"""F108 canonical completed-trade accounting, independent of display PnL.

Prices here are reference prices BEFORE slippage. Adapters must supply explicit
cost evidence; missing funding must never silently become zero. Monetary amounts
share the quote currency. Persistence/entry serialization belongs to the caller.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Literal

ZERO = Decimal("0")
ONE = Decimal("1")
DRAWDOWN_LIMIT = Decimal("0.20")


def _finite(value: Decimal) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("accounting values must be finite Decimal amounts")


@dataclass(frozen=True)
class ExitLeg:
    quantity: Decimal
    reference_price: Decimal
    fee: Decimal
    slippage_cost: Decimal


@dataclass(frozen=True)
class CompletedTrade:
    direction: Literal["long", "short"]
    quantity: Decimal
    entry_reference_price: Decimal
    initial_margin: Decimal
    entry_fee: Decimal
    entry_slippage_cost: Decimal
    # Signed cost: funding received is negative. None means unavailable evidence.
    funding_paid: Decimal | None
    exits: tuple[ExitLeg, ...]

    def net_return(self) -> Decimal:
        """Full-trade net return on frozen initial margin, without tolerance."""
        if self.direction not in ("long", "short"):
            raise ValueError("invalid trade direction")
        if self.funding_paid is None:
            raise ValueError("complete funding evidence is required")
        for value in (self.quantity, self.entry_reference_price, self.initial_margin):
            _finite(value)
            if value <= ZERO:
                raise ValueError("quantity, price and margin must be positive")
        _finite(self.funding_paid)
        costs = self.entry_fee + self.entry_slippage_cost + self.funding_paid
        for value in (self.entry_fee, self.entry_slippage_cost):
            _finite(value)
            if value < ZERO:
                raise ValueError("fee and slippage costs cannot be negative")
        gross = ZERO
        closed = ZERO
        sign = ONE if self.direction == "long" else -ONE
        for leg in self.exits:
            for value in (leg.quantity, leg.reference_price, leg.fee, leg.slippage_cost):
                _finite(value)
            if leg.quantity <= ZERO or leg.reference_price <= ZERO or leg.fee < ZERO or leg.slippage_cost < ZERO:
                raise ValueError("invalid exit leg")
            closed += leg.quantity
            gross += sign * leg.quantity * (leg.reference_price - self.entry_reference_price)
            costs += leg.fee + leg.slippage_cost
        if closed != self.quantity:
            raise ValueError("only fully closed trades can settle risk accounting")
        return (gross - costs) / self.initial_margin


@dataclass(frozen=True)
class RiskState:
    """Normalized cycle equity; each trade invests the cycle equity as margin.

    This sizing convention must also be used by the matching backtest adapter.
    No implicit cycle reset: restoring entries requires a new persisted cycle.
    """

    equity: Decimal = ONE
    peak_equity: Decimal = ONE
    consecutive_losses: int = 0
    entries_paused: bool = False
    pause_reason: str | None = None


@dataclass(frozen=True)
class RiskSettlement:
    state: RiskState
    warn_author: bool
    newly_paused: bool
    drawdown: Decimal


def settle_risk(state: RiskState, trade: CompletedTrade) -> RiskSettlement:
    net_return = trade.net_return()
    for value in (state.equity, state.peak_equity):
        _finite(value)
    if (
        state.equity < ZERO
        or state.peak_equity <= ZERO
        or state.equity > state.peak_equity
        or state.consecutive_losses < 0
    ):
        raise ValueError("invalid risk state")
    losses = state.consecutive_losses
    if net_return < ZERO:
        losses += 1
    elif net_return > ZERO:
        losses = 0
    # A gap can exhaust the cycle. Equity bottoms at zero; trade retains full loss.
    equity = max(ZERO, state.equity * (ONE + net_return))
    peak = max(state.peak_equity, equity)
    drawdown = (peak - equity) / peak
    reason = state.pause_reason
    if not state.entries_paused:
        if drawdown >= DRAWDOWN_LIMIT:
            reason = "net_drawdown"
        elif losses >= 5:
            reason = "consecutive_losses"
    paused = state.entries_paused or reason is not None
    return RiskSettlement(
        state=replace(
            state,
            equity=equity,
            peak_equity=peak,
            consecutive_losses=losses,
            entries_paused=paused,
            pause_reason=reason,
        ),
        warn_author=losses == 3 and net_return < ZERO and not state.entries_paused,
        newly_paused=paused and not state.entries_paused,
        drawdown=drawdown,
    )


def completed_signal_trade(
    signal: dict,
    partial_exits: list[dict],
    *,
    fee_bps: Decimal,
    slippage_bps: Decimal,
    funding_paid: Decimal | None,
) -> CompletedTrade:
    """Translate the platform's executed exit ledger, never its display PnL.

    Quantity is normalized to one unit for a 100% filled signal; funding amounts
    must use that same quantity convention. Exit advice rows already store the
    executed percentage, so do not reapply their requested close percentages.
    """
    if signal.get("status") != "closed":
        raise ValueError("signal is not closed")
    for value in (fee_bps, slippage_bps):
        _finite(value)
        if value < ZERO:
            raise ValueError("cost basis points cannot be negative")
    filled = Decimal(str(signal.get("filled_position_pct"))) / Decimal("100")
    _finite(filled)
    if filled <= ZERO:
        raise ValueError("no confirmed entry quantity")
    raw_entry = signal.get("actual_entry_price")
    if raw_entry is None and signal.get("entry_hit_at") is not None:
        raw_entry = signal.get("entry_price")
    entry = Decimal(str(raw_entry))
    leverage = Decimal(str(signal.get("leverage")))
    _finite(entry)
    _finite(leverage)
    if entry <= ZERO or leverage <= ZERO:
        raise ValueError("entry and leverage must be positive")
    fee_rate = fee_bps / Decimal("10000")
    slippage_rate = slippage_bps / Decimal("10000")
    legs = []
    closed = ZERO
    seen = set()
    for row in partial_exits:
        if row["id"] in seen:
            raise ValueError("duplicate partial exit evidence")
        seen.add(row["id"])
        quantity = Decimal(str(row["closed_position_pct"])) / Decimal("100")
        price = Decimal(str(row["advice_price"]))
        _finite(quantity)
        _finite(price)
        if quantity <= ZERO or price <= ZERO:
            raise ValueError("invalid partial exit evidence")
        closed += quantity
        legs.append(ExitLeg(quantity, price, quantity * price * fee_rate, quantity * price * slippage_rate))
    if closed > filled:
        raise ValueError("partial exits exceed filled quantity")
    if closed < filled:
        price = Decimal(str(signal.get("close_price")))
        _finite(price)
        if price <= ZERO:
            raise ValueError("terminal close price is required")
        remaining = filled - closed
        legs.append(ExitLeg(remaining, price, remaining * price * fee_rate, remaining * price * slippage_rate))
    trade = CompletedTrade(
        direction=signal["direction"],
        quantity=filled,
        entry_reference_price=entry,
        initial_margin=filled * entry / leverage,
        entry_fee=filled * entry * fee_rate,
        entry_slippage_cost=filled * entry * slippage_rate,
        funding_paid=funding_paid,
        exits=tuple(legs),
    )
    trade.net_return()  # Fail before persistence when any full-cost evidence is missing.
    return trade
