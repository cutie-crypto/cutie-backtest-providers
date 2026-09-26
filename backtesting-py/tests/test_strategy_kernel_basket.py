"""123 组合策略 B1a: ``cutie.strategy_spec.v3`` compiler, multi-leg frames and
``rolling_zscore`` (SPEC_组合策略v3契约 §0, §1, §2.1–§2.7, §9 provider)."""

from __future__ import annotations

import sys
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from canonical_json import canonical_json, canonical_json_sha256  # noqa: E402
from strategy_kernel import (  # noqa: E402
    ERR_COVERAGE_INCOMPLETE,
    ERR_SPEC_INVALID,
    ERR_SPEC_UNSUPPORTED,
    STRATEGY_SPEC_V3_SCHEMA,
    FeatureFrame,
    StrategyContractError,
    StrategySpecV3Error,
    build_frames_v3,
    capability_payload,
    compile_strategy,
    compile_strategy_v3,
)
from test_strategy_kernel import REVISION, compile_spec, make_spec  # noqa: E402

H4 = 14400


def _feature(key: str, lag: int = 0) -> dict:
    return {"node": "feature", "key": key, "lag_bars": lag}


def _stream(leg: str, field: str = "close", lag: int = 0) -> dict:
    return {"node": "stream", "leg": leg, "field": field, "lag_bars": lag}


def _dec(value: str) -> dict:
    return {"node": "literal", "value_type": "decimal", "value": value}


def _ratio() -> dict:
    return {
        "key": "ratio",
        "kind": "derived",
        "expr": {"node": "arithmetic", "op": "div", "args": [_stream("a"), _stream("b")]},
        "interval": "4h",
        "value_kind": "price",
        "output_type": "decimal",
    }


def _primitive(key: str, primitive: str, source: str, params: dict) -> dict:
    return {
        "key": key,
        "primitive": primitive,
        "primitive_version": "1",
        "source_stream": source,
        "interval": "4h",
        "value_kind": "level",
        "output_type": "decimal",
        "params": params,
        "required": True,
    }


def _derived(key: str, expr: dict, output_type: str = "decimal") -> dict:
    return {
        "key": key,
        "kind": "derived",
        "expr": expr,
        "interval": "4h",
        "value_kind": "level",
        "output_type": output_type,
    }


def _cross(op: str) -> dict:
    return {
        "node": "cross",
        "op": op,
        "left": _feature("ratio_sma_fast"),
        "right": _feature("ratio_sma_slow"),
    }


def example_spec() -> dict:
    """SPEC §2.7 ``sma_cross_basic`` spec, verbatim except ``take_profit.value``:
    §2.7 prints ``"0.10"``, which §0.6/§2.5 (canonical Decimal) reject — the
    canonical form ``"0.1"`` is used here pending the SPEC fix."""
    return {
        "schema": "cutie.strategy_spec.v3",
        "strategy_family": "basket_ratio_sma_cross",
        "market": {
            "market_type": "futures",
            "exchange": "binance",
            "timeframe": "4h",
            "legs": [
                {"leg_id": "a", "symbol": "ETHUSDT", "side": "long", "weight": "1"},
                {"leg_id": "b", "symbol": "BTCUSDT", "side": "short", "weight": "1"},
            ],
        },
        "parameters": [],
        "features": [
            _ratio(),
            _derived(
                "ratio_sma_fast",
                {
                    "node": "arithmetic",
                    "op": "div",
                    "args": [_feature("ratio_sum_fast"), _dec("5")],
                },
            ),
            _derived(
                "ratio_sma_slow",
                {
                    "node": "arithmetic",
                    "op": "div",
                    "args": [_feature("ratio_sum_slow"), _dec("20")],
                },
            ),
            _primitive("ratio_sum_fast", "rolling_sum", "feature:ratio", {"window_bars": 5}),
            _primitive("ratio_sum_slow", "rolling_sum", "feature:ratio", {"window_bars": 20}),
        ],
        "entry": {
            "condition": _cross("crosses_above"),
            "order_model": "next_bar_open",
            "cooldown_bars": 6,
        },
        "exit": {
            "stop_loss": {"model": "basket_pnl_pct", "value": "0.05"},
            "take_profit": {"model": "basket_pnl_pct", "value": "0.1"},
            "time_exit_bars": None,
            "signal_exit": _cross("crosses_below"),
        },
        "risk": {
            "position_sizing": {"model": "fixed_margin_per_leg", "value": "1000"},
            "max_open_positions": 1,
            "allow_pyramiding": False,
            "leverage": "3",
        },
        "execution": {
            "decision_clock": "closed_bar",
            "signal_effective_at": "next_bar_open",
            "intrabar_priority": ["stop_loss", "take_profit", "time_exit", "signal_exit"],
            "position_mode": "one_way",
            "cost_model": {
                "schema": "cutie.execution_cost.v1",
                "fee_bps": "10",
                "slippage_bps": "5",
                "funding": "excluded",
            },
            "missing_data_policy": "skip_unaligned_bar",
            "kernel_api_version": "1",
        },
    }


