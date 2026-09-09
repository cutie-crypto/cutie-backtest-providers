"""Generate historical signal intents from runtime indicator and pricing kernels."""

from __future__ import annotations

import math
from decimal import Decimal

from strategy_entry_evaluators import evaluate_entry, evaluate_exit, required_warmup_bars
from strategy_execution_policy import validate_execution_policy
from strategy_sl_tp_kernel import compute_leg


def generate_intents(*, bars, evaluator, params, direction, leverage, symbol, execution_policy, start_at):
    if evaluator == "cci_rsi" and params.get("direction", "both") != direction:
        raise ValueError("CCI+RSI requires an explicit matching single-side backtest")
    if evaluator == "breakout" and params.get("direction", "long") != direction:
        raise ValueError("Donchian direction differs from the requested single-side backtest")
    policy = validate_execution_policy(execution_policy)
    history_bars = policy["indicator_history_bars"]
    warmup = required_warmup_bars(evaluator, params)
    if warmup is None or type(history_bars) is not int or history_bars < warmup:
        raise ValueError("unsupported evaluator or insufficient frozen history")
    for rule in policy["sl_tp_rule"].values():
        if rule["type"] == "atr_multiplier" and history_bars < rule["period"]:
            raise ValueError("frozen history does not cover ATR")
    if (
        direction not in ("long", "short")
        or type(leverage) is not int
        or not 1 <= leverage <= (5 if direction == "long" else 3)
    ):
        raise ValueError("invalid intent direction/leverage")
    if not bars or type(start_at) is not int:
        raise ValueError("historical signal bars required")
    step = bars[0].close_time - bars[0].open_time
    previous = None
    for bar in bars:
        if step <= 0 or bar.close_time - bar.open_time != step or (previous is not None and bar.open_time != previous):
            raise ValueError("signal bars must be contiguous")
        if any(not math.isfinite(v) or v <= 0 for v in (bar.open, bar.high, bar.low, bar.close)):
            raise ValueError("invalid signal bar price")
        if not bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high:
            raise ValueError("invalid signal OHLC bounds")
        previous = bar.close_time
    initialized = False
    last_entry = last_exit = False
    intents = []
    for i, bar in enumerate(bars):
        if bar.close_time < start_at:
            continue
        window = bars[max(0, i + 1 - history_bars) : i + 1]
        if len(window) < history_bars:
            raise ValueError("missing frozen indicator prehistory")
        entry = evaluate_entry(evaluator, params, window, direction) is not None
        exit_hit = evaluate_exit(evaluator, params, window, direction) is not None
        at = bar.close_time + policy["evaluation_lag_seconds"]
        if initialized:
            if exit_hit and not last_exit:
                intents.append({"id": f"exit-{bar.close_time}", "kind": "rule_exit", "at": at, "price": str(bar.close)})
            if entry and not last_entry:
                reference = Decimal(str(bar.close))
                stop = compute_leg(
                    bars=window,
                    direction=direction,
                    entry_price=reference,
                    rule=policy["sl_tp_rule"]["stop_loss"],
                    is_stop_loss=True,
                )
                take = compute_leg(
                    bars=window,
                    direction=direction,
                    entry_price=reference,
                    rule=policy["sl_tp_rule"]["take_profit"],
                    is_stop_loss=False,
                )
                if stop is None or take is None or not stop.is_finite() or not take.is_finite():
                    raise ValueError("cannot price frozen exit rules")
                sid = str(bar.close_time)
                intents.append(
                    {
                        "id": f"entry-{sid}",
                        "kind": "entry",
                        "at": at,
                        "signal": {
                            "id": sid,
                            "signal_type": "crypto",
                            "symbol": symbol,
                            "direction": direction,
                            "leverage": leverage,
                            "status": "published",
                            "lifecycle_status": "awaiting_entry",
                            "entry_execution_mode": "limit_only",
                            "created_at": at,
                            "entry_price": reference,
                            "create_snapshot_price": reference,
                            "stop_loss": stop,
                            "target_prices": [take],
                            "filled_position_pct": Decimal(0),
                            "remaining_position_pct": Decimal(0),
                            "tp_hit_count": 0,
                        },
                    }
                )
        initialized, last_entry, last_exit = True, entry, exit_hit
    return intents
