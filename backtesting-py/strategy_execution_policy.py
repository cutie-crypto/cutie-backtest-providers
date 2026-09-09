"""F108 frozen execution profile for the upcoming risk-aware replay/runtime.

An explicit profile is required: neither current ticker pricing nor the legacy
provider's next-open market fills may be silently presented as this model.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from canonical_json import canonical_decimal_str, canonical_json_sha256

SCHEMA = "cutie.strategy_signal_execution.v1"


def validate_execution_policy(raw: object) -> dict:
    if not isinstance(raw, dict) or set(raw) != {
        "schema",
        "reference_price",
        "entry_mode",
        "observation_timeframe",
        "evaluation_lag_seconds",
        "indicator_history_bars",
        "sl_tp_rule",
    }:
        raise ValueError("execution policy requires the complete frozen key set")
    if (
        raw["schema"] != SCHEMA
        or raw["reference_price"] != "signal_bar_close"
        or raw["entry_mode"] != "limit_only"
        or raw["observation_timeframe"] != "5m"
        or type(raw["evaluation_lag_seconds"]) is not int
        or raw["evaluation_lag_seconds"] != 30
    ):
        raise ValueError("unsupported execution model")
    if type(raw["indicator_history_bars"]) is not int or raw["indicator_history_bars"] < 2:
        raise ValueError("indicator history requires an explicit integer window")
    rules = raw["sl_tp_rule"]
    if not isinstance(rules, dict) or set(rules) != {"stop_loss", "take_profit"}:
        raise ValueError("both exit rules must be frozen")
    normalized = {}
    for name, rule in rules.items():
        if not isinstance(rule, dict):
            raise ValueError("exit rule must be an object")
        kind = rule.get("type")
        if kind == "fixed_pct":
            if set(rule) != {"type", "pct"}:
                raise ValueError("unexpected percentage rule fields")
            normalized[name] = {"type": kind, "pct": _positive(rule["pct"])}
        elif kind == "atr_multiplier":
            if set(rule) != {"type", "period", "multiplier"} or type(rule["period"]) is not int or rule["period"] < 2:
                raise ValueError("invalid ATR rule")
            normalized[name] = {"type": kind, "period": rule["period"], "multiplier": _positive(rule["multiplier"])}
        else:
            raise ValueError("unsupported exit rule")
    return {**raw, "sl_tp_rule": normalized}


def _positive(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("invalid exit rule number")
    try:
        decimal = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid exit rule number") from exc
    if not decimal.is_finite() or decimal <= 0:
        raise ValueError("exit rule number outside price-safe range")
    return canonical_decimal_str(decimal)


def execution_policy_hash(raw: object) -> str:
    return canonical_json_sha256(validate_execution_policy(raw))


def require_matching_execution_policy(backtested: object, requested: object) -> dict:
    """Canonical equality, including pricing model, not only SL/TP percentages."""
    policy = validate_execution_policy(backtested)
    if canonical_json_sha256(policy) != execution_policy_hash(requested):
        raise ValueError("execution configuration changed; a new backtest is required")
    return policy


def requested_signal_execution(raw: object, risk_policy: object) -> dict | None:
    """Normalize the optional request before snapshotting, hashing and dispatch."""
    if raw is None:
        return None
    if risk_policy is None:
        raise ValueError("signal_execution requires risk_policy")
    if not isinstance(raw, dict) or set(raw) != {"execution_policy", "cycle_limits"}:
        raise ValueError("signal_execution requires execution_policy and cycle_limits")
    limits = raw["cycle_limits"]
    if not isinstance(limits, dict) or set(limits) != {
        "daily_limit",
        "author_daily_limit",
        "cooldown_seconds",
        "day_offset_seconds",
    }:
        raise ValueError("signal_execution requires complete cycle limits")
    if any(type(value) is not int for value in limits.values()):
        raise ValueError("signal_execution limits require integers")
    if not 1 <= limits["daily_limit"] <= 10 or limits["author_daily_limit"] < 1 or limits["cooldown_seconds"] < 0:
        raise ValueError("signal_execution limits outside supported range")
    if not -86400 < limits["day_offset_seconds"] < 86400:
        raise ValueError("signal_execution day offset outside supported range")
    return {"execution_policy": validate_execution_policy(raw["execution_policy"]), "cycle_limits": dict(limits)}
