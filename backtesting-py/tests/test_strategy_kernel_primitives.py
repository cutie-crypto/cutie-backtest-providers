"""62-2b Phase 1 conformance tests: rolling_quantile/rsi_wilder/rolling_extreme,
kline.primary source streams (passthrough + ohlcv_resample.v1 as-of alignment),
and the golden_replay permitted_uses reduction.
"""

from __future__ import annotations

import copy
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cutie_backtesting_provider as provider  # noqa: E402
import test_strategy_kernel as tsk  # noqa: E402
from canonical_json import canonical_decimal_str, canonical_json_sha256  # noqa: E402
from strategy_execution import CoverageInput, build_coverage_manifest  # noqa: E402
from strategy_kernel import (  # noqa: E402
    COMPILER_TOOL_ID,
    ERR_COVERAGE_INCOMPLETE,
    ERR_SPEC_INVALID,
    StrategyContractError,
    _evaluate_windowed_primitive,
    _rsi_wilder_series,
    build_frames,
    capability_hash,
    capability_payload,
    compile_strategy,
    ohlcv_resample,
)

REVISION = tsk.REVISION
CONFORMANCE_FIXTURE = (
    Path(__file__).parent / "fixtures" / "strategy_kernel_conformance_v1.json"
)


def _load_conformance_cases() -> list[dict]:
    return json.loads(CONFORMANCE_FIXTURE.read_text(encoding="utf-8"))["cases"]


_CASES_BY_NAME = {case["name"]: case for case in _load_conformance_cases()}


def _rows_by_ts(case: dict) -> tuple[dict[int, tuple], list[int]]:
    rows_by_ts = {
        row["ts"]: (row["value"], row["ts"] + 3600, "r1") for row in case["input"]
    }
    ordered_timestamps = sorted(rows_by_ts)
    return rows_by_ts, ordered_timestamps


@pytest.mark.parametrize(
    "name",
    [
        "rolling_quantile_min_periods_boundary_and_interpolation",
        "rolling_quantile_gap_is_never_skipped",
        "rolling_extreme_max_and_min_no_leniency",
        "rolling_extreme_min_with_gap",
    ],
)
def test_windowed_primitive_conformance(name: str) -> None:
    """Exercises _evaluate_windowed_primitive directly against the shared
    conformance vectors — the primitive's pure math, decoupled from the
    required=true hard-fail wrapper build_frames applies around it (covered
    separately by the kline.primary integration tests below)."""
    case = _CASES_BY_NAME[name]
    feature = {
        "key": "under_test",
        "primitive": case["primitive"],
        "params": case["params"],
        "interval": "1h",
    }
    rows_by_ts, ordered_timestamps = _rows_by_ts(case)
    actual: list[str | None] = []
    for row in case["input"]:
        outcome = _evaluate_windowed_primitive(
            feature, rows_by_ts, ordered_timestamps, row["ts"], 3600, "$.under_test"
        )
        actual.append(canonical_decimal_str(outcome[0]) if outcome is not None else None)
    expected = [row["value"] for row in case["expected"]]
    assert actual == expected, f"{name}: {actual} != {expected}"


@pytest.mark.parametrize(
    "name",
    [
        "rsi_wilder_seed_at_fetch_window_start",
        "rsi_wilder_broken_chain_never_recovers",
    ],
)
def test_rsi_wilder_conformance(name: str) -> None:
    case = _CASES_BY_NAME[name]
    rows_by_ts, ordered_timestamps = _rows_by_ts(case)
    series = _rsi_wilder_series(
        rows_by_ts, ordered_timestamps, case["params"]["period"], "$.under_test"
    )
    actual = [
        canonical_decimal_str(series[row["ts"]][0])
        if series[row["ts"]][0] is not None
        else None
        for row in case["input"]
    ]
    expected = [row["value"] for row in case["expected"]]
    assert actual == expected, f"{name}: {actual} != {expected}"


def test_ohlcv_resample_conformance() -> None:
    case = _CASES_BY_NAME["ohlcv_resample_4h_drops_incomplete_trailing_bucket"]
    primary_step = 3600
    output = ohlcv_resample(
        copy.deepcopy(case["input"]), primary_step, case["params"]["target_interval"]
    )
    assert output == case["expected"]