def zscore_spec(window: int = 8) -> dict:
    """§6.2 ``basket_ratio_zscore`` shape: ``ratio_z`` over ``feature:ratio``."""
    spec = example_spec()
    spec["strategy_family"] = "basket_ratio_zscore"
    spec["features"] = [
        _ratio(),
        _primitive("ratio_z", "rolling_zscore", "feature:ratio", {"window_bars": window}),
    ]
    spec["entry"]["condition"] = {
        "node": "compare",
        "op": "lt",
        "left": _feature("ratio_z"),
        "right": _dec("-2"),
    }
    spec["exit"]["signal_exit"] = {
        "node": "compare",
        "op": "gt",
        "left": _feature("ratio_z"),
        "right": _dec("-0.5"),
    }
    return spec


def roc_spec(window: int = 3) -> dict:
    """§6.2 ``basket_ratio_roc`` shape: derived with a lagged feature node."""
    spec = example_spec()
    spec["strategy_family"] = "basket_ratio_roc"
    lagged = _feature("ratio", window)
    spec["features"] = [
        _ratio(),
        _derived(
            "ratio_roc",
            {
                "node": "arithmetic",
                "op": "div",
                "args": [
                    {"node": "arithmetic", "op": "sub", "args": [_feature("ratio"), lagged]},
                    lagged,
                ],
            },
        ),
    ]
    spec["entry"]["condition"] = {
        "node": "compare",
        "op": "gt",
        "left": _feature("ratio_roc"),
        "right": _dec("0.02"),
    }
    spec["exit"]["signal_exit"] = {
        "node": "compare",
        "op": "lt",
        "left": _feature("ratio_roc"),
        "right": _dec("0.01"),
    }
    return spec


def _rows(closes: list[str], start: int = 0) -> list[dict]:
    return [
        {
            "open_time": (start + index) * H4,
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": "1",
        }
        for index, close in enumerate(closes)
    ]


# ---------------------------------------------------------------- compile ok


def test_spec_example_2_7_compiles_with_topological_feature_order():
    spec = example_spec()
    plan = compile_strategy_v3(spec)
    assert plan.spec_hash == canonical_json_sha256(spec)
    assert plan.basket_key == "ETHUSDT~BTCUSDT"
    assert [leg["leg_id"] for leg in plan.legs] == ["a", "b"]
    # derived sma features reference primitives sorted after them (§2.7 note)
    assert plan.feature_order == (
        "ratio",
        "ratio_sum_fast",
        "ratio_sum_slow",
        "ratio_sma_fast",
        "ratio_sma_slow",
    )
    assert set(plan.feature_types.values()) == {"decimal"}


@pytest.mark.parametrize("builder", [zscore_spec, roc_spec])
def test_other_basket_families_compile(builder):
    plan = compile_strategy_v3(builder())
    assert plan.basket_key == "ETHUSDT~BTCUSDT"


def test_primitive_may_read_a_leg_kline_price_directly():
    spec = zscore_spec()
    spec["features"] = [
        _ratio(),
        {
            **_primitive("ratio_z", "rolling_zscore", "kline.leg.a.close", {"window_bars": 4}),
            "value_kind": "price",
        },
    ]
    compile_strategy_v3(spec)


# ------------------------------------------------------------ v2 / v3 mutual


def test_v3_entry_rejects_v2_schema_as_unknown():
    with pytest.raises(StrategySpecV3Error) as exc:
        compile_strategy_v3(make_spec())
    assert exc.value.code == ERR_SPEC_INVALID
    assert exc.value.path == "$.strategy_spec.schema"
    assert exc.value.detail()["schema"] == STRATEGY_SPEC_V3_SCHEMA


