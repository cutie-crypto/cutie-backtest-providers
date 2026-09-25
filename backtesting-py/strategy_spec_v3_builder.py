"""Deterministic ``cutie.strategy_spec.v3`` builder (SPEC_组合策略v3契约 §6.2).

Provider-side twin of the server ``strategy_spec_v3_builder.py``: the same
``(strategy_family, params, envelope)`` must produce byte-identical canonical
JSON on both sides (§0.2), so this module only applies §6.2 rules 1–8 and adds
no defaults of its own.  Parameter ranges follow the §6.1 tool table; the
output is still validated by ``compile_strategy_v3`` at execution time.
"""

from __future__ import annotations

import copy
from decimal import Decimal, InvalidOperation
from typing import Any

from canonical_json import CanonicalJsonError, canonical_decimal_str

STRATEGY_SPEC_V3_SCHEMA = "cutie.strategy_spec.v3"

_COMMON_PARAM_KEYS = {
    "legs",
    "leverage",
    "margin_per_leg",
    "basket_stop_loss_pct",
    "basket_take_profit_pct",
    "cooldown_bars",
    "time_exit_bars",
}
_FAMILY_PARAM_KEYS = {
    "basket_ratio_sma_cross": {"fast_window", "slow_window"},
    "basket_ratio_roc": {"roc_window", "entry_threshold", "exit_threshold"},
    "basket_ratio_zscore": {"zscore_window", "entry_z", "exit_z"},
}
_ENVELOPE_KEYS = {"timeframe", "fee_bps", "slippage_bps"}


class StrategySpecV3BuildError(ValueError):
    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path
        self.message = message


def _fail(path: str, message: str) -> None:
    raise StrategySpecV3BuildError(path, message)


def _int(value: Any, path: str, minimum: int, maximum: int | None = None) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        _fail(path, f"must be an integer in [{minimum}, {maximum}]")
    return value


def _decimal(value: Any, path: str) -> Decimal:
    """A canonical Decimal string param (§0.6); non-canonical input is refused
    rather than normalized so both builders agree on the accepted domain."""
    if not isinstance(value, str):
        _fail(path, "must be a canonical Decimal string")
    try:
        parsed = Decimal(value)
        canonical = canonical_decimal_str(value)
    except (ArithmeticError, CanonicalJsonError, InvalidOperation, ValueError):
        _fail(path, "invalid Decimal string")
    if canonical != value:
        _fail(path, "must be a canonical Decimal string")
    return parsed


def _open_unit(value: Any, path: str) -> str:
    parsed = _decimal(value, path)
    if not Decimal(0) < parsed < Decimal(1):
        _fail(path, "must be in (0, 1)")
    return value


def _literal(value: Decimal) -> dict[str, Any]:
    return {
        "node": "literal",
        "value_type": "decimal",
        "value": canonical_decimal_str(value),
    }


def _feature_ref(key: str, lag_bars: int = 0) -> dict[str, Any]:
    return {"node": "feature", "key": key, "lag_bars": lag_bars}


def _derived(key: str, expr: dict[str, Any], timeframe: str, value_kind: str) -> dict:
    return {
        "key": key,
        "kind": "derived",
        "expr": expr,
        "interval": timeframe,
        "value_kind": value_kind,
        "output_type": "decimal",
    }


def _primitive(key: str, primitive: str, window_bars: int, timeframe: str) -> dict:
    return {
        "key": key,
        "primitive": primitive,
        "primitive_version": "1",
        "source_stream": "feature:ratio",
        "interval": timeframe,
        "value_kind": "level",
        "output_type": "decimal",
        "params": {"window_bars": window_bars},
        "required": True,
    }


def _arithmetic(op: str, *args: dict[str, Any]) -> dict[str, Any]:
    return {"node": "arithmetic", "op": op, "args": list(args)}


def _compare(op: str, key: str, literal: Decimal) -> dict[str, Any]:
    return {
        "node": "compare",
        "op": op,
        "left": _feature_ref(key),
        "right": _literal(literal),
    }


def _cross(op: str) -> dict[str, Any]:
    return {
        "node": "cross",
        "op": op,
        "left": _feature_ref("ratio_sma_fast"),
        "right": _feature_ref("ratio_sma_slow"),
    }


