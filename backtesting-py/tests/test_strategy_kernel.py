"""62-2a compiler/kernel/capability conformance tests."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cutie_backtesting_provider as provider  # noqa: E402
from canonical_json import canonical_json_sha256  # noqa: E402
from strategy_kernel import (  # noqa: E402
    COMPILER_TOOL_ID,
    ERR_CAPABILITY_MISMATCH,
    ERR_COVERAGE_INCOMPLETE,
    ERR_SPEC_INVALID,
    ERR_SPEC_UNSUPPORTED,
    FeatureFrame,
    StrategyContractError,
    StrategyKernel,
    build_frames,
    capability_hash,
    capability_payload,
    compile_strategy,
    initial_state,
    paper_tick,
    simulate,
)

REVISION = "0123456789abcdef0123456789abcdef01234567"
CAPABILITY_FIXTURE = (
    Path(__file__).parent / "fixtures" / "strategy_execution_capability_v1.json"
)


def _literal(value: str) -> dict:
    return {"node": "literal", "value_type": "decimal", "value": value}


def _condition(op: str = "gt") -> dict:
    return {"node": "compare", "op": op, "left": _literal("2"), "right": _literal("1")}


def make_spec(
    *,
    side: str = "long",
    condition: dict | None = None,
    stop_model: str = "fixed_percent",
    stop_value: object = "0.05",
    take_model: str = "fixed_percent",
    take_value: object = "0.05",
    sizing_model: str = "fixed_notional",
    sizing_value: str = "1000",
    leverage: str = "2",
    time_exit_bars: int | None = None,
    funding: str = "excluded",
    features: list[dict] | None = None,
) -> dict:
    return {
        "schema": "cutie.strategy_spec.v2",
        "strategy_family": "provider_contract_fixture",
        "market": {
            "market_type": "futures",
            "exchange": "binance",
            "symbols": ["BTCUSDT"],
            "timeframe": "1h",
        },
        "parameters": [],
        "features": copy.deepcopy(features or []),
        "entry": {
            "condition": copy.deepcopy(condition or _condition()),
            "side": side,
            "order_model": "next_bar_open",
            "cooldown_bars": 0,
        },
        "exit": {
            "stop_loss": {"model": stop_model, "value": copy.deepcopy(stop_value)},
            "take_profit": {"model": take_model, "value": copy.deepcopy(take_value)},
            "time_exit_bars": time_exit_bars,
            "signal_exit": None,
        },
        "risk": {
            "position_sizing": {"model": sizing_model, "value": sizing_value},
            "max_open_positions": 1,
            "allow_pyramiding": False,
            "leverage": leverage,
        },
        "execution": {
            "decision_clock": "closed_bar",
            "signal_effective_at": "next_bar_open",
            "intrabar_priority": [
                "stop_loss",
                "take_profit",
                "time_exit",
                "signal_exit",
            ],
            "position_mode": "one_way",
            "cost_model": {
                "schema": "cutie.execution_cost.v1",
                "fee_bps": "10",
                "slippage_bps": "5",
                "funding": funding,
            },
            "missing_data_policy": "fail",
            "kernel_api_version": "1",
        },
    }


def _infer_test_value(
    expr: dict, feature_types: dict[str, str], operators: list[dict]
) -> str:
    if expr["node"] == "literal":
        return expr["value_type"]
    if expr["node"] == "feature":
        return feature_types[expr["key"]]
    if expr["node"] == "arithmetic":
        arg_types = [
            _infer_test_value(arg, feature_types, operators) for arg in expr["args"]
        ]
        operators.append(
            {
                "node": "arithmetic",
                "op": expr["op"],
                "version": "1",
                "arg_types": arg_types,
                "return_type": arg_types[0],
            }
        )
        return arg_types[0]
    raise AssertionError(expr)


def _collect_test_condition(
    expr: dict, feature_types: dict[str, str], operators: list[dict]
) -> None:
    if expr["node"] in {"compare", "cross"}:
        left = _infer_test_value(expr["left"], feature_types, operators)
        right = _infer_test_value(expr["right"], feature_types, operators)
        operators.append(
            {
                "node": expr["node"],
                "op": expr["op"],
                "version": "1",
                "arg_types": [left, right],
                "return_type": "boolean",
            }
        )
        return
    if expr["node"] in {"all", "any"}:
        for child in expr["args"]:
            _collect_test_condition(child, feature_types, operators)
        return
    if expr["node"] == "not":
        _collect_test_condition(expr["arg"], feature_types, operators)
        return
    raise AssertionError(expr)


def _sorted_unique_objects(items: list[dict], fields: tuple[str, ...]) -> list[dict]:
    unique = {
        json.dumps(item, sort_keys=True, separators=(",", ":")): item for item in items
    }
    return sorted(
        unique.values(), key=lambda item: tuple(str(item[field]) for field in fields)
    )


def make_manifest(spec: dict) -> dict:
    feature_types = {item["key"]: item["output_type"] for item in spec["features"]}
    operators: list[dict] = []
    _collect_test_condition(spec["entry"]["condition"], feature_types, operators)
    if spec["exit"]["signal_exit"] is not None:
        _collect_test_condition(spec["exit"]["signal_exit"], feature_types, operators)
    for field in ("stop_loss", "take_profit"):
        rule = spec["exit"][field]
        if rule["model"] == "feature_expression":
            _infer_test_value(rule["value"], feature_types, operators)
    operators = _sorted_unique_objects(
        operators,
        ("node", "op", "version", "arg_types", "return_type"),
    )
    features = [
        {
            "primitive": item["primitive"],
            "version": item["primitive_version"],
            "source_stream": item["source_stream"],
            "interval": item["interval"],
            "value_kind": item["value_kind"],
            "output_type": item["output_type"],
        }
        for item in spec["features"]
    ]
    features = _sorted_unique_objects(
        features,
        (
            "primitive",
            "version",
            "source_stream",
            "interval",
            "value_kind",
            "output_type",
        ),
    )
    data_requirements = [
        {
            "stream_id": "binance.futures.kline.1h",
            "kind": "kline",
            "execution_role": "primary_execution_kline",
            "provider": "binance",
            "storage_source": "central_klines",
            "result_source": "binance_futures",
            "exchange": "binance",
            "market": "futures",
            "symbols": ["BTCUSDT"],
            "interval": "1h",
            "warmup_bars": 0,
            "max_freshness_seconds": 108000,
            "gap_policy": "none",
            "allowed_transforms": [],
        }
    ]
    if spec["features"]:
        data_requirements.append(
            {
                "stream_id": "coinglass.futures_cvd.1h",
                "kind": "feature",
                "execution_role": "feature_input",
                "provider": "coinglass",
                "storage_source": "market_metrics_history",
                "result_source": None,
                "exchange": "binance",
                "market": "futures",
                "symbols": ["BTCUSDT"],
                "interval": "1h",
                "warmup_bars": 2,
                "max_freshness_seconds": 108000,
                "gap_policy": "none",
                "allowed_transforms": [],
            }
        )
    data_requirements.sort(key=lambda item: item["stream_id"])
    data_sources = [
        {
            "provider": item["provider"],
            "storage_source": item["storage_source"],
            "kind": item["kind"],
            "market": item["market"],
            "result_source": item["result_source"],
        }
        for item in data_requirements
    ]
    data_sources = _sorted_unique_objects(
        data_sources,
        ("provider", "storage_source", "kind", "market", "result_source"),
    )
    source = {
        "source_artifact_id": "700001",
        "role": "strategy_definition",
        "source_sha256": "1" * 64,
        "content_sha256": "2" * 64,
        "parser_version": "1.0.0",
        "ingestion_schema": "cutie.strategy_lab_artifact_ingestion.v1",
        "ingestion_signature_version": "hmac-sha256.v2",
        "ingestion_key_id": "dev-v1",
        "ingestion_signature": "3" * 64,
    }
    return {
        "schema": "cutie.strategy_artifact_manifest.v1",
        "artifact_kind": "declarative_strategy",
        "strategy_spec_schema": "cutie.strategy_spec.v2",
        "spec_hash": canonical_json_sha256(spec),
        "compiler": {"id": "cutie.strategy_spec.compiler", "version": "1"},
        "kernel_contract": {
            "api_version": "1",
            "required_modes": ["historical_replay", "paper_tick"],
        },
        "capability_requirements": {
            "operators": operators,
            "features": features,
            "data_sources": data_sources,
            "cost_models": ["cutie.execution_cost.v1"],
            "data_transforms": [],
            "result_schemas": ["cutie.backtest_result.v2"],
            "coverage_schemas": ["cutie.strategy_coverage_manifest.v1"],
            "trace_schemas": ["cutie.strategy_execution_trace.v1"],
            "evidence_schemas": ["cutie.strategy_execution_evidence.v1"],
        },
        "data_requirements": data_requirements,
        "source_materials": [source],
        "provenance_policy": "exact_set",
    }


def execution_params(*, sizing_capital: str = "10000") -> dict:
    return {
        "schema_version": "cutie.execution_params.v1",
        "symbol": "BTCUSDT",
        "market": "futures",
        "timeframe": "1h",
        "start_at": 0,
        "end_at": 10800,
        "initial_capital": sizing_capital,
        "fee_bps": "10",
        "slippage_bps": "5",
        "provider_tool_id": COMPILER_TOOL_ID,
        "provider_params": {
            "instrument_rules": {
                "symbol": "BTCUSDT",
                "price_tick": "0.1",
                "qty_step": "0.001",
                "min_qty": "0.001",
                "min_notional": "5",
            }
        },
    }


def frame(
    index: int,
    *,
    open_: str = "100",
    high: str = "101",
    low: str = "99",
    close: str = "100",
) -> FeatureFrame:
    return FeatureFrame(
        bar_open_at=index * 3600,
        bar_close_at=(index + 1) * 3600,
        available_at=(index + 1) * 3600,
        symbol="BTCUSDT",
        values={"open": open_, "high": high, "low": low, "close": close, "volume": "1"},
        stream_revisions={},
    )


def compile_spec(spec: dict):
    return compile_strategy(spec, make_manifest(spec), capability_payload(REVISION))


def build_request(spec: dict) -> dict:
    manifest = make_manifest(spec)
    spec_hash = canonical_json_sha256(spec)
    manifest_hash = canonical_json_sha256(manifest)
    artifact_hash = canonical_json_sha256(
        {
            "schema": "cutie.strategy_artifact_digest.v1",
            "spec_hash": spec_hash,
            "manifest_hash": manifest_hash,
        }
    )
    return {
        "schema": "cutie.strategy_execution_request.v1",
        "execution_mode": "historical_replay",
        "run_id": "900001",
        "artifact": {
            "artifact_id": "900002",
            "artifact_version_id": "900003",
            "version_no": 1,
            "spec_hash": spec_hash,
            "manifest_hash": manifest_hash,
            "artifact_hash": artifact_hash,
        },
        "strategy_spec": spec,
        "artifact_manifest": manifest,
        "execution_params": execution_params(),
        "expected_capability_hash": capability_hash(capability_payload(REVISION)),
        "expected_provider_revision": REVISION,
        "dispatch_nonce": "nonce-62-2",
        "result_contract": {
            "result_schema": "cutie.backtest_result.v2",
            "coverage_schema": "cutie.strategy_coverage_manifest.v1",
            "trace_schema": "cutie.strategy_execution_trace.v1",
            "evidence_schema": "cutie.strategy_execution_evidence.v1",
        },
    }


def build_feature_request() -> dict:
    feature = {
        "key": "cvd_1h",
        "primitive": "rolling_sum",
        "primitive_version": "1",
        "source_stream": "coinglass.futures_cvd",
        "interval": "1h",
        "value_kind": "flow",
        "output_type": "decimal",
        "params": {"window_bars": 1},
        "required": True,
    }
    condition = {
        "node": "cross",
        "op": "crosses_above",
        "left": {"node": "feature", "key": "cvd_1h", "lag_bars": 0},
        "right": _literal("1.5"),
    }
    return build_request(make_spec(condition=condition, features=[feature]))


def set_request_warmup(request: dict, stream_id: str, warmup_bars: int) -> None:
    manifest = request["artifact_manifest"]
    requirement = next(
        item for item in manifest["data_requirements"] if item["stream_id"] == stream_id
    )
    requirement["warmup_bars"] = warmup_bars
    request["artifact"]["manifest_hash"] = canonical_json_sha256(manifest)
    request["artifact"]["artifact_hash"] = canonical_json_sha256(
        {
            "schema": "cutie.strategy_artifact_digest.v1",
            "spec_hash": request["artifact"]["spec_hash"],
            "manifest_hash": request["artifact"]["manifest_hash"],
        }
    )


def test_provider_capability_fixture_exact_payload_and_hash():
    fixture = json.loads(CAPABILITY_FIXTURE.read_text(encoding="utf-8"))
    assert (
        capability_payload(fixture["capability"]["provider_revision"])
        == fixture["capability"]
    )
    assert capability_hash(fixture["capability"]) == fixture["sha256"]
    assert fixture["capability"]["data_transforms"] == ["ohlcv_resample.v1"]
    assert (
        fixture["sha256"]
        == "4e1760a3d6fa9ee5325d680acc830eb860f185d5c9dc05b6dbe5fd0d646bbad1"
    )


def test_malformed_nested_capability_fails_as_capability_mismatch():
    capability = capability_payload(REVISION)
    capability["operators"][0]["signatures"][0]["unknown"] = True

    with pytest.raises(StrategyContractError) as caught:
        compile_strategy(make_spec(), make_manifest(make_spec()), capability)

    assert caught.value.code == ERR_CAPABILITY_MISMATCH
    assert caught.value.path.endswith("signatures[0]")


def test_capability_cannot_overclaim_an_invalid_operator_arity():
    capability = capability_payload(REVISION)
    capability["operators"][0]["signatures"][0]["arg_types"] = ["decimal"]

    with pytest.raises(StrategyContractError) as caught:
        compile_strategy(make_spec(), make_manifest(make_spec()), capability)

    assert caught.value.code == ERR_CAPABILITY_MISMATCH
    assert caught.value.path.endswith("signatures[0]")


def test_unknown_spec_key_fails_closed_as_invalid():
    spec = make_spec()
    spec["python_code"] = "print('must never execute')"
    with pytest.raises(StrategyContractError) as caught:
        compile_strategy(spec, make_manifest(make_spec()), capability_payload(REVISION))
    assert caught.value.code == ERR_SPEC_INVALID
    assert caught.value.path == "$.strategy_spec"


def test_valid_but_unadvertised_operator_is_unsupported_not_defaulted():
    spec = make_spec(condition=_condition("gte"))
    with pytest.raises(StrategyContractError) as caught:
        compile_spec(spec)
    assert caught.value.code == ERR_SPEC_UNSUPPORTED
    assert "operators" in caught.value.path


@pytest.mark.parametrize("op", ["add", "mul", "min", "max"])
def test_variadic_arithmetic_uses_exact_declared_signature(op: str):
    arithmetic = {
        "node": "arithmetic",
        "op": op,
        "args": [_literal("3"), _literal("2"), _literal("1")],
    }
    spec = make_spec(
        condition={
            "node": "compare",
            "op": "gt",
            "left": arithmetic,
            "right": _literal("0"),
        }
    )
    capability = capability_payload(REVISION)
    signature = {
        "arg_types": ["decimal", "decimal", "decimal"],
        "return_type": "decimal",
    }
    operator = next(
        (
            item
            for item in capability["operators"]
            if item["node"] == "arithmetic" and item["op"] == op
        ),
        None,
    )
    if operator is None:
        capability["operators"].append(
            {
                "node": "arithmetic",
                "op": op,
                "version": "1",
                "signatures": [signature],
            }
        )
    else:
        operator["signatures"].append(signature)
        operator["signatures"].sort(
            key=lambda item: (tuple(item["arg_types"]), item["return_type"])
        )
    capability["operators"].sort(
        key=lambda item: (item["node"], item["op"], item["version"])
    )

    plan = compile_strategy(spec, make_manifest(spec), capability)

    assert {
        "node": "arithmetic",
        "op": op,
        "version": "1",
        "arg_types": ["decimal", "decimal", "decimal"],
        "return_type": "decimal",
    } in plan.artifact_manifest["capability_requirements"]["operators"]
    result = simulate(
        plan, [frame(0), frame(1)], initial_state(plan, execution_params())
    )
    assert len(result["trades"]) == 1


@pytest.mark.parametrize(
    ("op", "args"),
    [
        ("sub", [_literal("3"), _literal("2"), _literal("1")]),
        ("div", [_literal("3"), _literal("2"), _literal("1")]),
        ("abs", [_literal("3"), _literal("2")]),
    ],
)
def test_fixed_arity_arithmetic_fails_closed(op: str, args: list[dict]):
    spec = make_spec(
        condition={
            "node": "compare",
            "op": "gt",
            "left": {"node": "arithmetic", "op": op, "args": args},
            "right": _literal("0"),
        }
    )

    with pytest.raises(StrategyContractError) as caught:
        compile_strategy(spec, make_manifest(spec), capability_payload(REVISION))

    assert caught.value.code == ERR_SPEC_INVALID
    assert caught.value.path.endswith("args")


def test_funding_included_is_structurally_valid_but_unsupported():
    spec = make_spec(funding="included")
    with pytest.raises(StrategyContractError) as caught:
        compile_spec(spec)
    assert caught.value.code == ERR_SPEC_UNSUPPORTED
    assert caught.value.path.endswith("funding")


def test_long_next_open_same_bar_priority_and_cost_math():
    spec = make_spec()
    plan = compile_spec(spec)
    state = initial_state(plan, execution_params())
    result = simulate(
        plan,
        [frame(0), frame(1, high="106", low="94", close="101")],
        state,
    )
    assert result["trades"] == [
        {
            "seq": 1,
            "opened_at": 3600,
            "closed_at": 3600,
            "side": "long",
            "qty": "10",
            "entry_price": "100",
            "exit_price": "95",
            "fee": "1.95",
            "slippage": "0.975",
            "pnl": "-52.925",
        }
    ]
    assert result["trace_trades"][0]["exit_kind"] == "stop_loss"
    assert result["metrics"]["total_return"] == "-0.0052925"
    assert result["diagnostics"][-1]["kind"] == "no_next_bar"


def test_short_gap_take_uses_open_and_non_negative_costs():
    plan = compile_spec(make_spec(side="short"))
    result = simulate(
        plan,
        [
            frame(0),
            frame(1, high="104", low="96"),
            frame(2, open_="94", high="95", low="93", close="94"),
        ],
        initial_state(plan, execution_params()),
    )
    trade = result["trades"][0]
    assert trade["side"] == "short"
    assert trade["exit_price"] == "94"
    assert trade["fee"] == "1.94"
    assert trade["slippage"] == "0.97"
    assert trade["pnl"] == "57.09"
    assert result["trace_trades"][0]["exit_kind"] == "take_profit"


@pytest.mark.parametrize(
    ("side", "high", "low", "expected_kind", "expected_price"),
    [
        ("long", "106", "96", "take_profit", "105"),
        ("short", "106", "96", "stop_loss", "105"),
    ],
)
def test_remaining_fixed_percent_directions(
    side: str,
    high: str,
    low: str,
    expected_kind: str,
    expected_price: str,
):
    plan = compile_spec(make_spec(side=side))

    result = simulate(
        plan,
        [frame(0), frame(1, high=high, low=low)],
        initial_state(plan, execution_params()),
    )

    assert result["trace_trades"][0]["exit_kind"] == expected_kind
    assert result["trades"][0]["exit_price"] == expected_price


def test_r_multiple_uses_quantized_stop_distance():
    plan = compile_spec(make_spec(take_model="r_multiple", take_value="2"))

    result = simulate(
        plan,
        [frame(0), frame(1, high="111", low="96")],
        initial_state(plan, execution_params()),
    )

    assert result["trace_trades"][0]["exit_kind"] == "take_profit"
    assert result["trades"][0]["exit_price"] == "110"


def test_feature_expression_prices_are_frozen_on_the_signal_frame():
    plan = compile_spec(
        make_spec(
            stop_model="feature_expression",
            stop_value=_literal("95"),
            take_model="feature_expression",
            take_value=_literal("105"),
        )
    )

    result = simulate(
        plan,
        [frame(0), frame(1, high="106", low="96")],
        initial_state(plan, execution_params()),
    )

    assert result["trace_trades"][0]["stop_loss"] == "95"
    assert result["trace_trades"][0]["take_profit"] == "105"
    assert result["trace_trades"][0]["exit_kind"] == "take_profit"


@pytest.mark.parametrize(
    ("model", "value", "expected_qty"),
    [
        ("fixed_notional", "1000", "10"),
        ("fixed_margin", "1000", "20"),
        ("risk_fraction", "0.1", "200"),
    ],
)
def test_sizing_models_are_deterministic(model: str, value: str, expected_qty: str):
    plan = compile_spec(make_spec(sizing_model=model, sizing_value=value))
    result = simulate(
        plan,
        [frame(0), frame(1)],
        initial_state(plan, execution_params()),
    )
    assert result["trades"][0]["qty"] == expected_qty
    assert result["trace_trades"][0]["exit_kind"] == "end_of_data"


def test_qty_rounding_and_minimum_order_reject_without_trade():
    plan = compile_spec(make_spec(sizing_value="0.05"))
    result = simulate(
        plan,
        [frame(0), frame(1)],
        initial_state(plan, execution_params()),
    )
    assert result["trades"] == []
    assert any(item["kind"] == "order_rejected" for item in result["diagnostics"])


def test_qty_rounds_down_to_trusted_step():
    plan = compile_spec(make_spec())

    result = simulate(
        plan,
        [frame(0), frame(1, open_="101", high="102", low="100", close="101")],
        initial_state(plan, execution_params()),
    )

    assert result["trades"][0]["qty"] == "9.9"
    assert result["trace_trades"][0]["exit_kind"] == "end_of_data"


def test_feature_frames_cross_and_never_read_future_availability():
    feature = {
        "key": "cvd_1h",
        "primitive": "rolling_sum",
        "primitive_version": "1",
        "source_stream": "coinglass.futures_cvd",
        "interval": "1h",
        "value_kind": "flow",
        "output_type": "decimal",
        "params": {"window_bars": 1},
        "required": True,
    }
    condition = {
        "node": "cross",
        "op": "crosses_above",
        "left": {"node": "feature", "key": "cvd_1h", "lag_bars": 0},
        "right": _literal("1.5"),
    }
    spec = make_spec(condition=condition, features=[feature])
    plan = compile_spec(spec)
    primary = [
        {
            "open_time": index * 3600,
            "open": "100",
            "high": "101",
            "low": "99",
            "close": "100",
            "volume": "1",
        }
        for index in range(3)
    ]
    feature_rows = [
        {
            "ts": index * 3600,
            "value": value,
            "available_at": (index + 1) * 3600,
            "revision": "r1",
        }
        for index, value in enumerate(("1", "2", "3"))
    ]
    coverage = {
        "summary": {"strict_eligible": True},
        "request_identity": {"symbol": "BTCUSDT"},
    }
    frames = build_frames(
        {
            "binance.futures.kline.1h": primary,
            "coinglass.futures_cvd.1h": feature_rows,
        },
        coverage,
        plan,
    )
    assert [item.values["cvd_1h"] for item in frames] == ["1", "2", "3"]
    result = simulate(plan, frames, initial_state(plan, execution_params()))
    assert result["trades"][0]["opened_at"] == 7200

    future_rows = copy.deepcopy(feature_rows)
    future_rows[0]["available_at"] = 7201
    with pytest.raises(StrategyContractError) as caught:
        build_frames(
            {
                "binance.futures.kline.1h": primary,
                "coinglass.futures_cvd.1h": future_rows,
            },
            coverage,
            plan,
        )
    assert caught.value.code == ERR_COVERAGE_INCOMPLETE


def test_multiple_features_share_one_declared_source_without_overwrite():
    features = [
        {
            "key": key,
            "primitive": "rolling_sum",
            "primitive_version": "1",
            "source_stream": "coinglass.futures_cvd",
            "interval": "1h",
            "value_kind": "flow",
            "output_type": "decimal",
            "params": {"window_bars": window},
            "required": True,
        }
        for key, window in (("cvd_fast", 1), ("cvd_slow", 2))
    ]
    condition = {
        "node": "compare",
        "op": "gt",
        "left": {"node": "feature", "key": "cvd_fast", "lag_bars": 0},
        "right": {"node": "feature", "key": "cvd_slow", "lag_bars": 0},
    }
    plan = compile_spec(make_spec(condition=condition, features=features))
    primary = [
        {
            "open_time": timestamp,
            "open": "100",
            "high": "101",
            "low": "99",
            "close": "100",
            "volume": "1",
        }
        for timestamp in (3600, 7200)
    ]
    feature_rows = [
        {
            "ts": timestamp,
            "value": value,
            "available_at": timestamp + 3600,
            "revision": "r1",
        }
        for timestamp, value in ((0, "1"), (3600, "2"), (7200, "3"))
    ]

    frames = build_frames(
        {
            "binance.futures.kline.1h": primary,
            "coinglass.futures_cvd.1h": feature_rows,
        },
        {
            "summary": {"strict_eligible": True},
            "request_identity": {"symbol": "BTCUSDT"},
        },
        plan,
    )

    assert frames[0].values["cvd_fast"] == "2"
    assert frames[0].values["cvd_slow"] == "3"


def test_warmup_frame_extends_lookback_but_can_never_trade():
    """A frame before execution_start_at must be appended to state.frames
    (so lagged feature/cross lookups have history) but must never reach
    entry/exit decision logic. Before the kernel had warmup semantics,
    evaluate() rejected any bar_open_at < execution_start_at outright, so
    simulate() below would raise KernelExecutionError on the first frame.
    """
    plan = compile_spec(make_spec())
    primary = [
        {
            "open_time": index * 3600,
            "open": "100",
            "high": "101",
            "low": "99",
            "close": "100",
            "volume": "1",
        }
        for index in range(4)
    ]
    coverage = {
        "summary": {"strict_eligible": True},
        "request_identity": {"symbol": "BTCUSDT"},
    }
    frames = build_frames({"binance.futures.kline.1h": primary}, coverage, plan)
    assert len(frames) == 4

    params = execution_params()
    params["start_at"] = 3600
    params["end_at"] = 3600 + 3 * 3600
    result = simulate(plan, frames, initial_state(plan, params))

    # The always-true entry condition is satisfied on the warmup frame
    # (index 0, ts=0) too; if warmup could reach decision logic, entry would
    # be signalled there and filled on the first evaluation frame's open
    # (ts=3600). Warmup semantics forbid this: the earliest eligible signal
    # is the first evaluation frame (index 1), filled on the next one.
    assert not any(item["frame_index"] == 0 for item in result["decisions"])
    assert result["trades"][0]["opened_at"] == 7200

    # Positive lock on the frame_index offset documented on KernelState:
    # frame_index is the physical index into state.frames (warmup frames
    # counted), so the finalize() end-of-data close on the last of the 4
    # frames (F3) must carry frame_index == len(frames) - 1 == 3, not an
    # evaluation-window-relative index.
    assert len(frames) == 4
    assert result["decisions"][-1]["frame_index"] == len(frames) - 1 == 3
    assert result["fill_ledger"][-1]["frame_index"] == len(frames) - 1 == 3


def test_replay_loop_and_paper_tick_use_identical_evaluate():
    plan = compile_spec(make_spec(time_exit_bars=2))
    frames = [frame(0), frame(1), frame(2)]
    replay = simulate(plan, frames, initial_state(plan, execution_params()))

    state = initial_state(plan, execution_params())
    kernel = StrategyKernel(plan)
    for item in frames:
        paper_tick(kernel, state, item)
    kernel.finalize(state)
    assert state.trades == replay["trades"]
    assert state.trace_trades == replay["trace_trades"]
    assert state.decisions == replay["decisions"]
    assert state.fill_ledger == replay["fill_ledger"]
    assert state.cost_ledger == replay["cost_ledger"]


def test_initial_state_requires_trusted_instrument_rules():
    plan = compile_spec(make_spec())
    params = execution_params()
    params["provider_params"] = {}
    with pytest.raises(StrategyContractError) as caught:
        initial_state(plan, params)
    assert caught.value.code == ERR_SPEC_INVALID


@pytest.mark.parametrize(
    ("duration_days", "expected_chunks"),
    [(90, 1), (91, 2), (365, 5)],
)
def test_feature_fetch_maps_closed_api_chunks_to_exact_half_open_stream(
    duration_days,
    expected_chunks,
    monkeypatch,
):
    day_seconds = 24 * 60 * 60
    requested_ranges: list[tuple[int, int]] = []

    def fetch_chunk(**kwargs):
        start_at = kwargs["start_at"]
        end_at = kwargs["end_at"]
        requested_ranges.append((start_at, end_at))
        first_point = ((start_at + day_seconds - 1) // day_seconds) * day_seconds
        return [
            {"ts": timestamp, "value": str(timestamp // day_seconds)}
            for timestamp in range(first_point, end_at + 1, day_seconds)
        ]

    monkeypatch.setattr(provider, "_fetch_artifact_metric_chunk", fetch_chunk)
    rows = provider._fetch_artifact_features(
        {
            "stream_id": "coinglass.futures_cvd.1d",
            "exchange": "binance",
            "interval": "1d",
        },
        {"features": [{"source_stream": "coinglass.futures_cvd"}]},
        "BTCUSDT",
        0,
        duration_days * day_seconds,
    )

    assert len(rows) == duration_days
    assert [row["ts"] for row in rows] == [
        index * day_seconds for index in range(duration_days)
    ]
    assert len(requested_ranges) == expected_chunks
    assert all(
        end_at - start_at + 1 <= 90 * day_seconds
        for start_at, end_at in requested_ranges
    )
    assert all(
        previous_end + 1 == current_start
        for (_, previous_end), (current_start, _) in zip(
            requested_ranges, requested_ranges[1:]
        )
    )


def test_feature_fetch_does_not_mask_conflicting_duplicate_points(monkeypatch):
    monkeypatch.setattr(
        provider,
        "_fetch_artifact_metric_chunk",
        lambda **kwargs: [{"ts": 0, "value": "1"}, {"ts": 0, "value": "2"}],
    )

    with pytest.raises(StrategyContractError) as caught:
        provider._fetch_artifact_features(
            {
                "stream_id": "coinglass.futures_cvd.1d",
                "exchange": "binance",
                "interval": "1d",
            },
            {"features": [{"source_stream": "coinglass.futures_cvd"}]},
            "BTCUSDT",
            0,
            24 * 60 * 60,
        )

    assert caught.value.code == ERR_COVERAGE_INCOMPLETE
    assert caught.value.path == "$.data_streams.feature"


def test_catalog_only_compiler_tool_advertises_artifact_capability(monkeypatch):
    monkeypatch.setattr(provider, "PROVIDER_REVISION", REVISION)
    response = TestClient(provider.app).get("/catalog")
    assert response.status_code == 200
    tools = response.json()["tools"]
    compiler = next(item for item in tools if item["tool_id"] == COMPILER_TOOL_ID)
    assert compiler["strategy_execution_capability"] == capability_payload(REVISION)
    assert compiler["strategy_execution_capability_hash"] == capability_hash(
        capability_payload(REVISION)
    )
    assert all(
        "strategy_execution_capability" not in item
        for item in tools
        if item["tool_id"] != COMPILER_TOOL_ID
    )


def test_catalog_omits_compiler_until_revision_is_immutable(monkeypatch):
    monkeypatch.setattr(provider, "PROVIDER_REVISION", "unknown")

    response = TestClient(provider.app).get("/catalog")

    assert response.status_code == 200
    assert COMPILER_TOOL_ID not in {
        item["tool_id"] for item in response.json()["tools"]
    }


def test_partial_artifact_intent_never_falls_back_to_legacy(monkeypatch):
    monkeypatch.setattr(provider, "PROVIDER_REVISION", REVISION)
    response = TestClient(provider.app).post(
        "/cutie/backtest",
        json={"artifact": {"artifact_id": "900002"}},
        headers={"X-Cutie-Connector-Version": "1.2.3"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["result_status"] == "failed"
    assert body["error_type"] == ERR_SPEC_INVALID
    assert "executed_strategy_name" not in json.dumps(body)


@pytest.mark.parametrize(
    ("field_path", "value"),
    [
        (("run_id",), "9" * 100_000),
        (("artifact", "version_no"), 2**53),
    ],
)
def test_execution_identity_bounds_fail_closed(monkeypatch, field_path, value):
    monkeypatch.setattr(provider, "PROVIDER_REVISION", REVISION)
    request = build_request(make_spec())
    target = request
    for key in field_path[:-1]:
        target = target[key]
    target[field_path[-1]] = value

    response = TestClient(provider.app).post(
        "/cutie/backtest",
        json=request,
        headers={"X-Cutie-Connector-Version": "1.2.3"},
    )

    assert response.status_code == 200
    assert response.json()["error_type"] == ERR_SPEC_INVALID


def test_artifact_range_limit_allows_exactly_365_days(monkeypatch):
    monkeypatch.setattr(provider, "PROVIDER_REVISION", REVISION)
    calls: list[tuple[int, int]] = []

    def fetch_klines(exchange, market, symbol, timeframe, start_ms, end_ms):
        calls.append((start_ms, end_ms))
        return [[0, 100, 101, 99, 100, 1]]

    monkeypatch.setattr(provider, "_fetch_from_central", fetch_klines)
    request = build_request(make_spec())
    request["execution_params"]["end_at"] = 365 * 24 * 60 * 60

    response = TestClient(provider.app).post(
        "/cutie/backtest",
        json=request,
        headers={"X-Cutie-Connector-Version": "1.2.3"},
    )

    assert calls == [(0, request["execution_params"]["end_at"] * 1000)]
    assert response.status_code == 200
    assert response.json()["error_type"] == ERR_COVERAGE_INCOMPLETE


@pytest.mark.parametrize(
    "range_seconds",
    [365 * 24 * 60 * 60 + 1, 366 * 24 * 60 * 60],
)
def test_artifact_range_over_365_days_fails_before_data_access(
    range_seconds,
    monkeypatch,
):
    monkeypatch.setattr(provider, "PROVIDER_REVISION", REVISION)

    def fail_if_fetched(*args, **kwargs):
        raise AssertionError("range validation must happen before data access")

    monkeypatch.setattr(provider, "_fetch_from_central", fail_if_fetched)
    request = build_request(make_spec())
    request["execution_params"]["end_at"] = range_seconds

    response = TestClient(provider.app).post(
        "/cutie/backtest",
        json=request,
        headers={"X-Cutie-Connector-Version": "1.2.3"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["error_type"] == ERR_SPEC_INVALID
    assert body["error_detail"]["path"] == "$.execution_params"


def test_feature_warmup_400_days_fails_before_any_adapter(monkeypatch):
    monkeypatch.setattr(provider, "PROVIDER_REVISION", REVISION)
    calls = {"kline": 0, "feature": 0}

    def fail_kline(*args, **kwargs):
        calls["kline"] += 1
        raise AssertionError("warmup budget must be checked before K-line access")

    def fail_feature(**kwargs):
        calls["feature"] += 1
        raise AssertionError("warmup budget must be checked before feature access")

    monkeypatch.setattr(provider, "_fetch_from_central", fail_kline)
    monkeypatch.setattr(provider, "_fetch_artifact_metric_chunk", fail_feature)
    request = build_feature_request()
    day_seconds = 24 * 60 * 60
    request["execution_params"]["start_at"] = 400 * day_seconds
    request["execution_params"]["end_at"] = 401 * day_seconds
    set_request_warmup(
        request,
        "coinglass.futures_cvd.1h",
        400 * 24,
    )

    response = TestClient(provider.app).post(
        "/cutie/backtest",
        json=request,
        headers={"X-Cutie-Connector-Version": "1.2.3"},
    )

    assert calls == {"kline": 0, "feature": 0}
    assert response.status_code == 200
    body = response.json()
    assert body["error_type"] == ERR_SPEC_INVALID
    assert (
        body["error_detail"]["path"]
        == "$.artifact_manifest.data_requirements[1].warmup_bars"
    )


@pytest.mark.parametrize(
    ("extra_warmup_steps", "expected_fetches", "expected_error"),
    [(0, 1, ERR_COVERAGE_INCOMPLETE), (1, 0, ERR_SPEC_INVALID)],
)
def test_requirement_adapter_window_enforces_365_day_budget(
    extra_warmup_steps,
    expected_fetches,
    expected_error,
    monkeypatch,
):
    monkeypatch.setattr(provider, "PROVIDER_REVISION", REVISION)
    fetches = 0

    def unavailable_kline(*args, **kwargs):
        nonlocal fetches
        fetches += 1
        return None

    monkeypatch.setattr(provider, "_fetch_from_central", unavailable_kline)
    request = build_feature_request()
    hour_seconds = 60 * 60
    warmup_bars = 364 * 24 + extra_warmup_steps
    request["execution_params"]["start_at"] = warmup_bars * hour_seconds
    request["execution_params"]["end_at"] = (
        request["execution_params"]["start_at"] + 24 * hour_seconds
    )
    set_request_warmup(
        request,
        "coinglass.futures_cvd.1h",
        warmup_bars,
    )

    response = TestClient(provider.app).post(
        "/cutie/backtest",
        json=request,
        headers={"X-Cutie-Connector-Version": "1.2.3"},
    )

    assert fetches == expected_fetches
    assert response.status_code == 200
    body = response.json()
    assert body["error_type"] == expected_error
    if extra_warmup_steps:
        assert (
            body["error_detail"]["path"]
            == "$.artifact_manifest.data_requirements[1].warmup_bars"
        )


def test_365_day_execution_with_positive_warmup_fails_before_fetch(monkeypatch):
    monkeypatch.setattr(provider, "PROVIDER_REVISION", REVISION)
    fetches = 0

    def unavailable_kline(*args, **kwargs):
        nonlocal fetches
        fetches += 1
        return None

    monkeypatch.setattr(provider, "_fetch_from_central", unavailable_kline)
    request = build_request(make_spec())
    hour_seconds = 60 * 60
    request["execution_params"]["start_at"] = hour_seconds
    request["execution_params"]["end_at"] = hour_seconds + 365 * 24 * hour_seconds
    set_request_warmup(request, "binance.futures.kline.1h", 1)

    response = TestClient(provider.app).post(
        "/cutie/backtest",
        json=request,
        headers={"X-Cutie-Connector-Version": "1.2.3"},
    )

    assert fetches == 0
    assert response.status_code == 200
    body = response.json()
    assert body["error_type"] == ERR_SPEC_INVALID
    assert (
        body["error_detail"]["path"]
        == "$.artifact_manifest.data_requirements[0].warmup_bars"
    )


def test_spec_example_720_hour_warmup_with_short_execution_enters_fetch(monkeypatch):
    monkeypatch.setattr(provider, "PROVIDER_REVISION", REVISION)
    fetches = 0

    def unavailable_kline(*args, **kwargs):
        nonlocal fetches
        fetches += 1
        return None

    monkeypatch.setattr(provider, "_fetch_from_central", unavailable_kline)
    request = build_feature_request()
    hour_seconds = 60 * 60
    request["execution_params"]["start_at"] = 720 * hour_seconds
    request["execution_params"]["end_at"] = 744 * hour_seconds
    set_request_warmup(request, "coinglass.futures_cvd.1h", 720)

    response = TestClient(provider.app).post(
        "/cutie/backtest",
        json=request,
        headers={"X-Cutie-Connector-Version": "1.2.3"},
    )

    assert fetches == 1
    assert response.status_code == 200
    assert response.json()["error_type"] == ERR_COVERAGE_INCOMPLETE


@pytest.mark.parametrize(
    ("case", "expected_path"),
    [
        ("empty_rules", "$.execution_params.provider_params.instrument_rules"),
        (
            "rule_symbol_mismatch",
            "$.execution_params.provider_params.instrument_rules.symbol",
        ),
        ("fee_mismatch", "$.execution_params"),
        ("slippage_mismatch", "$.execution_params"),
    ],
)
def test_artifact_execution_params_fail_before_data_access(
    case,
    expected_path,
    monkeypatch,
):
    monkeypatch.setattr(provider, "PROVIDER_REVISION", REVISION)

    def fail_if_fetched(*args, **kwargs):
        raise AssertionError("execution params must be validated before data access")

    monkeypatch.setattr(provider, "_fetch_from_central", fail_if_fetched)
    request = build_request(make_spec())
    params = request["execution_params"]
    if case == "empty_rules":
        params["provider_params"]["instrument_rules"] = {}
    elif case == "rule_symbol_mismatch":
        params["provider_params"]["instrument_rules"]["symbol"] = "ETHUSDT"
    elif case == "fee_mismatch":
        params["fee_bps"] = "11"
    else:
        params["slippage_bps"] = "6"

    response = TestClient(provider.app).post(
        "/cutie/backtest",
        json=request,
        headers={"X-Cutie-Connector-Version": "1.2.3"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["error_type"] == ERR_SPEC_INVALID
    assert body["error_detail"]["path"] == expected_path


def test_artifact_endpoint_executes_compiled_plan_and_returns_hashed_evidence(
    monkeypatch,
):
    monkeypatch.setattr(provider, "PROVIDER_REVISION", REVISION)
    monkeypatch.setattr(
        provider,
        "_fetch_from_central",
        lambda exchange, market, symbol, timeframe, start_ms, end_ms: [
            [0, 100, 101, 99, 100, 1],
            [3_600_000, 100, 106, 94, 101, 1],
            [7_200_000, 101, 102, 100, 101, 1],
        ],
    )
    request = build_request(make_spec())
    response = TestClient(provider.app).post(
        "/cutie/backtest",
        json=request,
        headers={"X-Cutie-Connector-Version": "1.2.3"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["result_status"] == "success"
    assert body["schema_version"] == "cutie.backtest_result.v2"
    assert body["capability_hash"] == request["expected_capability_hash"]
    assert (
        canonical_json_sha256(body["coverage_manifest"])
        == body["coverage_manifest_hash"]
    )
    assert (
        canonical_json_sha256(body["execution_trace"]) == body["execution_trace_hash"]
    )
    assert (
        canonical_json_sha256(body["execution_evidence"])
        == body["execution_evidence_hash"]
    )
    assert body["data_manifest"]["source"] == "binance_futures"
    assert body["execution_evidence"]["executed_params_hash"] == canonical_json_sha256(
        request["execution_params"]
    )


def test_primary_warmup_fetch_covers_warmup_and_data_manifest_stays_evaluation_only(
    monkeypatch,
):
    """Regression for the Pre incident (run 336079400808218624):
    primary_execution_kline declared warmup_bars>0 while the fetch only ever
    requested [start_at, end_at), so the coverage check that requires the
    fetch to reach warmup_start_at always failed
    (ERR_STRATEGY_COVERAGE_INCOMPLETE). The fetch must widen to warmup_start
    like every other role, while result.v2 data_manifest (SPEC §7.5) keeps
    reporting the evaluation window only.
    """
    monkeypatch.setattr(provider, "PROVIDER_REVISION", REVISION)
    fetch_calls: list[tuple[int, int]] = []

    def fetch_klines(exchange, market, symbol, timeframe, start_ms, end_ms):
        fetch_calls.append((start_ms, end_ms))
        return [
            [0, 100, 101, 99, 100, 1],
            [3_600_000, 100, 106, 94, 101, 1],
            [7_200_000, 101, 102, 100, 101, 1],
            [10_800_000, 101, 102, 100, 101, 1],
        ]

    monkeypatch.setattr(provider, "_fetch_from_central", fetch_klines)
    request = build_request(make_spec())
    request["execution_params"]["start_at"] = 3600
    request["execution_params"]["end_at"] = 3600 + 3 * 3600
    set_request_warmup(request, "binance.futures.kline.1h", 1)

    response = TestClient(provider.app).post(
        "/cutie/backtest",
        json=request,
        headers={"X-Cutie-Connector-Version": "1.2.3"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["result_status"] == "success"
    # The fetch must reach warmup_start_at (ts=0), not just the evaluation
    # start (ts=3600).
    assert fetch_calls == [(0, 14_400_000)]

    data_manifest = body["data_manifest"]
    assert data_manifest["start_at"] == 3600
    assert data_manifest["end_at"] == 14400
    assert data_manifest["kline_count"] == 3

    primary_stream = next(
        item
        for item in body["coverage_manifest"]["streams"]
        if item["execution_role"] == "primary_execution_kline"
    )
    assert primary_stream["required_range"] == {
        "start_at": 3600,
        "end_at": 14400,
        "warmup_start_at": 0,
    }
    # SPEC §7.5: primary actual_range/point_count/checksum must mirror
    # data_manifest exactly; warmup is proven only via required_range, never
    # by widening actual_range.
    assert primary_stream["actual_range"]["start_at"] == data_manifest["start_at"]
    assert primary_stream["actual_range"]["end_at"] == data_manifest["end_at"]
    assert primary_stream["point_count"] == data_manifest["kline_count"] == 3
    assert primary_stream["checksum"]["value"] == data_manifest["checksum"]

    # The always-true entry condition also matches on the warmup bar
    # (ts=0); if the kernel let warmup bars reach decision logic, the first
    # trade would open at ts=3600 (signalled on the warmup bar, filled on
    # the first evaluation bar). It must instead open at ts=7200 (signalled
    # on the first evaluation bar, filled on the second).
    assert body["trades"][0]["opened_at"] == 7200


def test_primary_warmup_insufficient_history_fails_closed(monkeypatch):
    """The central adapter is missing the declared warmup bar (ts=0); only
    the evaluation-window bars exist. Coverage must fail closed instead of
    silently running the strategy without its declared warmup."""
    monkeypatch.setattr(provider, "PROVIDER_REVISION", REVISION)

    def fetch_klines(exchange, market, symbol, timeframe, start_ms, end_ms):
        return [
            [3_600_000, 100, 106, 94, 101, 1],
            [7_200_000, 101, 102, 100, 101, 1],
            [10_800_000, 101, 102, 100, 101, 1],
        ]

    monkeypatch.setattr(provider, "_fetch_from_central", fetch_klines)
    request = build_request(make_spec())
    request["execution_params"]["start_at"] = 3600
    request["execution_params"]["end_at"] = 3600 + 3 * 3600
    set_request_warmup(request, "binance.futures.kline.1h", 1)

    response = TestClient(provider.app).post(
        "/cutie/backtest",
        json=request,
        headers={"X-Cutie-Connector-Version": "1.2.3"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["error_type"] == ERR_COVERAGE_INCOMPLETE
    assert body["error_detail"]["path"] == "$.coverage.binance.futures.kline.1h"


@pytest.mark.parametrize(
    "transform",
    [
        "combine_first.v1",
        "ffill_after_close.v1",
        "flow_dilution_shifted.v1",
    ],
)
def test_unimplemented_transform_fails_at_capability_before_data_access(
    transform,
    monkeypatch,
):
    """These three coarse-fill transforms remain unimplemented in 62-2b Phase 1;
    only ohlcv_resample.v1 is advertised (see test_ohlcv_resample_* below)."""
    monkeypatch.setattr(provider, "PROVIDER_REVISION", REVISION)

    def fail_if_fetched(*args, **kwargs):
        raise AssertionError("capability rejection must happen before data access")

    monkeypatch.setattr(provider, "_fetch_from_central", fail_if_fetched)
    request = build_request(make_spec())
    manifest = request["artifact_manifest"]
    manifest["data_requirements"][0]["allowed_transforms"] = [transform]
    manifest["capability_requirements"]["data_transforms"] = [transform]
    request["artifact"]["manifest_hash"] = canonical_json_sha256(manifest)
    request["artifact"]["artifact_hash"] = canonical_json_sha256(
        {
            "schema": "cutie.strategy_artifact_digest.v1",
            "spec_hash": request["artifact"]["spec_hash"],
            "manifest_hash": request["artifact"]["manifest_hash"],
        }
    )

    with pytest.raises(StrategyContractError) as caught:
        compile_strategy(
            request["strategy_spec"], manifest, capability_payload(REVISION)
        )

    assert caught.value.code == ERR_SPEC_UNSUPPORTED
    assert (
        caught.value.path
        == "$.artifact_manifest.capability_requirements.data_transforms"
    )
    assert caught.value.required == transform
    assert caught.value.actual == ["ohlcv_resample.v1"]

    response = TestClient(provider.app).post(
        "/cutie/backtest",
        json=request,
        headers={"X-Cutie-Connector-Version": "1.2.3"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["error_type"] == ERR_SPEC_UNSUPPORTED
    assert body["error_detail"]["path"] == caught.value.path
    assert body["error_detail"]["required"] == transform
    assert body["error_detail"]["actual"] == ["ohlcv_resample.v1"]
    assert "coverage_manifest" not in body