def test_v2_compiler_rejects_v3_schema_as_unknown():
    with pytest.raises(StrategyContractError) as exc:
        compile_strategy(example_spec(), {}, capability_payload(REVISION))
    assert exc.value.code == ERR_SPEC_INVALID
    assert exc.value.path == "$.strategy_spec.schema"
    assert exc.value.actual == STRATEGY_SPEC_V3_SCHEMA


def test_v2_spec_naming_rolling_zscore_stays_capability_unsupported():
    feature = {
        "key": "cvd_z",
        "primitive": "rolling_zscore",
        "primitive_version": "1",
        "source_stream": "coinglass.futures_cvd",
        "interval": "1h",
        "value_kind": "flow",
        "output_type": "decimal",
        "params": {"window_bars": 5},
        "required": True,
    }
    condition = {
        "node": "compare",
        "op": "gt",
        "left": _feature("cvd_z"),
        "right": _dec("1"),
    }
    with pytest.raises(StrategyContractError) as exc:
        compile_spec(make_spec(condition=condition, features=[feature]))
    assert exc.value.code == ERR_SPEC_UNSUPPORTED
    assert exc.value.path.startswith("$.artifact_manifest.capability_requirements.features")


def test_capability_payload_never_mentions_v3():
    payload = canonical_json(capability_payload(REVISION))
    assert "v3" not in payload
    assert "rolling_zscore" not in payload


def test_v2_feature_frame_serialization_is_unchanged():
    frame = FeatureFrame(0, H4, H4, "BTCUSDT", {"close": "1"}, {})
    assert set(frame.as_dict()) == {
        "bar_open_at",
        "bar_close_at",
        "available_at",
        "symbol",
        "values",
        "stream_revisions",
    }


# ------------------------------------------------------ §2 invalid, one each


def _set(path: tuple, value):
    def mutate(spec: dict) -> None:
        target = spec
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = value

    return mutate


def _delete(path: tuple):
    def mutate(spec: dict) -> None:
        target = spec
        for part in path[:-1]:
            target = target[part]
        del target[path[-1]]

    return mutate


def _add_leg(spec: dict) -> None:
    spec["market"]["legs"].append(
        {"leg_id": "c", "symbol": "SOLUSDT", "side": "long", "weight": "1"}
    )


def _swap_legs(spec: dict) -> None:
    spec["market"]["legs"].reverse()


def _derived_cycle(spec: dict) -> None:
    spec["features"][1]["expr"]["args"][0] = _feature("ratio_sma_slow")
    spec["features"][2]["expr"]["args"][0] = _feature("ratio_sma_fast")


def _forward_primitive_ref(spec: dict) -> None:
    spec["features"][3]["source_stream"] = "feature:ratio_sum_slow"


def _stream_in_entry(spec: dict) -> None:
    spec["entry"]["condition"]["left"] = _stream("a")


def _unsorted_features(spec: dict) -> None:
    features = spec["features"]
    features[1], features[2] = features[2], features[1]


