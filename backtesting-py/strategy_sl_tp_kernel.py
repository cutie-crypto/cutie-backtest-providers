"""Pure SL/TP price kernel shared by runtime and historical signal generation."""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Optional

from strategy_entry_evaluators import Bar


def align_initial_prices(*, direction, entry, stop, take, price_tick):
    """Use one frozen tick rule for signal creation and historical intents.

    Limit entry rounds away from paying more; protection and profit targets
    round toward earlier exits. Reject collapsed levels rather than fabricate
    a wider risk distance. No tick preserves the legacy price contract.
    """
    if price_tick is None:
        return entry, stop, take
    tick = Decimal(str(price_tick))
    if direction not in {"long", "short"} or not tick.is_finite() or tick <= 0:
        raise ValueError("invalid frozen price tick")
    long = direction == "long"
    entry_round = ROUND_FLOOR if long else ROUND_CEILING
    stop_round = ROUND_CEILING if long else ROUND_FLOOR
    values = [
        (value / tick).to_integral_value(rounding=rounding) * tick
        for value, rounding in ((entry, entry_round), (stop, stop_round), (take, entry_round))
    ]
    entry, stop, take = values
    if any(not value.is_finite() or value <= 0 for value in values) or not (
        stop < entry < take if long else take < entry < stop
    ):
        raise ValueError("price tick collapses initial order levels")
    return entry, stop, take


def atr_series(bars: list[Bar], period: int) -> list[Optional[float]]:
    """Wilder ATR：TR = max(H-L, |H-Cprev|, |L-Cprev|)，ATR 是 TR 的 Wilder 平滑
    （alpha=1/period, adjust=False，与 strategy_entry_evaluators._wilder_smooth 同公式）。
    首根无 prev close，TR[0] = H[0]-L[0]。
    """
    n = len(bars)
    if n == 0:
        return []
    tr: list[float] = [bars[0].high - bars[0].low]
    for i in range(1, n):
        prev_close = bars[i - 1].close
        tr.append(
            max(
                bars[i].high - bars[i].low,
                abs(bars[i].high - prev_close),
                abs(bars[i].low - prev_close),
            )
        )
    alpha = 1.0 / period
    out: list[Optional[float]] = [None] * n
    prev: Optional[float] = None
    for i, v in enumerate(tr):
        prev = v if prev is None else (alpha * v + (1 - alpha) * prev)
        out[i] = prev
    return out


def compute_leg(
    *, bars: list[Bar], direction: str, entry_price: Decimal, rule: Optional[dict], is_stop_loss: bool
) -> Optional[Decimal]:
    if not isinstance(rule, dict):
        return None
    rule_type = rule.get("type")
    try:
        if rule_type == "fixed_pct":
            pct = Decimal(str(rule.get("pct")))
            if pct <= 0:
                return None
            offset = entry_price * pct / Decimal("100")
        elif rule_type == "atr_multiplier":
            period = int(rule.get("period"))
            multiplier = Decimal(str(rule.get("multiplier")))
            if period < 2 or multiplier <= 0:
                return None
            atr = atr_series(bars, period)
            atr_value = atr[-1]
            if atr_value is None or len(bars) < period:
                return None
            offset = Decimal(str(atr_value)) * multiplier
        else:
            return None
    except (InvalidOperation, TypeError, ValueError, IndexError):
        return None

    if offset <= 0:
        return None

    # long：止损在入场价下方、止盈在上方；short 相反。
    is_long = direction == "long"
    below = is_long if is_stop_loss else not is_long
    price = entry_price - offset if below else entry_price + offset
    if price <= 0:
        return None
    return price