def _family_parts(
    family: str, params: dict[str, Any], timeframe: str
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Rules 3–5: family features, entry condition and signal_exit."""
    if family == "basket_ratio_sma_cross":
        fast = _int(params["fast_window"], "$.params.fast_window", 2, 50)
        slow = _int(params["slow_window"], "$.params.slow_window", 5, 200)
        if not fast < slow:
            _fail("$.params.fast_window", "must be less than slow_window")
        features = [
            _primitive("ratio_sum_fast", "rolling_sum", fast, timeframe),
            _primitive("ratio_sum_slow", "rolling_sum", slow, timeframe),
            _derived(
                "ratio_sma_fast",
                _arithmetic("div", _feature_ref("ratio_sum_fast"), _literal(Decimal(fast))),
                timeframe,
                "level",
            ),
            _derived(
                "ratio_sma_slow",
                _arithmetic("div", _feature_ref("ratio_sum_slow"), _literal(Decimal(slow))),
                timeframe,
                "level",
            ),
        ]
        return features, _cross("crosses_above"), _cross("crosses_below")
    if family == "basket_ratio_roc":
        window = _int(params["roc_window"], "$.params.roc_window", 2, 100)
        entry = _open_unit(params["entry_threshold"], "$.params.entry_threshold")
        exit_ = _decimal(params["exit_threshold"], "$.params.exit_threshold")
        if not Decimal(0) <= exit_ < Decimal(entry):
            _fail("$.params.exit_threshold", "must be in [0, entry_threshold)")
        lagged = _feature_ref("ratio", window)
        features = [
            _derived(
                "ratio_roc",
                _arithmetic(
                    "div", _arithmetic("sub", _feature_ref("ratio"), lagged), lagged
                ),
                timeframe,
                "level",
            )
        ]
        return (
            features,
            _compare("gt", "ratio_roc", Decimal(entry)),
            _compare("lt", "ratio_roc", exit_),
        )
    window = _int(params["zscore_window"], "$.params.zscore_window", 10, 200)
    entry_z = _decimal(params["entry_z"], "$.params.entry_z")
    if not Decimal(0) < entry_z <= Decimal(5):
        _fail("$.params.entry_z", "must be in (0, 5]")
    exit_z = _decimal(params["exit_z"], "$.params.exit_z")
    if not Decimal(0) <= exit_z < entry_z:
        _fail("$.params.exit_z", "must be in [0, entry_z)")
    features = [_primitive("ratio_z", "rolling_zscore", window, timeframe)]
    return (
        features,
        _compare("lt", "ratio_z", -entry_z),
        _compare("gt", "ratio_z", -exit_z),
    )


def build_strategy_spec_v3(
    strategy_family: str, params: dict[str, Any], envelope: dict[str, Any]
) -> dict[str, Any]:
    """§6.2 rules 1–8: ``(strategy_family, tool params, envelope
    {timeframe, fee_bps, slippage_bps})`` → v3 spec dict (then canonical_json)."""
    if strategy_family not in _FAMILY_PARAM_KEYS:
        _fail("$.strategy_family", "unsupported basket strategy family")
    expected = _COMMON_PARAM_KEYS | _FAMILY_PARAM_KEYS[strategy_family]
    if not isinstance(params, dict) or set(params) != expected:
        _fail("$.params", f"exact keys required: {sorted(expected)}")
    if not isinstance(envelope, dict) or set(envelope) != _ENVELOPE_KEYS:
        _fail("$.envelope", f"exact keys required: {sorted(_ENVELOPE_KEYS)}")
    timeframe = envelope["timeframe"]
    if not isinstance(timeframe, str):
        _fail("$.envelope.timeframe", "must be a string")
    legs = params["legs"]
    if not isinstance(legs, list) or any(not isinstance(item, dict) for item in legs):
        _fail("$.params.legs", "must be an array of objects")
    if any(not isinstance(item.get("leg_id"), str) for item in legs):
        _fail("$.params.legs", "every leg needs a string leg_id")
    leverage = _int(params["leverage"], "$.params.leverage", 1, 3)
    margin = _decimal(params["margin_per_leg"], "$.params.margin_per_leg")
    if margin <= 0:
        _fail("$.params.margin_per_leg", "must be > 0")
    stop = _open_unit(params["basket_stop_loss_pct"], "$.params.basket_stop_loss_pct")
    take = _open_unit(params["basket_take_profit_pct"], "$.params.basket_take_profit_pct")
    cooldown = _int(params["cooldown_bars"], "$.params.cooldown_bars", 0)
    time_exit = params["time_exit_bars"]
    if time_exit is not None:
        time_exit = _int(time_exit, "$.params.time_exit_bars", 1)
    fee_bps = _decimal(envelope["fee_bps"], "$.envelope.fee_bps")
    slippage_bps = _decimal(envelope["slippage_bps"], "$.envelope.slippage_bps")

    family_features, entry_condition, signal_exit = _family_parts(
        strategy_family, params, timeframe
    )
    ratio = _derived(
        "ratio",
        _arithmetic(
            "div",
            {"node": "stream", "leg": "a", "field": "close", "lag_bars": 0},
            {"node": "stream", "leg": "b", "field": "close", "lag_bars": 0},
        ),
        timeframe,
        "price",
    )
    features = sorted(
        [ratio, *family_features], key=lambda item: item["key"].encode("utf-16-be")
    )
    return {
        "schema": STRATEGY_SPEC_V3_SCHEMA,
        "strategy_family": strategy_family,
        "market": {
            "market_type": "futures",
            "exchange": "binance",
            "timeframe": timeframe,
            "legs": sorted(copy.deepcopy(legs), key=lambda item: item["leg_id"]),
        },
        "parameters": [],
        "features": features,
        "entry": {
            "condition": entry_condition,
            "order_model": "next_bar_open",
            "cooldown_bars": cooldown,
        },
        "exit": {
            "stop_loss": {"model": "basket_pnl_pct", "value": stop},
            "take_profit": {"model": "basket_pnl_pct", "value": take},
            "time_exit_bars": time_exit,
            "signal_exit": signal_exit,
        },
        "risk": {
            "position_sizing": {
                "model": "fixed_margin_per_leg",
                "value": canonical_decimal_str(margin),
            },
            "max_open_positions": 1,
            "allow_pyramiding": False,
            "leverage": str(leverage),
        },
        "execution": {
            "decision_clock": "closed_bar",
            "signal_effective_at": "next_bar_open",
            "intrabar_priority": ["stop_loss", "take_profit", "time_exit", "signal_exit"],
            "position_mode": "one_way",
            "cost_model": {
                "schema": "cutie.execution_cost.v1",
                "fee_bps": canonical_decimal_str(fee_bps),
                "slippage_bps": canonical_decimal_str(slippage_bps),
                "funding": "excluded",
            },
            "missing_data_policy": "skip_unaligned_bar",
            "kernel_api_version": "1",
        },
    }


__all__ = [
    "STRATEGY_SPEC_V3_SCHEMA",
    "StrategySpecV3BuildError",
    "build_strategy_spec_v3",
]