INVALID_CASES = [
    # §2.1 top level
    ("top_extra_key", _set(("extra",), 1), "$.strategy_spec", ERR_SPEC_INVALID),
    ("top_missing_key", _delete(("parameters",)), "$.strategy_spec", ERR_SPEC_INVALID),
    ("family", _set(("strategy_family",), "ema_cross"), "$.strategy_spec.strategy_family", ERR_SPEC_INVALID),
    ("parameters_nonempty", _set(("parameters",), [{"key": "x"}]), "$.strategy_spec.parameters", ERR_SPEC_INVALID),
    ("float_value", _set(("entry", "cooldown_bars"), 6.0), "$", ERR_SPEC_INVALID),
    # §2.2 market
    ("market_v2_symbols", _set(("market", "symbols"), ["ETHUSDT"]), "$.strategy_spec.market", ERR_SPEC_INVALID),
    ("market_type_spot", _set(("market", "market_type"), "spot"), "$.strategy_spec.market.market_type", ERR_SPEC_INVALID),
    ("timeframe_15m", _set(("market", "timeframe"), "15m"), "$.strategy_spec.market.timeframe", ERR_SPEC_INVALID),
    ("exchange_key", _set(("market", "exchange"), "Binance!"), "$.strategy_spec.market.exchange", ERR_SPEC_INVALID),
    ("legs_three", _add_leg, "$.strategy_spec.market.legs", ERR_SPEC_INVALID),
    ("legs_one", lambda s: s["market"]["legs"].pop(), "$.strategy_spec.market.legs", ERR_SPEC_INVALID),
    ("legs_unsorted", _swap_legs, "$.strategy_spec.market.legs[0].leg_id", ERR_SPEC_INVALID),
    ("leg_id_c", _set(("market", "legs", 1, "leg_id"), "c"), "$.strategy_spec.market.legs[1].leg_id", ERR_SPEC_INVALID),
    ("leg_extra_key", _set(("market", "legs", 0, "exchange"), "binance"), "$.strategy_spec.market.legs[0]", ERR_SPEC_INVALID),
    ("leg_side", _set(("market", "legs", 0, "side"), "both"), "$.strategy_spec.market.legs[0].side", ERR_SPEC_INVALID),
    ("leg_weight", _set(("market", "legs", 0, "weight"), "2"), "$.strategy_spec.market.legs[0].weight", ERR_SPEC_INVALID),
    ("leg_same_symbol", _set(("market", "legs", 1, "symbol"), "ETHUSDT"), "$.strategy_spec.market.legs", ERR_SPEC_INVALID),
    ("leg_not_usdt", _set(("market", "legs", 1, "symbol"), "ETHBTC"), "$.strategy_spec.market.legs[1].symbol", ERR_SPEC_INVALID),
    ("basket_key_over_30", _set(("market", "legs", 0, "symbol"), "ABCDEFGHIJKLMNOPQRSTUSDT"), "$.strategy_spec.market.legs", ERR_SPEC_INVALID),
    # §2.3 features
    ("features_unsorted", _unsorted_features, "$.strategy_spec.features", ERR_SPEC_INVALID),
    ("derived_missing_key", _delete(("features", 0, "value_kind")), "$.strategy_spec.features[0]", ERR_SPEC_INVALID),
    ("derived_kind", _set(("features", 0, "kind"), "primitive"), "$.strategy_spec.features[0].kind", ERR_SPEC_INVALID),
    ("derived_interval", _set(("features", 0, "interval"), "1h"), "$.strategy_spec.features[0].interval", ERR_SPEC_INVALID),
    ("derived_output_type", _set(("features", 0, "output_type"), "integer"), "$.strategy_spec.features[0].output_type", ERR_SPEC_INVALID),
    ("derived_unknown_feature", _set(("features", 1, "expr", "args", 0), _feature("nope")), "$.strategy_spec.features[1].expr.args[0].key", ERR_SPEC_INVALID),
    ("derived_cycle", _derived_cycle, "$.strategy_spec.features", ERR_SPEC_INVALID),
    ("primitive_forward_ref", _forward_primitive_ref, "$.strategy_spec.features[3].source_stream", ERR_SPEC_INVALID),
    ("primitive_unknown_ref", _set(("features", 3, "source_stream"), "feature:nope"), "$.strategy_spec.features[3].source_stream", ERR_SPEC_INVALID),
    ("primitive_v2_source", _set(("features", 3, "source_stream"), "kline.primary.close"), "$.strategy_spec.features[3].source_stream", ERR_SPEC_INVALID),
    ("primitive_bad_kline_leg", _set(("features", 3, "source_stream"), "kline.leg.c.close"), "$.strategy_spec.features[3].source_stream", ERR_SPEC_INVALID),
    ("primitive_unknown", _set(("features", 3, "primitive"), "ema"), "$.strategy_spec.features[3].primitive", ERR_SPEC_INVALID),
    ("primitive_version", _set(("features", 3, "primitive_version"), "2"), "$.strategy_spec.features[3].primitive_version", ERR_SPEC_INVALID),
    ("primitive_required_false", _set(("features", 3, "required"), False), "$.strategy_spec.features[3].required", ERR_SPEC_INVALID),
    ("primitive_output_integer", _set(("features", 3, "output_type"), "integer"), "$.strategy_spec.features[3].output_type", ERR_SPEC_INVALID),
    ("primitive_params", _set(("features", 3, "params"), {"window_bars": 5, "x": 1}), "$.strategy_spec.features[3].params", ERR_SPEC_INVALID),
    ("primitive_interval", _set(("features", 3, "interval"), "1d"), "$.strategy_spec.features[3].interval", ERR_SPEC_INVALID),
    # §2.4 ValueExpr stream node
    ("stream_unknown_leg", _set(("features", 0, "expr", "args", 0), _stream("c")), "$.strategy_spec.features[0].expr.args[0].leg", ERR_SPEC_INVALID),
    ("stream_field", _set(("features", 0, "expr", "args", 0), _stream("a", "vwap")), "$.strategy_spec.features[0].expr.args[0].field", ERR_SPEC_INVALID),
    ("stream_lag", _set(("features", 0, "expr", "args", 0), _stream("a", lag=10001)), "$.strategy_spec.features[0].expr.args[0].lag_bars", ERR_SPEC_INVALID),
    ("stream_extra_key", _set(("features", 0, "expr", "args", 0), {**_stream("a"), "symbol": "ETHUSDT"}), "$.strategy_spec.features[0].expr.args[0]", ERR_SPEC_INVALID),
    ("stream_in_condition", _stream_in_entry, "$.strategy_spec.entry.condition.left", ERR_SPEC_INVALID),
    # §2.5 entry
    ("entry_v2_side", _set(("entry", "side"), "long"), "$.strategy_spec.entry", ERR_SPEC_INVALID),
    ("entry_order_model", _set(("entry", "order_model"), "same_bar_close"), "$.strategy_spec.entry.order_model", ERR_SPEC_INVALID),
    ("entry_cooldown", _set(("entry", "cooldown_bars"), -1), "$.strategy_spec.entry.cooldown_bars", ERR_SPEC_INVALID),
    # §2.5 exit
    ("exit_v2_model", _set(("exit", "stop_loss", "model"), "fixed_percent"), "$.strategy_spec.exit.stop_loss.model", ERR_SPEC_UNSUPPORTED),
    ("exit_unknown_model", _set(("exit", "take_profit", "model"), "trailing"), "$.strategy_spec.exit.take_profit.model", ERR_SPEC_INVALID),
    ("exit_value_ge_1", _set(("exit", "take_profit", "value"), "1"), "$.strategy_spec.exit.take_profit.value", ERR_SPEC_INVALID),
    ("exit_value_zero", _set(("exit", "stop_loss", "value"), "0"), "$.strategy_spec.exit.stop_loss.value", ERR_SPEC_INVALID),
    ("exit_value_noncanonical", _set(("exit", "take_profit", "value"), "0.10"), "$.strategy_spec.exit.take_profit.value", ERR_SPEC_INVALID),
    ("exit_time_zero", _set(("exit", "time_exit_bars"), 0), "$.strategy_spec.exit.time_exit_bars", ERR_SPEC_INVALID),
    ("exit_missing_key", _delete(("exit", "signal_exit")), "$.strategy_spec.exit", ERR_SPEC_INVALID),
    # §2.5 risk
    ("risk_v2_sizing", _set(("risk", "position_sizing", "model"), "fixed_margin"), "$.strategy_spec.risk.position_sizing.model", ERR_SPEC_INVALID),
    ("risk_sizing_value", _set(("risk", "position_sizing", "value"), "0"), "$.strategy_spec.risk.position_sizing.value", ERR_SPEC_INVALID),
    ("risk_max_open", _set(("risk", "max_open_positions"), 2), "$.strategy_spec.risk.max_open_positions", ERR_SPEC_INVALID),
    ("risk_max_open_bool", _set(("risk", "max_open_positions"), True), "$.strategy_spec.risk.max_open_positions", ERR_SPEC_INVALID),
    ("risk_pyramiding", _set(("risk", "allow_pyramiding"), True), "$.strategy_spec.risk.allow_pyramiding", ERR_SPEC_INVALID),
    ("risk_leverage_4", _set(("risk", "leverage"), "4"), "$.strategy_spec.risk.leverage", ERR_SPEC_INVALID),
    ("risk_leverage_fraction", _set(("risk", "leverage"), "1.5"), "$.strategy_spec.risk.leverage", ERR_SPEC_INVALID),
    # §2.5 execution
    ("exec_missing_data_fail", _set(("execution", "missing_data_policy"), "fail"), "$.strategy_spec.execution.missing_data_policy", ERR_SPEC_INVALID),
    ("exec_priority", _set(("execution", "intrabar_priority"), ["stop_loss", "take_profit", "time_exit"]), "$.strategy_spec.execution.intrabar_priority", ERR_SPEC_INVALID),
    ("exec_clock", _set(("execution", "decision_clock"), "tick"), "$.strategy_spec.execution", ERR_SPEC_INVALID),
    ("exec_position_mode", _set(("execution", "position_mode"), "hedge"), "$.strategy_spec.execution.position_mode", ERR_SPEC_INVALID),
    ("exec_funding_included", _set(("execution", "cost_model", "funding"), "included"), "$.strategy_spec.execution.cost_model.funding", ERR_SPEC_UNSUPPORTED),
    ("exec_fee_negative", _set(("execution", "cost_model", "fee_bps"), "-1"), "$.strategy_spec.execution.cost_model.fee_bps", ERR_SPEC_INVALID),
    ("exec_kernel_api", _set(("execution", "kernel_api_version"), "2"), "$.strategy_spec.execution.kernel_api_version", ERR_SPEC_INVALID),
]


