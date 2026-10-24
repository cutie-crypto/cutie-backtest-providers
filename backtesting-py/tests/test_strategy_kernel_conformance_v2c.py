"""v2c conformance golden (JES21-B3b 二期 K1): ``ema`` / ``macd`` /
``rolling_zscore`` in StrategySpec v2.

The fixture is the single cross-repo source the server (S1) replays; its
expected values come from ``_v2c_reference`` (no kernel import) and are
cross-checked against pandas here, independently of the kernel tests.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import _v2c_reference as ref  # noqa: E402
from canonical_json import canonical_json_sha256  # noqa: E402

FIXTURE = json.loads(ref.FIXTURE.read_text(encoding="utf-8"))
CASES = {case["name"]: case for case in FIXTURE["cases"]}


def test_fixture_schema_and_sha256_cover_the_cases() -> None:
    assert FIXTURE["schema"] == "cutie.strategy_kernel_conformance.v2c"
    assert set(FIXTURE) == {"schema", "source_of_truth", "note", "sha256", "cases"}
    assert FIXTURE["sha256"] == canonical_json_sha256(FIXTURE["cases"])
    assert {case["primitive"] for case in FIXTURE["cases"]} == {
        "ema",
        "macd",
        "rolling_zscore",
    }


def test_fixture_is_exactly_what_the_reference_builds() -> None:
    assert FIXTURE == ref.build_fixture()


@pytest.mark.parametrize("name", sorted(CASES))
def test_golden_matches_pandas_float_reference(name: str) -> None:
    case = CASES[name]
    expected = [row["value"] for row in case["expected"]]
    floats = ref.pandas_reference(case)
    assert [value is None for value in expected] == [value is None for value in floats]
    for golden, approx in zip(expected, floats):
        if golden is not None:
            assert float(golden) == pytest.approx(approx, rel=1e-12, abs=1e-12)


def test_boundary_coverage_is_present() -> None:
    """warmup 不足 / 缺口 / 常数序列 (stdev 0) each have a case per primitive."""
    for primitive in ("ema", "macd", "rolling_zscore"):
        names = [n for n, c in CASES.items() if c["primitive"] == primitive]
        assert any("gap" in n for n in names), primitive
        assert any("constant" in n for n in names), primitive
    for name in ("ema_warmup_never_reached", "macd_signal_warmup_not_reached"):
        assert all(row["value"] is None for row in CASES[name]["expected"])


# --- kernel against the golden (strategy_kernel is only imported below) ---

from decimal import localcontext  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import test_strategy_kernel as tsk  # noqa: E402
import test_strategy_kernel_primitives as tkp  # noqa: E402
from canonical_json import canonical_decimal_str  # noqa: E402
from strategy_kernel import (  # noqa: E402
    _DECIMAL_CONTEXT,
    ERR_COVERAGE_INCOMPLETE,
    ERR_SPEC_INVALID,
    ERR_SPEC_UNSUPPORTED,
    METRICS_SOURCE_STREAMS,
    FeatureFrame,
    StrategyContractError,
    StrategyKernel,
    _evaluate_windowed_primitive,
    _recursive_series_v2,
    _validate_capability,
    build_frames,
    capability_payload,
    compile_strategy,
)


def _kernel_series(case: dict) -> list:
    rows_by_ts = {
        row["ts"]: (row["value"], row["ts"] + 3600, "r1") for row in case["input"]
    }
    ordered = sorted(rows_by_ts)
    feature = {
        "key": "under_test",
        "primitive": case["primitive"],
        "params": case["params"],
        "interval": "1h",
    }
    if case["primitive"] in {"ema", "macd"}:
        series = _recursive_series_v2(feature, rows_by_ts, ordered, "$.under_test")
        values = [series[ts][0] for ts in ordered]
    else:
        values = []
        for ts in ordered:
            outcome = _evaluate_windowed_primitive(
                feature, rows_by_ts, ordered, ts, 3600, "$.under_test"
            )
            values.append(outcome[0] if outcome is not None else None)
    with localcontext(_DECIMAL_CONTEXT):
        return [canonical_decimal_str(v) if v is not None else None for v in values]


@pytest.mark.parametrize("name", sorted(CASES))
def test_kernel_matches_v2c_golden(name: str) -> None:
    case = CASES[name]
    expected = [row["value"] for row in case["expected"]]
    assert _kernel_series(case) == expected


def test_capability_declares_only_runtime_operators_and_new_primitives() -> None:
    capability = capability_payload(tsk.REVISION)
    _validate_capability(capability)
    ops = {(item["node"], item["op"]) for item in capability["operators"]}
    assert ops == {
        ("arithmetic", "add"),
        ("arithmetic", "sub"),
        ("arithmetic", "mul"),
        ("arithmetic", "div"),
        ("compare", "gt"),
        ("compare", "gte"),
        ("compare", "lt"),
        ("compare", "lte"),
        ("cross", "crosses_above"),
        ("cross", "crosses_below"),
    }
    div = next(item for item in capability["operators"] if item["op"] == "div")
    assert [sig["arg_types"] for sig in div["signatures"]] == [["decimal", "decimal"]]
    by_name = {item["primitive"]: item for item in capability["feature_primitives"]}
    for primitive in ("ema", "macd", "rolling_zscore"):
        assert by_name[primitive]["source_streams"] == [
            "kline.primary.close",
            "kline.primary.high",
            "kline.primary.low",
            "kline.primary.open",
        ]
    for primitive in ("rolling_extreme", "rolling_sum"):
        assert set(METRICS_SOURCE_STREAMS) <= set(by_name[primitive]["source_streams"])
    assert "metrics.market.fear_greed_index" in METRICS_SOURCE_STREAMS
    assert "metrics.btc.btc_dominance" not in METRICS_SOURCE_STREAMS
    assert {
        "provider": "cutie",
        "storage_source": "market_metrics_history",
        "kinds": ["feature"],
        "markets": ["futures"],
        "result_sources": [],
    } in capability["data_sources"]


def _condition_kernel(left: list[str], right: list[str]) -> tuple:
    frames = [
        FeatureFrame(
            i * 3600, (i + 1) * 3600, (i + 1) * 3600, "BTCUSDT", {"a": a, "b": b}, {}
        )
        for i, (a, b) in enumerate(zip(left, right))
    ]
    kernel = StrategyKernel.__new__(StrategyKernel)
    kernel.plan = SimpleNamespace(feature_types={"a": "decimal", "b": "decimal"})
    kernel.parameters = {}
    return kernel, SimpleNamespace(frames=frames)


def _cross(op: str) -> dict:
    return {
        "node": "cross",
        "op": op,
        "left": {"node": "feature", "key": "a", "lag_bars": 0},
        "right": {"node": "feature", "key": "b", "lag_bars": 0},
    }


def test_crosses_below_is_strict_and_touching_is_not_a_cross() -> None:
    # a: 12 -> 10 -> 9 against b=10: touching at f=1 is not a cross; the
    # strict drop below at f=2 from an equal previous bar is.
    kernel, state = _condition_kernel(["12", "10", "9"], ["10", "10", "10"])
    hits = [kernel._condition(_cross("crosses_below"), state, i) for i in range(3)]
    assert hits == [False, False, True]
    kernel, state = _condition_kernel(["8", "10", "11"], ["10", "10", "10"])
    hits = [kernel._condition(_cross("crosses_above"), state, i) for i in range(3)]
    assert hits == [False, False, True]


def test_arithmetic_sub_mul_div_with_lag_bars_evaluate() -> None:
    kernel, state = _condition_kernel(["100", "110"], ["2", "4"])
    lagged_a = {"node": "feature", "key": "a", "lag_bars": 1}
    change = {
        "node": "arithmetic",
        "op": "div",
        "args": [
            {
                "node": "arithmetic",
                "op": "sub",
                "args": [{"node": "feature", "key": "a", "lag_bars": 0}, lagged_a],
            },
            lagged_a,
        ],
    }
    assert str(kernel._value(change, state, 1)) == "0.1"
    product = {
        "node": "arithmetic",
        "op": "mul",
        "args": [
            {"node": "feature", "key": "b", "lag_bars": 0},
            {"node": "feature", "key": "b", "lag_bars": 1},
        ],
    }
    assert kernel._value(product, state, 1) == 8


def _kline_feature(primitive: str, params: dict, interval: str) -> dict:
    return {
        "key": f"{primitive}_close",
        "primitive": primitive,
        "primitive_version": "1",
        "source_stream": "kline.primary.close",
        "interval": interval,
        "value_kind": "price",
        "output_type": "decimal",
        "params": params,
        "required": True,
    }


@pytest.mark.parametrize(
    ("primitive", "params", "lookback"),
    [
        ("ema", {"window_bars": 3}, 3),
        ("rolling_zscore", {"window_bars": 4}, 4),
        ("macd", {"fast": 3, "slow": 5, "signal": 3, "output": "hist"}, 7),
    ],
)
def test_new_primitives_compile_on_coarse_kline_and_gate_warmup(
    primitive: str, params: dict, lookback: int
) -> None:
    spec = tsk.make_spec(features=[_kline_feature(primitive, params, "4h")])
    spec["market"]["timeframe"] = "1h"
    manifest = tkp._kline_primary_manifest(
        spec, requirement_interval="4h", warmup_bars=lookback - 1
    )
    with pytest.raises(StrategyContractError) as caught:
        compile_strategy(spec, manifest, capability_payload(tsk.REVISION))
    assert caught.value.required == lookback
    manifest = tkp._kline_primary_manifest(
        spec, requirement_interval="4h", warmup_bars=lookback
    )
    plan = compile_strategy(spec, manifest, capability_payload(tsk.REVISION))
    assert plan.feature_types[f"{primitive}_close"] == "decimal"


@pytest.mark.parametrize(
    ("primitive", "params"),
    [
        ("ema", {"window_bars": 3}),
        ("rolling_zscore", {"window_bars": 4}),
        ("macd", {"fast": 3, "slow": 5, "signal": 3, "output": "line"}),
    ],
)
def test_new_primitives_rejected_on_kline_primary_passthrough(
    primitive: str, params: dict
) -> None:
    spec = tsk.make_spec(features=[_kline_feature(primitive, params, "1h")])
    spec["market"]["timeframe"] = "1h"
    manifest = tkp._kline_primary_manifest(spec, requirement_interval="1h")
    with pytest.raises(StrategyContractError) as caught:
        compile_strategy(spec, manifest, capability_payload(tsk.REVISION))
    assert caught.value.code == ERR_SPEC_UNSUPPORTED


@pytest.mark.parametrize(
    "params",
    [
        {"fast": 5, "slow": 5, "signal": 3, "output": "line"},
        {"fast": 3, "slow": 5, "signal": 1, "output": "line"},
        {"fast": 3, "slow": 5, "signal": 3, "output": "bars"},
        {"fast": 3, "slow": 5, "signal": 3},
    ],
)
def test_macd_param_shape_is_closed(params: dict) -> None:
    spec = tsk.make_spec(features=[_kline_feature("macd", params, "4h")])
    spec["market"]["timeframe"] = "1h"
    manifest = tkp._kline_primary_manifest(
        spec, requirement_interval="4h", warmup_bars=50
    )
    with pytest.raises(StrategyContractError) as caught:
        compile_strategy(spec, manifest, capability_payload(tsk.REVISION))
    assert caught.value.code == ERR_SPEC_INVALID


def _metrics_spec_and_manifest(source_stream: str, interval: str) -> tuple[dict, dict]:
    feature = {
        "key": "metric_raw",
        "primitive": "rolling_extreme",
        "primitive_version": "1",
        "source_stream": source_stream,
        "interval": interval,
        "value_kind": "level",
        "output_type": "decimal",
        "params": {"window_bars": 1, "mode": "max"},
        "required": True,
    }
    spec = tsk.make_spec(features=[feature])
    spec["market"]["timeframe"] = "1h"
    manifest = tsk.make_manifest(spec)
    requirement = next(
        item for item in manifest["data_requirements"] if item["kind"] == "feature"
    )
    requirement.update(
        {
            "stream_id": f"{source_stream}.{interval}",
            "provider": "cutie",
            "storage_source": "market_metrics_history",
            "exchange": "auto",
            "interval": interval,
            "warmup_bars": 1,
        }
    )
    manifest["data_requirements"] = sorted(
        manifest["data_requirements"], key=lambda item: item["stream_id"]
    )
    manifest["capability_requirements"]["data_sources"] = sorted(
        (
            {
                "provider": item["provider"],
                "storage_source": item["storage_source"],
                "kind": item["kind"],
                "market": item["market"],
                "result_source": item["result_source"],
            }
            for item in manifest["data_requirements"]
        ),
        key=lambda item: (item["provider"], item["storage_source"], item["kind"]),
    )
    manifest["spec_hash"] = canonical_json_sha256(spec)
    return spec, manifest


def test_metrics_daily_feature_compiles_and_reads_previous_closed_day() -> None:
    spec, manifest = _metrics_spec_and_manifest("metrics.market.fear_greed_index", "1d")
    plan = compile_strategy(spec, manifest, capability_payload(tsk.REVISION))
    primary = [
        {
            "open_time": 86400 + i * 3600,
            "open": "1",
            "high": "1",
            "low": "1",
            "close": "1",
            "volume": "1",
        }
        for i in range(24)
    ]
    metric_rows = [
        {
            "ts": day * 86400,
            "value": value,
            "available_at": (day + 1) * 86400,
            "revision": "r1",
        }
        for day, value in enumerate(["20", "30"])
    ]
    primary_id = next(
        item["stream_id"]
        for item in manifest["data_requirements"]
        if item["execution_role"] == "primary_execution_kline"
    )
    coverage = {
        "summary": {"strict_eligible": True},
        "request_identity": {"symbol": "BTCUSDT"},
    }
    frames = build_frames(
        {primary_id: primary, "metrics.market.fear_greed_index.1d": metric_rows},
        coverage,
        plan,
    )
    # Day-1 hourly frames close at 90000..172800: only the one closing at
    # 172800 (= day-1 ts + 86400) may read day 1; every earlier one reads day 0.
    values = [frame.values["metric_raw"] for frame in frames]
    assert values[:23] == ["20"] * 23
    assert frames[23].bar_close_at == 172800 and values[23] == "30"
    # Without day 1 that last frame fails closed instead of reusing day 0.
    with pytest.raises(StrategyContractError) as caught:
        build_frames(
            {primary_id: primary, "metrics.market.fear_greed_index.1d": metric_rows[:1]},
            coverage,
            plan,
        )
    assert caught.value.code == ERR_COVERAGE_INCOMPLETE


@pytest.mark.parametrize(
    "stream",
    ["metrics.global.fear_greed_index", "metrics.market", "metrics.self.Funding"],
)
def test_metrics_stream_shape_is_closed(stream: str) -> None:
    spec, manifest = _metrics_spec_and_manifest(stream, "1d")
    with pytest.raises(StrategyContractError):
        compile_strategy(spec, manifest, capability_payload(tsk.REVISION))
