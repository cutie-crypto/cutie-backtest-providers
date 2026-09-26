"""123 组合策略 B1b: provider ``strategy_spec_v3_builder`` (SPEC_组合策略v3契约 §6.2)."""

from __future__ import annotations

import hashlib
import json
import sys
from decimal import ROUND_DOWN, localcontext
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from canonical_json import canonical_json  # noqa: E402
from strategy_kernel import compile_strategy_v3  # noqa: E402
from strategy_spec_v3_builder import (  # noqa: E402
    StrategySpecV3BuildError,
    build_strategy_spec_v3,
)

# SPEC_组合策略v3契约 §2.7 example spec, copied verbatim from the SPEC
# (dev 22c9ec0bc, take_profit already corrected to "0.1" by §2.6.1).
SECTION_2_7_SPEC = r"""
{
  "schema": "cutie.strategy_spec.v3",
  "strategy_family": "basket_ratio_sma_cross",
  "market": {
    "market_type": "futures", "exchange": "binance", "timeframe": "4h",
    "legs": [
      {"leg_id": "a", "symbol": "ETHUSDT", "side": "long",  "weight": "1"},
      {"leg_id": "b", "symbol": "BTCUSDT", "side": "short", "weight": "1"}
    ]
  },
  "parameters": [],
  "features": [
    {"key": "ratio", "kind": "derived",
     "expr": {"node":"arithmetic","op":"div","args":[
       {"node":"stream","leg":"a","field":"close","lag_bars":0},
       {"node":"stream","leg":"b","field":"close","lag_bars":0}]},
     "interval": "4h", "value_kind": "price", "output_type": "decimal"},
    {"key": "ratio_sma_fast", "kind": "derived",
     "expr": {"node":"arithmetic","op":"div","args":[
       {"node":"feature","key":"ratio_sum_fast","lag_bars":0},
       {"node":"literal","value_type":"decimal","value":"5"}]},
     "interval": "4h", "value_kind": "level", "output_type": "decimal"},
    {"key": "ratio_sma_slow", "kind": "derived",
     "expr": {"node":"arithmetic","op":"div","args":[
       {"node":"feature","key":"ratio_sum_slow","lag_bars":0},
       {"node":"literal","value_type":"decimal","value":"20"}]},
     "interval": "4h", "value_kind": "level", "output_type": "decimal"},
    {"key": "ratio_sum_fast", "primitive": "rolling_sum", "primitive_version": "1",
     "source_stream": "feature:ratio", "interval": "4h", "value_kind": "level",
     "output_type": "decimal", "params": {"window_bars": 5}, "required": true},
    {"key": "ratio_sum_slow", "primitive": "rolling_sum", "primitive_version": "1",
     "source_stream": "feature:ratio", "interval": "4h", "value_kind": "level",
     "output_type": "decimal", "params": {"window_bars": 20}, "required": true}
  ],
  "entry": {
    "condition": {"node":"cross","op":"crosses_above",
      "left":{"node":"feature","key":"ratio_sma_fast","lag_bars":0},
      "right":{"node":"feature","key":"ratio_sma_slow","lag_bars":0}},
    "order_model": "next_bar_open",
    "cooldown_bars": 6
  },
  "exit": {
    "stop_loss":   {"model": "basket_pnl_pct", "value": "0.05"},
    "take_profit": {"model": "basket_pnl_pct", "value": "0.1"},
    "time_exit_bars": null,
    "signal_exit": {"node":"cross","op":"crosses_below",
      "left":{"node":"feature","key":"ratio_sma_fast","lag_bars":0},
      "right":{"node":"feature","key":"ratio_sma_slow","lag_bars":0}}
  },
  "risk": {
    "position_sizing": {"model": "fixed_margin_per_leg", "value": "1000"},
    "max_open_positions": 1,
    "allow_pyramiding": false,
    "leverage": "3"
  },
  "execution": {
    "decision_clock": "closed_bar",
    "signal_effective_at": "next_bar_open",
    "intrabar_priority": ["stop_loss","take_profit","time_exit","signal_exit"],
    "position_mode": "one_way",
    "cost_model": {"schema":"cutie.execution_cost.v1","fee_bps":"10","slippage_bps":"5","funding":"excluded"},
    "missing_data_policy": "skip_unaligned_bar",
    "kernel_api_version": "1"
  }
}
"""

LEGS = [
    {"leg_id": "a", "symbol": "ETHUSDT", "side": "long", "weight": "1"},
    {"leg_id": "b", "symbol": "BTCUSDT", "side": "short", "weight": "1"},
]
ENVELOPE = {"timeframe": "4h", "fee_bps": "10", "slippage_bps": "5"}


def common(**changes) -> dict:
    params = {
        "legs": [dict(item) for item in LEGS],
        "leverage": 3,
        "margin_per_leg": "1000",
        "basket_stop_loss_pct": "0.05",
        "basket_take_profit_pct": "0.1",
        "cooldown_bars": 6,
        "time_exit_bars": None,
    }
    params.update(changes)
    return params


def sha(spec: dict) -> str:
    return hashlib.sha256(canonical_json(spec).encode("utf-8")).hexdigest()


def test_sma_cross_builder_is_byte_equal_to_section_2_7_example():
    expected = json.loads(SECTION_2_7_SPEC)
    built = build_strategy_spec_v3(
        "basket_ratio_sma_cross", common(fast_window=5, slow_window=20), ENVELOPE
    )
    assert canonical_json(built) == canonical_json(expected)
    assert sha(built) == sha(expected)
    assert [item["key"] for item in built["features"]] == [
        "ratio",
        "ratio_sma_fast",
        "ratio_sma_slow",
        "ratio_sum_fast",
        "ratio_sum_slow",
    ]
    assert compile_strategy_v3(built).spec_hash == sha(expected)