@pytest.mark.parametrize(
    "mutate,path,code",
    [case[1:] for case in INVALID_CASES],
    ids=[case[0] for case in INVALID_CASES],
)
def test_section_2_invalid_spec_is_rejected(mutate, path, code):
    spec = example_spec()
    mutate(spec)
    with pytest.raises(StrategySpecV3Error) as exc:
        compile_strategy_v3(spec)
    assert exc.value.path == path
    assert exc.value.code == code


@pytest.mark.parametrize(
    "params",
    [{"window_bars": 1}, {"window_bars": 10001}, {"window_bars": 5, "min_periods": 2}],
)
def test_rolling_zscore_params_are_exactly_window_bars_from_2(params):
    spec = zscore_spec()
    spec["features"][1]["params"] = params
    with pytest.raises(StrategySpecV3Error) as exc:
        compile_strategy_v3(spec)
    assert exc.value.path.startswith("$.strategy_spec.features[1].params")


def test_compile_does_not_alias_caller_spec():
    spec = example_spec()
    plan = compile_strategy_v3(spec)
    spec["market"]["legs"][0]["symbol"] = "SOLUSDT"
    assert plan.strategy_spec["market"]["legs"][0]["symbol"] == "ETHUSDT"
    assert plan.legs[0]["symbol"] == "ETHUSDT"


