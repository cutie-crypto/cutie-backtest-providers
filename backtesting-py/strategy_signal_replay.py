"""F108 single-signal observation replay through the actual lifecycle detector.

This does not run indicators or force-close a remaining position. The campaign
replayer must supply signal intents, rule exits and a complete candle manifest.
"""

from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal

from signal_lifecycle_kernel import Candle, SignalLifecycleKernel
from strategy_dynamic_stop import StopRules, advance_stop, initial_stop_state


def validate_limit_signal(signal: dict, candles: list[Candle]) -> dict:
    """Replay one freshly published, single-target limit signal at closed bars.

    Each observation occurs at candle.close_time + 1. Historical expiry therefore
    uses the replay clock, never wall clock. Same-bar ambiguity uses the exact
    runtime detector, including its current TP preference. Input is not mutated.
    """
    validate_observation_candles(candles)
    return validate_new_limit_signal(signal, candles[0].open_time)


def validate_new_limit_signal(signal: dict, observation_start: int) -> dict:
    """Validate a new intent against an already validated observation range."""
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
    if type(created_at) is not int or created_at < 0 or observation_start > created_at:
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
    return state


def validate_observation_candles(candles: list[Candle]) -> None:
    if not candles:
        raise ValueError("observation evidence must cover signal creation")
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


def replay_managed_limit_signal(
    signal: dict, candles: list[Candle], *, rules: StopRules, management_candles: list[Candle]
) -> dict:
    """Replay managed stops with separate closed strategy bars and 5m execution.

    Each management bar must be exactly reconstructible from the observations;
    this prevents different price sources or timeframes from silently changing
    the stop. The observation at its close is processed with the OLD stop first.
    """
    state = validate_limit_signal(signal, candles)
    management = validate_management_candles(candles, management_candles)
    stop_state = None
    events, updates = [], []
    for candle in candles:
        if state.get("status") == "closed":
            break
        result = advance_limit_signal(state, candle)
        state = result["signal"]
        events.extend(result["events"])
        if state.get("status") != "active":
            continue
        bar = management.get(candle.close_time)
        if bar is not None:
            state, stop_state, update = apply_managed_stop(state, stop_state, rules, bar)
            if update is not None:
                updates.append(update)
    return {
        "signal": state,
        "events": events,
        "terminal": state.get("status") == "closed",
        "stop_updates": updates,
        "stop_state": asdict(stop_state) if stop_state else None,
    }


def validate_management_candles(observations: list[Candle], bars: list[Candle]) -> dict[int, Candle]:
    if not bars:
        raise ValueError("closed strategy bars required for stop management")
    by_open = {bar.open_time: index for index, bar in enumerate(observations)}
    period = bars[0].close_time + 1 - bars[0].open_time
    if period < 300 or period % 300:
        raise ValueError("management timeframe must be a multiple of 5m")
    previous = None
    for bar in bars:
        if bar.close_time + 1 - bar.open_time != period or (previous is not None and bar.open_time != previous + 1):
            raise ValueError("management bars must be contiguous")
        index = by_open.get(bar.open_time)
        if index is None:
            raise ValueError("management bar lacks observation coverage")
        parts = observations[index : index + period // 300]
        if not parts or parts[-1].close_time != bar.close_time:
            raise ValueError("management bar lacks observation coverage")
        expected = (parts[0].open, max(part.high for part in parts), min(part.low for part in parts), parts[-1].close)
        if (bar.open, bar.high, bar.low, bar.close) != expected:
            raise ValueError("management OHLC differs from execution observations")
        previous = bar.close_time
    # Partial boundary bars may be absent, but no complete interior bar may be
    # omitted; otherwise later updates could rely on an incomplete extreme.
    if (
        bars[0].open_time - observations[0].open_time >= period
        or observations[-1].close_time - bars[-1].close_time >= period
    ):
        raise ValueError("management bars do not cover the observation range")
    return {bar.close_time: bar for bar in bars}


def apply_managed_stop(signal: dict, stop_state, rules: StopRules, bar: Candle):
    """Shared post-observation step for single-signal and campaign replay."""
    state = deepcopy(signal)
    if state.get("status") != "active":
        return state, stop_state, None
    if stop_state is None:
        stop_state = initial_stop_state(
            direction=state["direction"],
            entry_price=Decimal(str(state["entry_price"])),
            initial_stop=Decimal(str(state["stop_loss"])),
            entry_at=state["entry_hit_at"],
        )
    previous = stop_state
    stop_state = advance_stop(
        stop_state, rules, open_at=bar.open_time, close_at=bar.close_time, high=bar.high, low=bar.low, close=bar.close
    )
    update = None
    if stop_state.effective_stop != previous.effective_stop:
        update = {
            "observed_at": bar.close_time,
            "effective_from": bar.close_time + 1,
            "previous_stop": previous.effective_stop,
            "stop_loss": stop_state.effective_stop,
            "breakeven_active": stop_state.breakeven_active,
        }
        state["stop_loss"] = stop_state.effective_stop
    return state, stop_state, update


def managed_cycle_options(policy: dict, candles: list[Candle], step: int) -> dict:
    """Derive complete UTC strategy bars from the exact execution observations."""
    management = policy.get("stop_management")
    if management is None:
        return {}
    if type(step) is not int or step < 300 or step % 300:
        raise ValueError("invalid managed strategy timeframe")
    validate_observation_candles(candles)
    bars = []
    width = step // 300
    for i, candle in enumerate(candles):
        if candle.open_time % step or i + width > len(candles):
            continue
        parts = candles[i : i + width]
        bars.append(
            Candle(
                parts[0].open_time,
                parts[-1].close_time,
                parts[0].open,
                max(c.high for c in parts),
                min(c.low for c in parts),
                parts[-1].close,
            )
        )
    pct = management["trailing_pct"]
    return {
        "stop_rules": StopRules(None if pct is None else Decimal(pct), management["breakeven"]),
        "management_candles": bars,
    }
