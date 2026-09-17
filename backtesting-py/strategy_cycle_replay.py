"""Chronological F108 cycle replay from priced, edge-qualified signal intents.

Indicator evaluation produces intents separately. This layer models one strategy;
other strategies' author-quota consumption must be supplied as external entries.
No end-of-data liquidation or automatic risk-cycle restoration is performed.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from decimal import Decimal

from funding_history import funding_paid
from strategy_risk_accounting import RiskState, completed_signal_trade, settle_risk
from strategy_signal_replay import (
    advance_limit_signal,
    apply_managed_stop,
    validate_management_candles,
    validate_new_limit_signal,
    validate_observation_candles,
)
from canonical_json import canonical_decimal_str


def replay_cycle(
    *,
    intents,
    candles,
    fee_bps,
    slippage_bps,
    funding_history=None,
    market,
    symbol,
    direction,
    leverage,
    daily_limit,
    author_daily_limit,
    cooldown_seconds,
    day_offset_seconds,
    external_entry_times=(),
    stop_rules=None,
    management_candles=None,
    observation_source=None,
):
    validate_observation_candles(candles)
    if stop_rules is not None:
        # Evidence normalization removes decimal exponents during JSON transport.
        # Canonicalize before detection so nested string evidence also round-trips.
        def canonical_bars(values):
            return [
                replace(
                    bar,
                    **{
                        key: Decimal(canonical_decimal_str(getattr(bar, key)))
                        for key in ("open", "high", "low", "close")
                    },
                )
                for bar in values
            ]

        candles = canonical_bars(candles)
        if management_candles is not None:
            management_candles = canonical_bars(management_candles)
    timeline = _validated_timeline(
        intents,
        candles,
        market,
        symbol,
        direction,
        leverage,
        daily_limit,
        author_daily_limit,
        cooldown_seconds,
        day_offset_seconds,
        funding_history,
    )
    if (stop_rules is None) != (management_candles is None):
        raise ValueError("stop rules require complete strategy bar evidence")
    if stop_rules is not None:
        from strategy_dynamic_stop import StopRules

        if not isinstance(stop_rules, StopRules):
            raise ValueError("invalid stop rules")
        management = validate_management_candles(candles, management_candles)
        timeline.extend((bar.close_time + 1, 0.5, i, bar) for i, bar in enumerate(management.values()))
    stop_state = None
    stop_updates = []
    state, active, last_entry = RiskState(), None, None
    strategy_days, author_days = {}, {}

    def day(at):
        return (at + day_offset_seconds) // 86400

    for at in external_entry_times:
        if type(at) is not int:
            raise ValueError("author quota timestamps require integers")
        timeline.append((at, 1, 0, None))
    outcomes, settlements, event_log = [], [], []
    for at, kind, _, item in sorted(timeline, key=lambda row: row[:3]):
        if kind == 0:
            if active is None:
                continue
            observation = advance_limit_signal(active, item, observation_source=observation_source)
            active = observation["signal"]
            event_log.extend(observation["events"])
        elif kind == 0.5:
            if active is not None and active.get("status") == "active":
                active, stop_state, update = apply_managed_stop(active, stop_state, stop_rules, item)
                if update is not None:
                    stop_updates.append({"signal_id": str(active["id"]), **update})
            continue
        elif kind == 1:
            author_days[day(at)] = author_days.get(day(at), 0) + 1
            continue
        elif item["kind"] == "rule_exit":
            if active is None or active.get("status") != "active":
                outcomes.append({"intent_id": item["id"], "outcome": "no_position"})
                continue
            price = Decimal(str(item.get("price")))
            if not price.is_finite() or price <= 0:
                raise ValueError("invalid rule exit price")
            active.update(
                status="closed",
                lifecycle_status="strategy_exited",
                closed_at=at,
                close_price=price,
                remaining_position_pct=Decimal(0),
            )
            outcomes.append({"intent_id": item["id"], "outcome": "rule_exit"})
        else:
            reason = (
                "open_signal"
                if active
                else (
                    "risk_paused"
                    if state.entries_paused
                    else (
                        "daily_limit_reached"
                        if strategy_days.get(day(at), 0) >= daily_limit
                        else (
                            "author_daily_limit_reached"
                            if author_days.get(day(at), 0) >= author_daily_limit
                            else "cooldown" if last_entry is not None and at - last_entry < cooldown_seconds else None
                        )
                    )
                )
            )
            if reason:
                outcomes.append({"intent_id": item["id"], "outcome": reason})
                continue
            active = validate_new_limit_signal(item["signal"], candles[0].open_time)
            if active["created_at"] != at:
                raise ValueError("intent time differs from signal publication")
            last_entry = at
            strategy_days[day(at)] = strategy_days.get(day(at), 0) + 1
            author_days[day(at)] = author_days.get(day(at), 0) + 1
            outcomes.append({"intent_id": item["id"], "outcome": "published"})
            continue
        if active is None or active.get("status") != "closed":
            continue
        if active.get("lifecycle_status") != "expired_unfilled":
            paid = (
                Decimal(0)
                if market == "spot"
                else funding_paid(
                    funding_history,
                    [
                        {
                            "entry_ms": active["entry_hit_at"] * 1000,
                            "exit_ms": active["closed_at"] * 1000,
                            "quantity": "1",
                        }
                    ],
                    active["direction"],
                )
            )
            trade = completed_signal_trade(active, [], fee_bps=fee_bps, slippage_bps=slippage_bps, funding_paid=paid)
            result = settle_risk(state, trade)
            state = result.state
            settlements.append(
                {
                    "signal_id": str(active["id"]),
                    "net_return": str(trade.net_return()),
                    "state": asdict(state),
                    "closed_at": active["closed_at"],
                }
            )
        active = None
        stop_state = None
    result = {
        "outcomes": outcomes,
        "settlements": settlements,
        "events": event_log,
        "risk_state": asdict(state),
        "open_signal": active,
    }
    if stop_rules is not None:
        result["stop_updates"] = stop_updates
        result["stop_state"] = asdict(stop_state) if stop_state else None
    return result


def _validated_timeline(
    intents,
    candles,
    market,
    symbol,
    direction,
    leverage,
    daily_limit,
    author_daily_limit,
    cooldown_seconds,
    day_offset_seconds,
    funding_history,
):
    if not candles or not 1 <= daily_limit <= 10 or author_daily_limit < 1 or cooldown_seconds < 0:
        raise ValueError("invalid cycle replay configuration")
    if market not in ("spot", "futures") or (market == "futures" and funding_history is None):
        raise ValueError("complete market cost evidence required")
    if any(type(v) is not int for v in (daily_limit, author_daily_limit, cooldown_seconds, day_offset_seconds)):
        raise ValueError("cycle limits require integers")
    if (
        direction not in ("long", "short")
        or type(leverage) is not int
        or not 1 <= leverage <= (5 if direction == "long" else 3)
    ):
        raise ValueError("invalid cycle direction/leverage")
    if market == "spot" and (direction != "long" or leverage != 1):
        raise ValueError("spot cycle requires unleveraged long")
    if not isinstance(symbol, str) or not symbol:
        raise ValueError("cycle symbol is required")
    if funding_history is not None and funding_history.get("symbol") != symbol.replace("/", "").split(":")[0]:
        raise ValueError("funding symbol differs from cycle")
    timeline = [(c.close_time + 1, 0, i, c) for i, c in enumerate(candles)]
    seen = set()
    signal_ids = set()
    for index, intent in enumerate(intents):
        if type(intent.get("at")) is not int or intent.get("kind") not in ("entry", "rule_exit"):
            raise ValueError("invalid signal intent")
        if not candles[0].open_time <= intent["at"] <= candles[-1].close_time + 1:
            raise ValueError("intent outside observation coverage")
        identity = intent.get("id")
        if not isinstance(identity, str) or not identity or identity in seen:
            raise ValueError("intent IDs must be unique strings")
        seen.add(identity)
        if intent["kind"] == "entry":
            sid = intent["signal"].get("id")
            if not isinstance(sid, str) or not sid or sid in signal_ids:
                raise ValueError("entry signal IDs must be unique strings")
            signal_ids.add(sid)
        timeline.append((intent["at"], 2, index, intent))
        if intent["kind"] == "entry" and (
            intent["signal"].get("symbol") != symbol
            or intent["signal"].get("direction") != direction
            or intent["signal"].get("leverage") != leverage
        ):
            raise ValueError("intent differs from cycle direction/leverage")
    return timeline
