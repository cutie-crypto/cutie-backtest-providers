"""JES21-B3b 二期 K1b: two v2 kernel openings.

1. kline.primary passthrough history primitives (rsi_wilder / ema / macd /
   rolling_zscore): the primary array's first ``warmup_bars`` rows are warmup
   frames and may leave the feature unset; from the first decision frame on
   the value is required and a gap still fails closed.
2. ``exit.stop_loss`` / ``exit.take_profit`` may be ``null`` (that exit is not
   armed); both keys stay mandatory and stop/take/signal/time all empty is
   rejected.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import _v2c_reference as ref  # noqa: E402
import test_strategy_kernel as tsk  # noqa: E402
import test_strategy_kernel_primitives as tkp  # noqa: E402
from canonical_json import canonical_json_sha256  # noqa: E402
from strategy_kernel import (  # noqa: E402
    ERR_CAPABILITY_MISMATCH,
    ERR_COVERAGE_INCOMPLETE,
    ERR_SPEC_INVALID,
    ERR_SPEC_UNSUPPORTED,
    EXIT_CAPABILITY_NULLABLE_STOP_TAKE,
    CompiledPlan,
    StrategyContractError,
    build_frames,
    capability_payload,
    compile_strategy,
    initial_state,
    primitive_lookback_bars,
    simulate,
)

PRIMARY_STREAM = "binance.futures.kline.1h"
COVERAGE = {
    "summary": {"strict_eligible": True},
    "request_identity": {"symbol": "BTCUSDT"},
}
CLOSES = [
    "100", "101", "103", "102", "105", "104", "107", "106", "109", "108",
    "111", "110", "113", "112", "115", "114", "117", "116", "119", "118",
    "121", "120", "123", "122", "125", "124", "127", "126", "129", "128",
]


def _primary_rows(closes: list[str]) -> list[dict]:
    rows = []
    for index, close in enumerate(closes):
        rows.append(
            {
                "open_time": index * 3600,
                "open": close,
                "high": str(int(close) + 5),
                "low": str(int(close) - 5),
                "close": close,
                "volume": "1",
            }
        )
    return rows


def _feature(key: str, primitive: str, params: dict) -> dict:
    return {
        "key": key,
        "primitive": primitive,
        "primitive_version": "1",
        "source_stream": "kline.primary.close",
        "interval": "1h",
        "value_kind": "price",
        "output_type": "decimal",
        "params": params,
        "required": True,
    }


def _passthrough_manifest(spec: dict, warmup_bars: int) -> dict:
    manifest = tkp._kline_primary_manifest(spec, requirement_interval="1h")
    primary = next(
        item
        for item in manifest["data_requirements"]
        if item["execution_role"] == "primary_execution_kline"
    )
    primary["warmup_bars"] = warmup_bars
    return manifest


def _warmup_for(primitive: str, params: dict) -> int:
    if primitive == "rsi_wilder":
        return 10 * params["period"]
    return primitive_lookback_bars(primitive, params)


HISTORY_CASES = [
    ("rsi_wilder", {"period": 2}),
    ("ema", {"window_bars": 3}),
    ("macd", {"fast": 2, "slow": 4, "signal": 3, "output": "signal"}),
    ("rolling_zscore", {"window_bars": 4}),
]


@pytest.mark.parametrize(("primitive", "params"), HISTORY_CASES)
def test_passthrough_history_primitive_warmup_rows_may_be_unset(
    primitive: str, params: dict
) -> None:
    warmup = _warmup_for(primitive, params)
    spec = tsk.make_spec(features=[_feature("f", primitive, params)])
    plan = compile_strategy(
        spec, _passthrough_manifest(spec, warmup), capability_payload(tsk.REVISION)
    )
    closes = CLOSES[: warmup + 5]
    frames = build_frames({PRIMARY_STREAM: _primary_rows(closes)}, COVERAGE, plan)
    assert len(frames) == len(closes)
    # The array's first row can never resolve a history primitive.
    assert "f" not in frames[0].values
    # Every decision frame (row >= warmup_bars) carries the value.
    assert all("f" in frame.values for frame in frames[warmup:])
    if primitive != "rsi_wilder":
        expected = {
            "ema": lambda: ref.ema_reference(closes, params["window_bars"]),
            "macd": lambda: ref.macd_reference(
                closes,
                params["fast"],
                params["slow"],
                params["signal"],
                params["output"],
            ),
            "rolling_zscore": lambda: ref.zscore_reference(
                closes, params["window_bars"]
            ),
        }[primitive]()
        from decimal import localcontext

        from canonical_json import canonical_decimal_str

        with localcontext(ref.DECIMAL128):
            expected_strs = [
                canonical_decimal_str(v) if v is not None else None for v in expected
            ]
        assert [frame.values.get("f") for frame in frames] == expected_strs


@pytest.mark.parametrize(("primitive", "params"), HISTORY_CASES)
def test_passthrough_history_primitive_first_decision_frame_gap_fails_closed(
    primitive: str, params: dict
) -> None:
    """Bypass the compile-time warmup gate so the first decision frame
    (row == warmup_bars) cannot resolve: build_frames must fail closed on it
    instead of letting an unset value reach decision logic."""
    lookback = primitive_lookback_bars(primitive, params)
    # The value first resolves at row lookback - 1; a warmup one row shorter
    # than that makes row ``warmup`` the first decision frame without it.
    warmup = lookback - 2
    spec = tsk.make_spec(features=[_feature("f", primitive, params)])
    manifest = _passthrough_manifest(spec, warmup)
    plan = CompiledPlan(
        strategy_spec=spec,
        artifact_manifest=manifest,
        spec_hash="",
        manifest_hash="",
        artifact_hash="",
        parameter_types={},
        feature_types={"f": "decimal"},
    )
    with pytest.raises(StrategyContractError) as caught:
        build_frames({PRIMARY_STREAM: _primary_rows(CLOSES[:12])}, COVERAGE, plan)
    assert caught.value.code == ERR_COVERAGE_INCOMPLETE
    assert caught.value.required == warmup * 3600


def test_passthrough_ema_cross_replays_and_never_decides_on_warmup() -> None:
    fast = _feature("ema_fast", "ema", {"window_bars": 2})
    slow = _feature("ema_slow", "ema", {"window_bars": 4})
    condition = {
        "node": "cross",
        "op": "crosses_above",
        "left": {"node": "feature", "key": "ema_fast", "lag_bars": 0},
        "right": {"node": "feature", "key": "ema_slow", "lag_bars": 0},
    }
    spec = tsk.make_spec(
        features=[fast, slow], condition=condition, time_exit_bars=2
    )
    warmup = 4
    plan = compile_strategy(
        spec, _passthrough_manifest(spec, warmup), capability_payload(tsk.REVISION)
    )
    closes = ["100", "99", "98", "97", "96", "95", "100", "106", "104", "103",
              "102", "101", "100", "99"]
    frames = build_frames({PRIMARY_STREAM: _primary_rows(closes)}, COVERAGE, plan)
    params = tsk.execution_params()
    params["start_at"] = warmup * 3600
    params["end_at"] = len(closes) * 3600
    result = simulate(plan, frames, initial_state(plan, params))
    assert result["decisions"]
    assert all(item["frame_index"] >= warmup for item in result["decisions"])
    assert result["trades"]


# --- nullable stop_loss / take_profit ------------------------------------


def _signal_exit() -> dict:
    return {"node": "compare", "op": "lt", "left": tsk._literal("2"), "right": tsk._literal("1")}


def _null_spec(
    *,
    stop: bool = False,
    take: bool = False,
    time_exit_bars: int | None = None,
    signal_exit: dict | None = None,
    **overrides,
) -> dict:
    spec = tsk.make_spec(time_exit_bars=time_exit_bars, **overrides)
    if not stop:
        spec["exit"]["stop_loss"] = None
    if not take:
        spec["exit"]["take_profit"] = None
    spec["exit"]["signal_exit"] = signal_exit
    return spec


@pytest.mark.parametrize(
    "spec_kwargs",
    [
        {"take": True},
        {"stop": True},
        {"time_exit_bars": 3},
        {"signal_exit": "yes"},
    ],
)
def test_null_stop_take_compiles_when_another_exit_remains(spec_kwargs: dict) -> None:
    kwargs = dict(spec_kwargs)
    if kwargs.get("signal_exit") == "yes":
        kwargs["signal_exit"] = _signal_exit()
    plan = tsk.compile_spec(_null_spec(**kwargs))
    assert plan.spec_hash == canonical_json_sha256(plan.strategy_spec)


def test_all_exits_empty_is_rejected_invalid() -> None:
    with pytest.raises(StrategyContractError) as caught:
        tsk.compile_spec(_null_spec())
    assert caught.value.code == ERR_SPEC_INVALID
    assert caught.value.path == "$.strategy_spec.exit"


@pytest.mark.parametrize("missing", ["stop_loss", "take_profit"])
def test_stop_take_keys_stay_mandatory(missing: str) -> None:
    spec = _null_spec(time_exit_bars=3)
    del spec["exit"][missing]
    with pytest.raises(StrategyContractError) as caught:
        tsk.compile_spec(spec)
    assert caught.value.code == ERR_SPEC_INVALID


def test_r_multiple_take_needs_a_stop() -> None:
    spec = _null_spec(take=True, take_model="r_multiple", take_value="2")
    with pytest.raises(StrategyContractError) as caught:
        tsk.compile_spec(spec)
    assert caught.value.code == ERR_SPEC_INVALID
    assert caught.value.path == "$.strategy_spec.exit.take_profit.model"


def test_risk_fraction_sizing_needs_a_stop() -> None:
    spec = _null_spec(
        time_exit_bars=3, sizing_model="risk_fraction", sizing_value="0.01"
    )
    with pytest.raises(StrategyContractError) as caught:
        tsk.compile_spec(spec)
    assert caught.value.code == ERR_SPEC_INVALID
    assert caught.value.path == "$.strategy_spec.risk.position_sizing.model"


def test_null_exit_requires_the_declared_capability() -> None:
    capability = capability_payload(tsk.REVISION)
    assert capability["exit_capabilities"] == [EXIT_CAPABILITY_NULLABLE_STOP_TAKE]
    capability["exit_capabilities"] = []
    spec = _null_spec(time_exit_bars=3)
    with pytest.raises(StrategyContractError) as caught:
        compile_strategy(spec, tsk.make_manifest(spec), capability)
    assert caught.value.code == ERR_SPEC_UNSUPPORTED
    # A spec with both exits set does not need it.
    full = tsk.make_spec()
    compile_strategy(full, tsk.make_manifest(full), capability)
    # The key itself is part of the capability exact-set.
    del capability["exit_capabilities"]
    with pytest.raises(StrategyContractError) as caught:
        compile_strategy(full, tsk.make_manifest(full), capability)
    assert caught.value.code == ERR_CAPABILITY_MISMATCH


def test_both_null_time_exit_closes_on_time() -> None:
    plan = tsk.compile_spec(_null_spec(time_exit_bars=2))
    frames = [
        tsk.frame(0),
        tsk.frame(1, high="150", low="50", close="100"),
        tsk.frame(2, high="150", low="50", close="120"),
    ]
    result = simulate(plan, frames, initial_state(plan, tsk.execution_params()))
    trace = result["trace_trades"][0]
    assert trace["exit_kind"] == "time_exit"
    assert trace["stop_loss"] is None and trace["take_profit"] is None
    assert result["trades"][0]["exit_price"] == "120"


def test_null_stop_keeps_take_armed() -> None:
    plan = tsk.compile_spec(_null_spec(take=True))
    frames = [tsk.frame(0), tsk.frame(1, high="106", low="10", close="101")]
    result = simulate(plan, frames, initial_state(plan, tsk.execution_params()))
    trace = result["trace_trades"][0]
    assert trace["exit_kind"] == "take_profit"
    assert trace["stop_loss"] is None


def test_null_take_keeps_stop_armed() -> None:
    plan = tsk.compile_spec(_null_spec(stop=True))
    frames = [tsk.frame(0), tsk.frame(1, high="500", low="94", close="101")]
    result = simulate(plan, frames, initial_state(plan, tsk.execution_params()))
    trace = result["trace_trades"][0]
    assert trace["exit_kind"] == "stop_loss"
    assert trace["take_profit"] is None


# --- existing spec hashes are unchanged ------------------------------------

# Pinned at the pre-K1b kernel (3092c01) for specs that keep both exits set.
PINNED_SPEC_HASHES = {
    "default": "c04f462355d5b2d6491cbc8ac2ddbeb82df426f0072deaa0fec9155a2f7d51c9",
    "time_exit": "630f077412195d0b68a7dfbfe9b0135b0bfbdbcfc1e6d60490d128ecb999ab0d",
    "short_r_multiple": "dbbd55fb2aa990b4c19a01ad060407e526d5663c340302b1a969b45e81fd007c",
}


def _pinned_specs() -> dict[str, dict]:
    return {
        "default": tsk.make_spec(),
        "time_exit": tsk.make_spec(time_exit_bars=3),
        "short_r_multiple": tsk.make_spec(
            side="short", take_model="r_multiple", take_value="2"
        ),
    }


@pytest.mark.parametrize("name", sorted(PINNED_SPEC_HASHES))
def test_existing_spec_hash_unchanged(name: str) -> None:
    spec = _pinned_specs()[name]
    plan = tsk.compile_spec(copy.deepcopy(spec))
    assert plan.spec_hash == PINNED_SPEC_HASHES[name]


def test_existing_artifact_request_hashes_unchanged() -> None:
    request = tsk.build_request(tsk.make_spec())
    assert request["artifact"]["spec_hash"] == PINNED_SPEC_HASHES["default"]
    assert request["artifact"]["artifact_hash"] == "9c2847df9681e02463730495d933b4f0a6a145a480559a49909c3eb538ef7c36"