def test_ohlcv_resample_bucket_open_zero_lookahead_boundary() -> None:
    """available_at must equal the bucket's own close, and a decision frame
    may only read a 4h bucket once its own close has actually passed — never
    one second early, and every 1h frame inside the *next* bucket reads the
    same completed value until the next bucket itself closes (§5.5 as-of)."""
    full_primary = [
        {
            "open_time": i * 3600,
            "open": "100",
            "high": "101",
            "low": "99",
            "close": str(100 + i),
            "volume": "1",
        }
        for i in range(8)
    ]
    buckets = ohlcv_resample(full_primary, 3600, "4h")
    assert buckets == [
        {"open_time": 0, "open": "100", "high": "101", "low": "99", "close": "103"},
        {"open_time": 14400, "open": "100", "high": "101", "low": "99", "close": "107"},
    ]

    feature = {
        "key": "close_4h",
        "primitive": "rolling_extreme",
        "primitive_version": "1",
        "source_stream": "kline.primary.close",
        "interval": "4h",
        "value_kind": "price",
        "output_type": "decimal",
        "params": {"window_bars": 1, "mode": "max"},
        "required": True,
    }
    spec = tsk.make_spec(features=[feature])
    spec["market"]["timeframe"] = "1h"
    manifest = _kline_primary_manifest(spec, requirement_interval="4h")
    plan = compile_strategy(spec, manifest, capability_payload(REVISION))
    coverage = {
        "summary": {"strict_eligible": True},
        "request_identity": {"symbol": "BTCUSDT"},
    }
    derived_stream_id = "kline.primary.close.4h"
    derived_rows = [
        {
            "ts": bucket["open_time"],
            "value": bucket["close"],
            "available_at": bucket["open_time"] + 14400,
            "revision": "r1",
        }
        for bucket in buckets
    ]
    # The decision frames under test span the second bucket's own 1h bars
    # (open_time 14400..25200); bucket1 (ts=0) must already be visible to all
    # of them, and bucket2 only becomes visible to the last one (close=28800).
    second_bucket_primary = full_primary[4:8]

    frames = build_frames(
        {
            "binance.futures.kline.1h": second_bucket_primary,
            derived_stream_id: derived_rows,
        },
        coverage,
        plan,
    )
    assert [frame.bar_close_at for frame in frames] == [18000, 21600, 25200, 28800]
    # frames closing before bucket2 completes all read bucket1's value.
    assert [frame.values["close_4h"] for frame in frames[:3]] == ["103", "103", "103"]
    # the frame closing exactly at bucket2's own close (28800) reads bucket2.
    assert frames[3].values["close_4h"] == "107"

    # One second (i.e. one 1h bar) earlier than bucket2's close, it must not
    # be visible yet -> fails closed rather than silently reusing bucket1 or
    # fabricating bucket2 early.
    with pytest.raises(StrategyContractError) as caught:
        build_frames(
            {
                "binance.futures.kline.1h": second_bucket_primary,
                derived_stream_id: [derived_rows[0]],
            },
            coverage,
            plan,
        )
    assert caught.value.code == ERR_COVERAGE_INCOMPLETE


def _kline_primary_manifest(
    spec: dict, *, requirement_interval: str, warmup_bars: int = 0
) -> dict:
    """A manifest with the primary K-line plus one kline.primary feature
    requirement at `requirement_interval`. tsk.make_manifest already derives
    operators/features correctly from the real (non-empty) spec.features —
    only its hardcoded "any features -> add a coinglass data requirement"
    shortcut needs swapping out for the kline.primary requirement.

    `warmup_bars` (in the requirement's own coarse-bar units) must be set by
    the caller to at least the primitive's window_bars for a full pipeline
    run to succeed: an as-of anchor always needs one whole bucket *before*
    the decision frame's own bucket (the current bucket cannot be complete
    yet), so warmup_bars=window_bars-1 (sufficient for a same-interval
    feature) is one short here. Tests that only exercise compile-time
    validation or hand-construct build_frames inputs directly can leave the
    default 0.
    """
    base = tsk.make_manifest(spec)
    feature = spec["features"][0]
    field = feature["source_stream"].split(".")[-1]
    data_requirements = [
        item
        for item in base["data_requirements"]
        if item["stream_id"] != "coinglass.futures_cvd.1h"
    ]
    if requirement_interval != spec["market"]["timeframe"]:
        data_requirements = data_requirements + [
            {
                "stream_id": f"kline.primary.{field}.{requirement_interval}",
                "kind": "feature",
                "execution_role": "feature_input",
                "provider": "binance",
                "storage_source": "central_klines",
                "result_source": None,
                "exchange": "binance",
                "market": "futures",
                "symbols": ["BTCUSDT"],
                "interval": requirement_interval,
                "warmup_bars": warmup_bars,
                "max_freshness_seconds": 108000,
                "gap_policy": "none",
                "allowed_transforms": ["ohlcv_resample.v1"],
            }
        ]
    data_requirements = sorted(data_requirements, key=lambda item: item["stream_id"])
    data_sources = sorted(
        {
            (
                item["provider"],
                item["storage_source"],
                item["kind"],
                item["market"],
                item["result_source"],
            )
            for item in data_requirements
        }
    )
    manifest = copy.deepcopy(base)
    manifest["data_requirements"] = data_requirements
    manifest["capability_requirements"]["data_sources"] = [
        {
            "provider": p,
            "storage_source": s,
            "kind": k,
            "market": m,
            "result_source": r,
        }
        for (p, s, k, m, r) in data_sources
    ]
    manifest["capability_requirements"]["data_transforms"] = (
        ["ohlcv_resample.v1"] if requirement_interval != spec["market"]["timeframe"] else []
    )
    manifest["spec_hash"] = canonical_json_sha256(spec)
    return manifest


