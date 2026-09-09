"""F108 single-signal observation replay through the actual lifecycle detector.

This does not run indicators or force-close a remaining position. The campaign
replayer must supply signal intents, rule exits and a complete candle manifest.
"""

from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal

from signal_lifecycle_kernel import Candle, SignalLifecycleKernel


def validate_limit_signal(signal: dict, candles: list[Candle]) -> dict:
    """Replay one freshly published, single-target limit signal at closed bars.

    Each observation occurs at candle.close_time + 1. Historical expiry therefore
    uses the replay clock, never wall clock. Same-bar ambiguity uses the exact
    runtime detector, including its current TP preference. Input is not mutated.
    """
    state = deepcopy(signal)
    if (
        state.get("entry_execution_mode") != "limit_only"
        or state.get("status") != "published"
        or state.get("direction") not in ("long", "short")
        or state.get("signal_type") != "crypto"
        or len(state.get("target_prices", [])) != 1
        or state.get("entry_hit_at") is not None
        or Decimal(str(state.get("filled_position_pct", 0))) != 0
    ):
        raise ValueError("replay requires a new single-target limit signal")
    created_at = state.get("created_at")
    if type(created_at) is not int or created_at < 0 or not candles or candles[0].open_time > created_at:
        raise ValueError("observation evidence must cover signal creation")
    prices = [
        Decimal(str(state.get("entry_price"))),
        Decimal(str(state.get("stop_loss"))),
        Decimal(str(state["target_prices"][0])),
    ]
    if any(not value.is_finite() or value <= 0 for value in prices):
        raise ValueError("invalid signal prices")
    entry, stop, take = prices
    if not (stop < entry < take if state["direction"] == "long" else take < entry < stop):
        raise ValueError("signal exit prices conflict with direction")
    previous_close = None
    for candle in candles:
        if candle.close_time - candle.open_time != 299 or (
            previous_close is not None and candle.open_time != previous_close + 1
        ):
            raise ValueError("observation candles must be contiguous closed 5m bars")
        values = (candle.open, candle.high, candle.low, candle.close)
        if any(not isinstance(v, Decimal) or not v.is_finite() or v <= 0 for v in values):
            raise ValueError("invalid observation price")
        if not candle.low <= min(candle.open, candle.close) <= max(candle.open, candle.close) <= candle.high:
            raise ValueError("invalid observation OHLC bounds")
        previous_close = candle.close_time
    return state


def advance_limit_signal(signal: dict, candle: Candle) -> dict:
    """Advance already validated state by one observation, without input mutation."""
    state = deepcopy(signal)
    if state.get("status") == "closed":
        return {"signal": state, "events": [], "terminal": True}
    events = []
    detected = SignalLifecycleKernel.detect_candle_events(state, [candle], now=candle.close_time + 1)
    for event in detected:
        events.append(asdict(event))
        if event.event_type == "entry_hit":
            state.update(
                status="active",
                lifecycle_status="entered",
                entry_hit_at=event.observed_at,
                actual_entry_price=event.observed_price,
                filled_position_pct=Decimal(100),
                remaining_position_pct=Decimal(100),
            )
        elif event.event_type in ("tp_hit", "sl_hit", "expired_unfilled"):
            lifecycle = {"tp_hit": "tp_full", "sl_hit": "stopped_out", "expired_unfilled": "expired_unfilled"}
            state.update(
                status="closed",
                lifecycle_status=lifecycle[event.event_type],
                closed_at=event.observed_at,
                close_price=event.observed_price,
                remaining_position_pct=Decimal(0),
            )
            if event.event_type == "tp_hit":
                state["tp_hit_count"] = 1
        else:
            raise ValueError("unexpected event for frozen single-target limit model")
    return {"signal": state, "events": events, "terminal": state.get("status") == "closed"}


def replay_limit_signal(signal: dict, candles: list[Candle]) -> dict:
    state = validate_limit_signal(signal, candles)
    events = []
    for candle in candles:
        if state.get("status") == "closed":
            break
        result = advance_limit_signal(state, candle)
        state = result["signal"]
        events.extend(result["events"])
    return {"signal": state, "events": events, "terminal": state.get("status") == "closed"}