# ----------------------------------------------------------- rolling_zscore


def test_rolling_zscore_population_value_on_ratio():
    closes = ["2", "4", "4", "4", "5", "5", "7", "9", "1"]
    frames = build_frames_v3(
        {"a": _rows(closes), "b": _rows(["1"] * len(closes))},
        compile_strategy_v3(zscore_spec(window=8)),
    )
    values = [frame.values["ratio_z"] for frame in frames.frames]
    assert values[:7] == [None] * 7
    # window 2,4,4,4,5,5,7,9: mean 5, population stdev 2 -> (9-5)/2
    assert values[7] == "2"
    # window 4,4,4,5,5,7,9,1: mean 39/8, var 311/64 -> (1-39/8)/sqrt(311/64)
    with localcontext() as ctx:
        ctx.prec = 34
        expected = (Decimal(1) - Decimal(39) / 8) / (Decimal(311) / 64).sqrt()
    assert Decimal(values[8]) == expected
    assert values[8].startswith("-1.757")
    reasons = {gap["reason"] for gap in frames.feature_gaps if gap["feature"] == "ratio_z"}
    assert reasons == {"insufficient_history"}


def test_rolling_zscore_zero_stdev_is_a_gap_not_zero():
    frames = build_frames_v3(
        {"a": _rows(["3"] * 6 + ["4"]), "b": _rows(["1"] * 7)},
        compile_strategy_v3(zscore_spec(window=4)),
    )
    values = [frame.values["ratio_z"] for frame in frames.frames]
    assert values[3:6] == [None, None, None]
    assert values[6] is not None and Decimal(values[6]) > 0
    zero = [
        gap["bar_open_at"]
        for gap in frames.feature_gaps
        if gap["feature"] == "ratio_z" and gap["reason"] == "zero_stdev"
    ]
    assert zero == [3 * H4, 4 * H4, 5 * H4]


# -------------------------------------------------------- multi-leg frames