def _kline_primary_request(
    spec: dict, manifest: dict, *, start_at: int = 0, end_at: int
) -> dict:
    """Wraps a kline.primary manifest into a full execution request, mirroring
    tsk.build_request's hash wiring."""
    spec_hash = canonical_json_sha256(spec)
    manifest_hash = canonical_json_sha256(manifest)
    artifact_hash = canonical_json_sha256(
        {
            "schema": "cutie.strategy_artifact_digest.v1",
            "spec_hash": spec_hash,
            "manifest_hash": manifest_hash,
        }
    )
    params = tsk.execution_params()
    params["start_at"] = start_at
    params["end_at"] = end_at
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
        "execution_params": params,
        "expected_capability_hash": capability_hash(capability_payload(REVISION)),
        "expected_provider_revision": REVISION,
        "dispatch_nonce": "nonce-62-2b",
        "result_contract": {
            "result_schema": "cutie.backtest_result.v2",
            "coverage_schema": "cutie.strategy_coverage_manifest.v1",
            "trace_schema": "cutie.strategy_execution_trace.v1",
            "evidence_schema": "cutie.strategy_execution_evidence.v1",
        },
    }


def test_coarse_kline_primary_end_to_end_over_http(monkeypatch) -> None:
    """Full pipeline: Connector wire -> compile -> primary fetch -> the
    requirement's own independently-widened central fetch for
    ohlcv_resample derivation -> build_frames as-of read -> coverage
    .transforms entry -> golden_replay reduction.

    execution_start_at=14400 (bucket-aligned, one bucket in) with the coarse
    requirement's warmup_bars=1 (=window_bars, per the as-of lag documented
    on _kline_primary_manifest) so the very first decision frame already has
    a completed bucket to anchor on — the same reasoning a Server sizing a
    real manifest's warmup_bars would need to apply.
    """
    monkeypatch.setattr(provider, "PROVIDER_REVISION", REVISION)
    fetch_calls: list[tuple[int, int]] = []

    def fetch_klines(exchange, market, symbol, timeframe, start_ms, end_ms):
        fetch_calls.append((start_ms, end_ms))
        # 12 hourly bars from ts=0: three complete 4h buckets, closes 103
        # (bucket1, pure warmup construction), 107 (bucket2), 111 (bucket3).
        # high/low bound the whole 100..111 close range so OHLC ordering
        # always holds regardless of the varying close.
        return [[i * 3_600_000, 100, 200, 50, 100 + i, 1] for i in range(12)]

    monkeypatch.setattr(provider, "_fetch_from_central", fetch_klines)

    feature = {
        "key": "close_4h_high",
        "primitive": "rolling_extreme",
        "primitive_version": "1",
        "source_stream": "kline.primary.close",
        "interval": "4h",
        "value_kind": "price",
        "output_type": "decimal",
        "params": {"window_bars": 1, "mode": "max"},
        "required": True,
    }
    condition = {
        "node": "compare",
        "op": "gt",
        "left": {"node": "feature", "key": "close_4h_high", "lag_bars": 0},
        "right": {"node": "literal", "value_type": "decimal", "value": "105"},
    }
    spec = tsk.make_spec(condition=condition, features=[feature])
    manifest = _kline_primary_manifest(spec, requirement_interval="4h", warmup_bars=1)
    request = _kline_primary_request(spec, manifest, start_at=14400, end_at=43200)

    response = TestClient(provider.app).post(
        "/cutie/backtest",
        json=request,
        headers={"X-Cutie-Connector-Version": "1.2.3"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["result_status"] == "success", body

    # Two central fetches happened: primary's own [14400,43200) for decision
    # frames, and the coarse requirement's independently-widened [0,43200)
    # for bucket construction — both against the same declared central
    # source, never a different provider/storage_source.
    assert sorted(fetch_calls) == [(0, 43_200_000), (14_400_000, 43_200_000)]

    coverage = body["coverage_manifest"]
    derived_stream = next(
        s for s in coverage["streams"] if s["stream_id"] == "kline.primary.close@4h"
    )
    assert derived_stream["execution_role"] == "feature_input"
    assert derived_stream["result_source"] is None
    assert derived_stream["point_count"] == 3
    assert coverage["transforms"] == [
        {
            "output_stream_id": "kline.primary.close@4h",
            "input_stream_ids": ["binance.futures.kline.1h.BTCUSDT"],
            "transform": "ohlcv_resample.v1",
            "transform_version": "1",
            "params": {"target_interval": "4h"},
            "synthetic_ranges": [],
            "checksum": derived_stream["checksum"],
        }
    ]
    # ohlcv_resample.v1 is never synthetic and every stream this Provider
    # returns is already exact-complete -> golden_replay reduces true.
    assert coverage["summary"]["permitted_uses"] == {
        "backtest": True,
        "golden_replay": True,
        "paper": False,
    }
    # Bucket1 (close=103) is visible to decision frames closing 18000..28800
    # (anchored on the bucket completed before them) but 103 <= 105 so entry
    # never signals; bucket2 (close=107) only becomes visible to the frame
    # closing exactly at its own completion (28800), satisfying
    # close_4h_high > 105 there for the first time and filling on the next
    # frame's open.
    assert body["trades"], "bucket2's value (107 > 105) must have signalled entry"


def test_kline_primary_passthrough_reads_primary_field_directly() -> None:
    feature = {
        "key": "close_now",
        "primitive": "rolling_extreme",
        "primitive_version": "1",
        "source_stream": "kline.primary.close",
        "interval": "1h",
        "value_kind": "price",
        "output_type": "decimal",
        "params": {"window_bars": 1, "mode": "max"},
        "required": True,
    }
    spec = tsk.make_spec(features=[feature])
    manifest = _kline_primary_manifest(spec, requirement_interval="1h")
    plan = compile_strategy(spec, manifest, capability_payload(REVISION))
    primary_rows = [
        {
            "open_time": i * 3600,
            "open": "100",
            "high": "101",
            "low": "99",
            "close": str(100 + i),
            "volume": "1",
        }
        for i in range(3)
    ]
    coverage = {
        "summary": {"strict_eligible": True},
        "request_identity": {"symbol": "BTCUSDT"},
    }
    frames = build_frames({"binance.futures.kline.1h": primary_rows}, coverage, plan)
    assert [frame.values.get("close_now") for frame in frames] == ["100", "101", "102"]
    # No new stream/revision is produced for a passthrough kline.primary feature.
    assert "kline.primary.close" not in frames[-1].stream_revisions


def test_kline_primary_coarse_requires_1h_primary_and_4h_1d_target() -> None:
    feature = {
        "key": "close_4h",
        "primitive": "rsi_wilder",
        "primitive_version": "1",
        "source_stream": "kline.primary.close",
        "interval": "4h",
        "value_kind": "price",
        "output_type": "decimal",
        "params": {"period": 2},
        "required": True,
    }
    spec = tsk.make_spec(features=[feature])
    spec["market"]["timeframe"] = "15m"
    with pytest.raises(StrategyContractError) as caught:
        compile_strategy(
            spec, _kline_primary_manifest(spec, requirement_interval="4h"), capability_payload(REVISION)
        )
    assert caught.value.code == ERR_SPEC_INVALID


@pytest.mark.parametrize(
    "primitive,params",
    [
        ("rolling_quantile", {"window_bars": 1, "quantile": "0.5", "min_periods": 1}),
        ("rolling_quantile", {"window_bars": 4, "quantile": "1", "min_periods": 1}),
        ("rolling_quantile", {"window_bars": 4, "quantile": "0.5", "min_periods": 5}),
        ("rsi_wilder", {"period": 1}),
        ("rsi_wilder", {"period": 1001}),
        ("rolling_extreme", {"window_bars": 1, "mode": "median"}),
        ("rolling_extreme", {"window_bars": 0, "mode": "max"}),
    ],
)
def test_new_primitive_param_shapes_are_rejected_when_invalid(
    primitive: str, params: dict
) -> None:
    feature = {
        "key": "under_test",
        "primitive": primitive,
        "primitive_version": "1",
        "source_stream": "coinglass.futures_cvd",
        "interval": "1h",
        "value_kind": "flow",
        "output_type": "decimal",
        "params": params,
        "required": True,
    }
    spec = tsk.make_spec(features=[feature])
    with pytest.raises(StrategyContractError) as caught:
        compile_strategy(spec, tsk.make_manifest(spec), capability_payload(REVISION))
    assert caught.value.code == ERR_SPEC_INVALID


def test_rsi_wilder_requires_warmup_bars_at_least_10x_period() -> None:
    """SPEC §5.5: "使用 rsi_wilder 的 feature 其 warmup 必须 ≥10×period 个源 bar
    （种子效应收敛要求，compile 期校验）" — a compile-time gate, independent of
    whatever the Provider can actually fetch at execution time."""
    feature = {
        "key": "rsi_2",
        "primitive": "rsi_wilder",
        "primitive_version": "1",
        "source_stream": "coinglass.futures_cvd",
        "interval": "1h",
        "value_kind": "flow",
        "output_type": "decimal",
        "params": {"period": 2},
        "required": True,
    }
    spec = tsk.make_spec(features=[feature])
    manifest = tsk.make_manifest(spec)
    requirement = next(
        item
        for item in manifest["data_requirements"]
        if item["stream_id"] == "coinglass.futures_cvd.1h"
    )

    # tsk.make_manifest's fixture requirement declares warmup_bars=2; 10x
    # period=2 needs 20 -> must fail closed before any data access.
    assert requirement["warmup_bars"] == 2
    with pytest.raises(StrategyContractError) as caught:
        compile_strategy(spec, manifest, capability_payload(REVISION))
    assert caught.value.code == ERR_SPEC_INVALID
    assert caught.value.required == 20
    assert caught.value.actual == 2

    requirement["warmup_bars"] = 20
    manifest["spec_hash"] = canonical_json_sha256(spec)
    plan = compile_strategy(spec, manifest, capability_payload(REVISION))
    assert plan.feature_types["rsi_2"] == "decimal"


def test_rsi_wilder_warmup_check_uses_primary_warmup_for_a_passthrough_feature() -> None:
    feature = {
        "key": "rsi_close",
        "primitive": "rsi_wilder",
        "primitive_version": "1",
        "source_stream": "kline.primary.close",
        "interval": "1h",
        "value_kind": "price",
        "output_type": "decimal",
        "params": {"period": 3},
        "required": True,
    }
    spec = tsk.make_spec(features=[feature])
    manifest = _kline_primary_manifest(spec, requirement_interval="1h")
    primary_requirement = next(
        item
        for item in manifest["data_requirements"]
        if item["execution_role"] == "primary_execution_kline"
    )
    assert primary_requirement["warmup_bars"] == 0
    with pytest.raises(StrategyContractError) as caught:
        compile_strategy(spec, manifest, capability_payload(REVISION))
    assert caught.value.code == ERR_SPEC_INVALID
    assert caught.value.required == 30

    primary_requirement["warmup_bars"] = 30
    manifest["spec_hash"] = canonical_json_sha256(spec)
    compile_strategy(spec, manifest, capability_payload(REVISION))


def test_capability_advertises_the_three_new_primitives_and_ohlcv_resample() -> None:
    capability = capability_payload(REVISION)
    primitives = {item["primitive"] for item in capability["feature_primitives"]}
    assert primitives == {
        "rolling_sum",
        "rolling_quantile",
        "rsi_wilder",
        "rolling_extreme",
    }
    assert capability["data_transforms"] == ["ohlcv_resample.v1"]
    for item in capability["feature_primitives"]:
        if item["primitive"] == "rolling_sum":
            continue
        assert "kline.primary.close" in item["source_streams"]


def test_permitted_uses_golden_replay_reduces_true_when_all_streams_complete() -> None:
    request = tsk.build_feature_request()
    primary_requirement = next(
        item
        for item in request["artifact_manifest"]["data_requirements"]
        if item["execution_role"] == "primary_execution_kline"
    )
    feature_requirement = next(
        item
        for item in request["artifact_manifest"]["data_requirements"]
        if item["execution_role"] == "feature_input"
    )
    inputs = [
        CoverageInput(
            requirement=primary_requirement,
            checksum="a" * 64,
            revision="a" * 64,
            point_count=3,
            actual_start_at=0,
            actual_end_at=7200,
            available_through=10800,
        ),
        CoverageInput(
            requirement=feature_requirement,
            checksum="b" * 64,
            revision="b" * 64,
            point_count=3,
            actual_start_at=0,
            actual_end_at=7200,
            available_through=10800,
        ),
    ]
    data_manifest = {
        "source": "binance_futures",
        "symbol": "BTCUSDT",
        "market": "futures",
        "timeframe": "1h",
        "start_at": 0,
        "end_at": 10800,
        "kline_count": 3,
        "checksum_algo": "sha256",
        "checksum": "c" * 64,
    }
    coverage = build_coverage_manifest(request, inputs, data_manifest)
    assert coverage["summary"]["permitted_uses"] == {
        "backtest": True,
        "golden_replay": True,
        "paper": False,
    }
    for stream in coverage["streams"]:
        assert stream["permitted_uses"]["golden_replay"] is True


def test_coarse_kline_primary_coverage_stream_id_uses_at_separator_and_inherits_primary_revision() -> None:
    feature = {
        "key": "close_4h",
        "primitive": "rolling_extreme",
        "primitive_version": "1",
        "source_stream": "kline.primary.close",
        "interval": "4h",
        "value_kind": "price",
        "output_type": "decimal",
        "params": {"window_bars": 1, "mode": "max"},
        "required": True,
    }
    spec = tsk.make_spec(features=[feature])
    manifest = _kline_primary_manifest(spec, requirement_interval="4h")
    spec_hash = canonical_json_sha256(spec)
    manifest["spec_hash"] = spec_hash
    manifest_hash = canonical_json_sha256(manifest)
    artifact_hash = canonical_json_sha256(
        {
            "schema": "cutie.strategy_artifact_digest.v1",
            "spec_hash": spec_hash,
            "manifest_hash": manifest_hash,
        }
    )
    params = tsk.execution_params()
    params["end_at"] = 14400  # a whole 4h bucket, so the coarse requirement aligns
    request = {
        "artifact_manifest": manifest,
        "strategy_spec": spec,
        "artifact": {
            "artifact_version_id": "900003",
            "artifact_hash": artifact_hash,
        },
        "execution_params": params,
        "run_id": "900001",
        "execution_mode": "historical_replay",
    }
    primary_requirement = next(
        item
        for item in manifest["data_requirements"]
        if item["execution_role"] == "primary_execution_kline"
    )
    derived_requirement = next(
        item
        for item in manifest["data_requirements"]
        if item["stream_id"].startswith("kline.primary.")
    )
    inputs = [
        CoverageInput(
            requirement=primary_requirement,
            checksum="a" * 64,
            revision="a" * 64,
            point_count=4,
            actual_start_at=0,
            actual_end_at=10800,
            available_through=14400,
        ),
        CoverageInput(
            requirement=derived_requirement,
            checksum="d" * 64,
            revision="d" * 64,
            point_count=1,
            actual_start_at=0,
            actual_end_at=0,
            available_through=14400,
        ),
    ]
    data_manifest = {
        "source": "binance_futures",
        "symbol": "BTCUSDT",
        "market": "futures",
        "timeframe": "1h",
        "start_at": 0,
        "end_at": 14400,
        "kline_count": 4,
        "checksum_algo": "sha256",
        "checksum": "c" * 64,
    }
    coverage = build_coverage_manifest(request, inputs, data_manifest)
    derived_streams = [
        s for s in coverage["streams"] if s["stream_id"].startswith("kline.primary.")
    ]
    assert len(derived_streams) == 1
    assert derived_streams[0]["stream_id"] == "kline.primary.close@4h"
    # revision inherited verbatim from the primary stream's own revision.
    assert derived_streams[0]["revision"]["value"] == "a" * 64
    # checksum is the derived series' own (never the primary's).
    assert derived_streams[0]["checksum"]["value"] == "d" * 64
    assert coverage["transforms"] == [
        {
            "output_stream_id": "kline.primary.close@4h",
            "input_stream_ids": ["binance.futures.kline.1h.BTCUSDT"],
            "transform": "ohlcv_resample.v1",
            "transform_version": "1",
            "params": {"target_interval": "4h"},
            "synthetic_ranges": [],
            "checksum": {"algo": "sha256", "value": "d" * 64},
        }
    ]