def test_sma_cross_compiles_with_its_windows_as_canonical_literals():
    built = build_strategy_spec_v3(
        "basket_ratio_sma_cross",
        common(fast_window=10, slow_window=40, time_exit_bars=12, leverage=1),
        {"timeframe": "1h", "fee_bps": "4", "slippage_bps": "2.5"},
    )
    plan = compile_strategy_v3(built)
    features = {item["key"]: item for item in built["features"]}
    assert features["ratio_sma_fast"]["expr"]["args"][1]["value"] == "10"
    assert features["ratio_sum_slow"]["params"] == {"window_bars": 40}
    assert {item["interval"] for item in built["features"]} == {"1h"}
    assert built["exit"]["time_exit_bars"] == 12
    assert built["risk"]["leverage"] == "1"
    assert built["execution"]["cost_model"]["slippage_bps"] == "2.5"
    assert plan.basket_key == "ETHUSDT~BTCUSDT"


def test_roc_builder_compiles_with_lagged_ratio_and_threshold_literals():
    built = build_strategy_spec_v3(
        "basket_ratio_roc",
        common(roc_window=12, entry_threshold="0.03", exit_threshold="0"),
        ENVELOPE,
    )
    compile_strategy_v3(built)
    assert [item["key"] for item in built["features"]] == ["ratio", "ratio_roc"]
    lagged = {"node": "feature", "key": "ratio", "lag_bars": 12}
    assert built["features"][1]["expr"] == {
        "node": "arithmetic",
        "op": "div",
        "args": [
            {
                "node": "arithmetic",
                "op": "sub",
                "args": [{"node": "feature", "key": "ratio", "lag_bars": 0}, lagged],
            },
            lagged,
        ],
    }
    assert built["entry"]["condition"]["op"] == "gt"
    assert built["entry"]["condition"]["right"]["value"] == "0.03"
    assert built["exit"]["signal_exit"]["op"] == "lt"
    assert built["exit"]["signal_exit"]["right"]["value"] == "0"


def test_zscore_builder_compiles_with_negated_canonical_literals():
    built = build_strategy_spec_v3(
        "basket_ratio_zscore", common(zscore_window=30, entry_z="2", exit_z="0"), ENVELOPE
    )
    compile_strategy_v3(built)
    [ratio, ratio_z] = built["features"]
    assert ratio["key"] == "ratio" and ratio["value_kind"] == "price"
    assert ratio_z["primitive"] == "rolling_zscore"
    assert ratio_z["source_stream"] == "feature:ratio"
    assert ratio_z["params"] == {"window_bars": 30}
    assert ratio_z["value_kind"] == "level" and ratio_z["required"] is True
    assert built["entry"]["condition"]["op"] == "lt"
    assert built["entry"]["condition"]["right"]["value"] == "-2"
    # -0 is not canonical (§0.6 / rule 8): exit_z=0 negates to "0".
    assert built["exit"]["signal_exit"]["op"] == "gt"
    assert built["exit"]["signal_exit"]["right"]["value"] == "0"


def test_builder_is_deterministic_and_sorts_legs_by_leg_id():
    params = common(zscore_window=20, entry_z="1.5", exit_z="0.5")
    first = build_strategy_spec_v3("basket_ratio_zscore", params, ENVELOPE)
    params["legs"] = list(reversed(params["legs"]))
    second = build_strategy_spec_v3("basket_ratio_zscore", params, ENVELOPE)
    assert canonical_json(first) == canonical_json(second)
    assert [leg["leg_id"] for leg in second["market"]["legs"]] == ["a", "b"]


@pytest.mark.parametrize(
    "family, params, envelope",
    [
        ("basket_ratio_pairs", common(fast_window=5, slow_window=20), ENVELOPE),
        ("basket_ratio_sma_cross", common(fast_window=5, slow_window=20, extra=1), ENVELOPE),
        ("basket_ratio_sma_cross", common(fast_window=20, slow_window=20), ENVELOPE),
        (
            "basket_ratio_sma_cross",
            common(fast_window=5, slow_window=20, basket_take_profit_pct="0.10"),
            ENVELOPE,
        ),
        ("basket_ratio_sma_cross", common(fast_window=5, slow_window=20, leverage=4), ENVELOPE),
        (
            "basket_ratio_roc",
            common(roc_window=5, entry_threshold="0.02", exit_threshold="0.02"),
            ENVELOPE,
        ),
        ("basket_ratio_zscore", common(zscore_window=20, entry_z="5.5", exit_z="0"), ENVELOPE),
        (
            "basket_ratio_sma_cross",
            common(fast_window=5, slow_window=20),
            {**ENVELOPE, "fee_bps": "10.0"},
        ),
    ],
)
def test_builder_rejects_out_of_contract_params(family, params, envelope):
    with pytest.raises(StrategySpecV3BuildError):
        build_strategy_spec_v3(family, params, envelope)


def test_29_digit_canonical_literal_is_accepted_under_any_caller_context():
    # §0.6 / cutie.decimal128.v1: the canonical check runs at 34 digits, so a
    # 29-digit canonical value is kept byte-for-byte whatever the caller's
    # context is.
    threshold = "0.12345678901234567890123456789"
    params = common(roc_window=3, entry_threshold=threshold, exit_threshold="0.01")
    built = build_strategy_spec_v3("basket_ratio_roc", params, ENVELOPE)
    assert threshold in canonical_json(built)
    compile_strategy_v3(built)
    with localcontext() as ctx:
        ctx.prec = 10
        ctx.rounding = ROUND_DOWN
        assert canonical_json(
            build_strategy_spec_v3("basket_ratio_roc", params, ENVELOPE)
        ) == canonical_json(built)