def test_unaligned_bar_is_marked_skipped_and_lookback_counts_aligned_bars():
    plan = compile_strategy_v3(roc_spec(window=1))
    leg_b = _rows(["1", "1", "1", "1", "1"])
    del leg_b[2]
    frames = build_frames_v3(
        {"a": _rows(["100", "110", "120", "132", "145.2"]), "b": leg_b}, plan
    )
    assert frames.skipped_bars == 1
    assert frames.unaligned_bars == [{"bar_open_at": 2 * H4, "missing_legs": ["b"]}]
    assert [frame.skipped for frame in frames.frames] == [False, False, True, False, False]
    gap = frames.frames[2]
    assert gap.values == {}
    assert set(gap.legs) == {"a"}
    assert gap.as_dict()["skipped"] is True
    aligned = frames.frames[3]
    assert aligned.symbol == "ETHUSDT~BTCUSDT"
    assert aligned.legs == {
        "a": {"open": "132", "high": "132", "low": "132", "close": "132", "volume": "1"},
        "b": {"open": "1", "high": "1", "low": "1", "close": "1", "volume": "1"},
    }
    # lag 1 on the bar after the gap reads the previous *aligned* bar (110)
    assert aligned.values["ratio_roc"] == "0.2"
    assert frames.frames[1].values["ratio_roc"] == "0.1"
    assert frames.frames[0].values["ratio_roc"] is None


def test_example_frames_are_decimal_strings_and_warmup_is_structured():
    closes_a = [str(2000 + 10 * index) for index in range(25)]
    closes_b = [str(40000 + 100 * index) for index in range(25)]
    frames = build_frames_v3(
        {"a": _rows(closes_a), "b": _rows(closes_b)}, compile_strategy_v3(example_spec())
    )
    last = frames.frames[-1].values
    assert all(isinstance(value, str) for value in last.values())
    with localcontext() as ctx:
        ctx.prec = 34
        ratio = Decimal(closes_a[-1]) / Decimal(closes_b[-1])
    assert Decimal(last["ratio"]) == ratio
    assert frames.frames[18].values["ratio_sma_slow"] is None
    assert frames.frames[19].values["ratio_sma_slow"] is not None
    assert frames.skipped_bars == 0


def test_leading_bars_of_a_late_leg_are_unaligned():
    plan = compile_strategy_v3(roc_spec(window=1))
    frames = build_frames_v3(
        {"a": _rows(["1", "2", "3", "4"]), "b": _rows(["1", "1"], start=2)}, plan
    )
    assert [gap["bar_open_at"] for gap in frames.unaligned_bars] == [0, H4]
    assert frames.skipped_bars == 2


def test_leg_set_and_grid_must_match():
    plan = compile_strategy_v3(roc_spec())
    with pytest.raises(StrategyContractError) as exc:
        build_frames_v3({"a": _rows(["1"])}, plan)
    assert exc.value.code == ERR_COVERAGE_INCOMPLETE
    off_grid = _rows(["1", "1"])
    off_grid[1]["open_time"] = H4 + 60
    with pytest.raises(StrategyContractError) as exc:
        build_frames_v3({"a": _rows(["1", "1"]), "b": off_grid}, plan)
    assert exc.value.code == ERR_COVERAGE_INCOMPLETE
    unsorted = _rows(["1", "1"])
    unsorted.reverse()
    with pytest.raises(StrategyContractError) as exc:
        build_frames_v3({"a": _rows(["1", "1"]), "b": unsorted}, plan)
    assert exc.value.code == ERR_COVERAGE_INCOMPLETE


def spread_inverse_spec() -> dict:
    """Derived ``a.close / (a.close - b.close)``: valid positive prices still
    divide by zero on a bar where both legs close equal."""
    spec = example_spec()
    spec["features"] = [
        _derived(
            "spread_inv",
            {
                "node": "arithmetic",
                "op": "div",
                "args": [
                    _stream("a"),
                    {"node": "arithmetic", "op": "sub", "args": [_stream("a"), _stream("b")]},
                ],
            },
        )
    ]
    spec["entry"]["condition"] = {
        "node": "compare",
        "op": "gt",
        "left": _feature("spread_inv"),
        "right": _dec("0"),
    }
    spec["exit"]["signal_exit"] = None
    return spec


def test_derived_arithmetic_failure_is_a_structured_error_not_a_gap():
    # 62-2 §3.5 (v2 ``_ctx_op``): division by zero is a structured execution
    # failure, never a gap that silently yields zero trades.
    with pytest.raises(StrategyContractError) as exc:
        build_frames_v3(
            {"a": _rows(["2", "1"]), "b": _rows(["1", "1"])},
            compile_strategy_v3(spread_inverse_spec()),
        )
    assert exc.value.code == ERR_SPEC_INVALID
    assert exc.value.path == "$.features.spread_inv"
    assert exc.value.actual == H4
