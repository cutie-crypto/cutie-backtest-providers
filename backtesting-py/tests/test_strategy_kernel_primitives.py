"""62-2b Phase 1 conformance tests: rolling_quantile/rsi_wilder/rolling_extreme,
kline.primary source streams (passthrough + ohlcv_resample.v1 as-of alignment),
and the golden_replay permitted_uses reduction.
"""

from __future__ import annotations

import copy
import json
import sys
from decimal import Decimal, localcontext
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
    ERR_SPEC_UNSUPPORTED,
    CompiledPlan,
    StrategyContractError,
    _DECIMAL_CONTEXT,
    _evaluate_windowed_primitive,
    _rsi_wilder_series,
    build_frames,
    capability_hash,
    capability_payload,
    compile_strategy,
    ohlcv_resample,
)


def _canonical(value: Decimal) -> str:
    """canonical_decimal_str()'s normalize() rounds to whatever Decimal
    context is ambient at the call site (Python's default is prec=28, not
    decimal128's 34); tests that inspect a primitive's raw Decimal return
    value must serialize it under the same _DECIMAL_CONTEXT production code
    uses, or a correctly 34-digit-rounded value reads back truncated to 28."""
    with localcontext(_DECIMAL_CONTEXT):
        return canonical_decimal_str(value)

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
        "rolling_quantile_single_sample_at_stream_start",
        "rolling_extreme_max_and_min_no_leniency",
        "rolling_extreme_min_with_gap",
        "rolling_extreme_decimal128_rounding_tie_even_stays",
        "rolling_extreme_decimal128_rounding_tie_odd_rounds_up",
        "rolling_extreme_decimal128_rounding_more_than_half",
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
        actual.append(_canonical(outcome[0]) if outcome is not None else None)
    expected = [row["value"] for row in case["expected"]]
    assert actual == expected, f"{name}: {actual} != {expected}"


@pytest.mark.parametrize(
    "name",
    [
        "rsi_wilder_seed_avg_loss_zero",
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
        _canonical(series[row["ts"]][0])
        if series[row["ts"]][0] is not None
        else None
        for row in case["input"]
    ]
    expected = [row["value"] for row in case["expected"]]
    assert actual == expected, f"{name}: {actual} != {expected}"


@pytest.mark.parametrize(
    "name",
    [
        "ohlcv_resample_4h_drops_incomplete_trailing_bucket",
        "ohlcv_resample_1d_full_day_bucket",
    ],
)
def test_ohlcv_resample_conformance(name: str) -> None:
    case = _CASES_BY_NAME[name]
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
    manifest = _kline_primary_manifest(spec, requirement_interval="4h", warmup_bars=1)
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
    """Full pipeline: Connector wire -> compile -> one union-range primary
    fetch (widened up front to also cover the coarse requirement's own
    resample-source need) -> ohlcv_resample derivation from those same rows
    -> build_frames as-of read -> coverage .transforms entry -> golden_replay
    reduction.

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

    # Exactly one central fetch happened, widened to [0,43200) up front to
    # cover both the primary's own decision-frame need ([14400,43200)) and
    # the coarse requirement's bucket-construction need (back to ts=0) —
    # decision frames and derivation now read the identical fetched rows.
    assert fetch_calls == [(0, 43_200_000)]

    coverage = body["coverage_manifest"]
    primary_stream = next(
        s for s in coverage["streams"] if s["execution_role"] == "primary_execution_kline"
    )
    derived_stream = next(
        s for s in coverage["streams"] if s["stream_id"] == "kline.primary.close@4h"
    )
    assert derived_stream["execution_role"] == "feature_input"
    assert derived_stream["result_source"] is None
    assert derived_stream["point_count"] == 3
    # The derived stream's inherited revision is genuinely the checksum of
    # the same rows it was resampled from (the single union fetch), not a
    # separately (re-)fetched dataset — see _derive_kline_primary_feature_rows.
    assert derived_stream["revision"]["value"] == primary_stream["revision"]["value"]
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


def test_coarse_kline_primary_unaligned_start_at_keeps_the_bucket_before_it(
    monkeypatch,
) -> None:
    """Regression for the exact Codex probe: a 1h decision window
    [05:00,09:00) with a 4h coarse feature and warmup_bars=1 must not lose
    the [00:00,04:00) bucket. Naively offsetting from the unaligned
    start_at=18000 by warmup_bars*target_step=14400 lands at 3600 (still
    inside [00:00,04:00)), which used to make the fetch/coverage exclude
    that bucket's own open_time=0 row entirely — even though it is exactly
    the bucket every frame in [05:00,06:00) as-of reads (bucket_close=14400
    is the most recent completed bucket for those frames). The bucket-aligned
    kline_primary_bucket_required_start/_end fix must recover it and let the
    run succeed end to end.
    """
    monkeypatch.setattr(provider, "PROVIDER_REVISION", REVISION)
    fetch_calls: list[tuple[int, int]] = []

    def fetch_klines(exchange, market, symbol, timeframe, start_ms, end_ms):
        fetch_calls.append((start_ms, end_ms))
        # 9 hourly bars from ts=0 (00:00): bucket1 [00:00,04:00) closes 103,
        # bucket2 [04:00,08:00) closes 107; bar 8 alone starts bucket3 but
        # never completes within this fetch.
        return [[i * 3_600_000, 100, 200, 50, 100 + i, 1] for i in range(9)]

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
    # 05:00 -> 09:00: neither boundary is 4h-aligned relative to bucket
    # edges except by coincidence of warmup_bars=1's own bucket-floor step;
    # the point under test is that start_at=18000 itself falls inside
    # [04:00,08:00), not on a bucket edge.
    request = _kline_primary_request(spec, manifest, start_at=18000, end_at=32400)

    response = TestClient(provider.app).post(
        "/cutie/backtest",
        json=request,
        headers={"X-Cutie-Connector-Version": "1.2.3"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["result_status"] == "success", body

    # One union fetch, widened back to bucket-aligned ts=0 up front to also
    # cover the coarse requirement's resample-source need, recovering
    # bucket1 without a second, separate central call.
    assert fetch_calls == [(0, 32_400_000)]

    coverage = body["coverage_manifest"]
    derived_stream = next(
        s for s in coverage["streams"] if s["stream_id"] == "kline.primary.close@4h"
    )
    assert derived_stream["point_count"] == 2
    assert derived_stream["required_range"]["warmup_start_at"] == 0
    assert derived_stream["actual_range"]["start_at"] == 0

    # bucket1 [00:00,04:00) close=103 is read by the two frames closing
    # 06:00/07:00 (21600/25200) but 103 <= 105 so entry never signals there;
    # bucket2 [04:00,08:00) close=107 becomes visible at the frame closing
    # exactly at its own completion (28800), satisfying close_4h_high > 105.
    assert body["trades"], "bucket1 must have been recovered for the run to signal at all"


def test_derive_kline_primary_feature_rows_never_fetches_and_shares_revision() -> None:
    """§2 fix: _derive_kline_primary_feature_rows no longer independently
    (re-)fetches the primary K-line — a second, separate fetch could resample
    from data the decision frames never actually saw, pinning a coverage
    revision (inherited verbatim onto the derived stream, see
    build_coverage_manifest's ``primary_item.revision``) that would not be
    true. It is now a pure resample over whatever rows the caller (the single
    union-range fetch in _run_artifact_backtest) already fetched once, and
    every returned row's own revision is that same caller-supplied checksum
    verbatim — proving the two structurally share one source, not merely
    matching by coincidence."""
    primary_rows = [
        {
            "open_time": i * 3600,
            "open": "100",
            "high": "200",
            "low": "50",
            "close": str(100 + i),
            "volume": "1",
        }
        for i in range(8)
    ]
    requirement = {
        "stream_id": "kline.primary.close.4h",
        "interval": "4h",
    }
    rows = provider._derive_kline_primary_feature_rows(
        primary_rows, 3600, "shared-primary-revision", requirement, "close", 0, 28800
    )
    assert [row["value"] for row in rows] == ["103", "107"]
    assert all(row["revision"] == "shared-primary-revision" for row in rows)


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


def test_kline_primary_passthrough_rsi_wilder_does_not_keyerror() -> None:
    """Regression: rsi_wilder's precomputed series cache was only populated
    for non-passthrough features (build_frames skipped passthrough features
    entirely in that precompute loop), so a passthrough kline.primary
    rsi_wilder feature's per-frame lookup KeyError'd on a cache key that was
    never inserted — an unhandled Python exception, not a structured
    fail-closed error. ``compile_strategy`` now rejects a passthrough
    rsi_wilder feature outright (see
    test_rsi_wilder_warmup_check_uses_primary_warmup_for_a_passthrough_feature),
    since frame 0 is guaranteed to fail closed every run regardless of
    warmup_bars — so this hand-builds a CompiledPlan bypassing that gate to
    keep defense-in-depth coverage on build_frames itself never crashing
    with a bare KeyError if some future path ever reaches it.

    frame 0 (the array's very first row, whatever primary_requirement's
    warmup_bars happens to fetch) can never itself have `period` prior rows
    within that same array — inherent to a passthrough feature, which has no
    independent fetch of its own to extend further back than primary (unlike
    a regular feature stream, see
    test_multiple_features_share_one_declared_source_without_overwrite).
    build_frames requires every given row, including frame 0, to resolve, so
    this correctly still fails closed — the fix is that it now fails with
    the expected StrategyContractError(COVERAGE_INCOMPLETE), not a bare
    KeyError.
    """
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
    plan = CompiledPlan(
        strategy_spec=spec,
        artifact_manifest=manifest,
        spec_hash="",
        manifest_hash="",
        artifact_hash="",
        parameter_types={},
        feature_types={"rsi_close": "decimal"},
    )
    primary_rows = [
        {
            "open_time": i * 3600,
            "open": "100",
            "high": "1000",
            "low": "1",
            "close": str(100 + i),
            "volume": "1",
        }
        for i in range(6)
    ]
    coverage = {
        "summary": {"strict_eligible": True},
        "request_identity": {"symbol": "BTCUSDT"},
    }
    with pytest.raises(StrategyContractError) as caught:
        build_frames({"binance.futures.kline.1h": primary_rows}, coverage, plan)
    assert caught.value.code == ERR_COVERAGE_INCOMPLETE

    # The underlying series computation itself (decoupled from build_frames'
    # per-row frame 0 requirement) is exercised end to end by the
    # rsi_wilder_seed_avg_loss_zero/rsi_wilder_seed_at_fetch_window_start
    # conformance cases above — this test's job is only to prove the cache
    # wiring for a passthrough feature no longer KeyErrors.


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


def test_coarse_kline_primary_requirement_must_declare_ohlcv_resample_transform() -> None:
    """§4.2: transform selection is bound into the artifact hash via
    spec_hash; a coarse kline.primary requirement whose allowed_transforms
    omits ohlcv_resample.v1 must fail at compile, not be silently resampled
    anyway at runtime (the Provider would otherwise resample regardless of
    what the manifest actually declared)."""
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
    manifest = _kline_primary_manifest(spec, requirement_interval="4h", warmup_bars=1)
    requirement = next(
        item
        for item in manifest["data_requirements"]
        if item["stream_id"].startswith("kline.primary.")
    )
    assert requirement["allowed_transforms"] == ["ohlcv_resample.v1"]
    requirement["allowed_transforms"] = []
    manifest["capability_requirements"]["data_transforms"] = []
    with pytest.raises(StrategyContractError) as caught:
        compile_strategy(spec, manifest, capability_payload(REVISION))
    assert caught.value.code == ERR_SPEC_INVALID
    assert caught.value.required == "ohlcv_resample.v1"
    assert caught.value.actual == []


def test_passthrough_kline_primary_feature_must_not_have_its_own_requirement() -> None:
    """§5.5: a passthrough feature (interval == primary timeframe) must never
    have a data_requirement of its own ("不产生新 stream"). Before this check,
    the Provider's runtime predicate (matching by source_stream prefix and
    interval) could not distinguish an illegally-declared one from a
    legitimate coarse requirement, silently fetching/wasting it instead of
    rejecting the manifest."""
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
    illegal_requirement = {
        "stream_id": "kline.primary.close.1h",
        "kind": "feature",
        "execution_role": "feature_input",
        "provider": "binance",
        "storage_source": "central_klines",
        "result_source": None,
        "exchange": "binance",
        "market": "futures",
        "symbols": ["BTCUSDT"],
        "interval": "1h",
        "warmup_bars": 0,
        "max_freshness_seconds": 108000,
        "gap_policy": "none",
        "allowed_transforms": [],
    }
    manifest["data_requirements"] = sorted(
        manifest["data_requirements"] + [illegal_requirement],
        key=lambda item: item["stream_id"],
    )
    manifest["capability_requirements"]["data_sources"] = sorted(
        manifest["capability_requirements"]["data_sources"]
        + [
            {
                "provider": "binance",
                "storage_source": "central_klines",
                "kind": "feature",
                "market": "futures",
                "result_source": None,
            }
        ],
        key=lambda item: (item["provider"], item["storage_source"]),
    )
    with pytest.raises(StrategyContractError) as caught:
        compile_strategy(spec, manifest, capability_payload(REVISION))
    assert caught.value.code == ERR_SPEC_INVALID
    assert caught.value.actual == "kline.primary.close.1h"


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
    """The 10x-period warmup gate (_validate_rsi_warmup) still binds a
    passthrough rsi_wilder feature to the *primary* requirement's own
    warmup_bars (there is no requirement of its own to check). But even once
    that gate is satisfied, the feature itself can never execute: a
    passthrough feature has no lookback beyond whatever the primary
    requirement fetched, so its first frame is unconditionally unresolved
    (see _rsi_wilder_series' seed) no matter how large warmup_bars is —
    compile must reject the primitive itself, not just under-size warmup
    (this is the exact gap the old version of this test proved: satisfying
    the warmup_bars check used to make compile_strategy succeed even though
    every run was guaranteed to fail closed on frame 0)."""
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

    # Satisfying the warmup gate no longer makes this compile: the
    # passthrough + rsi_wilder combination itself is now rejected.
    primary_requirement["warmup_bars"] = 30
    manifest["spec_hash"] = canonical_json_sha256(spec)
    with pytest.raises(StrategyContractError) as caught:
        compile_strategy(spec, manifest, capability_payload(REVISION))
    assert caught.value.code == ERR_SPEC_UNSUPPORTED
    assert caught.value.path == "$.strategy_spec.features[0].primitive"


@pytest.mark.parametrize("primitive", ["rolling_sum", "rolling_extreme"])
def test_kline_primary_passthrough_window_primitive_requires_window_bars_one(
    primitive: str,
) -> None:
    """A passthrough feature has no lookback beyond the primary requirement's
    own fetch, so window_bars=1 (the anchor bar itself) is the only value
    whose first frame can ever resolve — anything wider is guaranteed to
    fail closed every run, so compile must reject it outright."""
    params = (
        {"window_bars": 2}
        if primitive == "rolling_sum"
        else {"window_bars": 2, "mode": "max"}
    )
    feature = {
        "key": "under_test",
        "primitive": primitive,
        "primitive_version": "1",
        "source_stream": "kline.primary.close",
        "interval": "1h",
        "value_kind": "price",
        "output_type": "decimal",
        "params": params,
        "required": True,
    }
    spec = tsk.make_spec(features=[feature])
    manifest = _kline_primary_manifest(spec, requirement_interval="1h")
    with pytest.raises(StrategyContractError) as caught:
        compile_strategy(spec, manifest, capability_payload(REVISION))
    assert caught.value.code == ERR_SPEC_UNSUPPORTED
    assert caught.value.path == "$.strategy_spec.features[0].params.window_bars"
    assert caught.value.required == 1
    assert caught.value.actual == 2


def test_kline_primary_passthrough_rolling_quantile_requires_min_periods_one() -> None:
    feature = {
        "key": "under_test",
        "primitive": "rolling_quantile",
        "primitive_version": "1",
        "source_stream": "kline.primary.close",
        "interval": "1h",
        "value_kind": "price",
        "output_type": "decimal",
        "params": {"window_bars": 4, "quantile": "0.5", "min_periods": 2},
        "required": True,
    }
    spec = tsk.make_spec(features=[feature])
    manifest = _kline_primary_manifest(spec, requirement_interval="1h")
    with pytest.raises(StrategyContractError) as caught:
        compile_strategy(spec, manifest, capability_payload(REVISION))
    assert caught.value.code == ERR_SPEC_UNSUPPORTED
    assert caught.value.path == "$.strategy_spec.features[0].params.min_periods"
    assert caught.value.required == 1
    assert caught.value.actual == 2

    # min_periods=1 is allowed even with a wide window_bars: a single-sample
    # window at frame 0 is still a valid quantile of one observation.
    feature["params"]["min_periods"] = 1
    spec = tsk.make_spec(features=[feature])
    manifest = _kline_primary_manifest(spec, requirement_interval="1h")
    compile_strategy(spec, manifest, capability_payload(REVISION))


@pytest.mark.parametrize(
    "field_name", ["provider", "storage_source", "exchange", "market"]
)
def test_coarse_kline_primary_requirement_must_match_primary_data_source_identity(
    field_name: str,
) -> None:
    """A coarse kline.primary requirement is never independently fetched —
    ohlcv_resample.v1 always resamples the primary requirement's own central
    source — so a declared provider/storage_source/exchange/market that
    differs from the primary's is a label the Provider's execution can never
    actually honor and must fail closed at compile."""
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
    manifest = _kline_primary_manifest(spec, requirement_interval="4h", warmup_bars=1)
    requirement = next(
        item
        for item in manifest["data_requirements"]
        if item["stream_id"].startswith("kline.primary.")
    )
    mismatched = {
        "provider": "coinglass",
        "storage_source": "market_metrics_history",
        "exchange": "okx",
        "market": "spot",
    }
    requirement[field_name] = mismatched[field_name]
    manifest["spec_hash"] = canonical_json_sha256(spec)
    # The kline.primary identity check (_validate_kline_primary_requirements)
    # runs before the capability_requirements exact-set comparison, so this
    # mismatch is caught without needing to keep data_sources in sync too.
    with pytest.raises(StrategyContractError) as caught:
        compile_strategy(spec, manifest, capability_payload(REVISION))
    assert caught.value.code == ERR_SPEC_INVALID
    assert caught.value.path == (
        f"$.artifact_manifest.data_requirements[{requirement['stream_id']}].{field_name}"
    )


@pytest.mark.parametrize(
    "primitive,params,required",
    [
        # rolling_sum's capability_payload source_streams do not include
        # kline.primary.* (only coinglass.futures_cvd) — window/quantile
        # primitives that are actually advertised for kline.primary sources.
        ("rolling_extreme", {"window_bars": 3, "mode": "max"}, 3),
        ("rolling_quantile", {"window_bars": 4, "quantile": "0.5", "min_periods": 3}, 3),
    ],
)
def test_coarse_kline_primary_requirement_warmup_bars_must_cover_primitive_lookback(
    primitive: str, params: dict, required: int
) -> None:
    """A coarse requirement's warmup_bars must reach the primitive's own
    lookback (window_bars, or rolling_quantile's min_periods) or the first
    decision frame in range compiles green but always fails closed for lack
    of a completed prior bucket to as-of anchor on."""
    feature = {
        "key": "under_test",
        "primitive": primitive,
        "primitive_version": "1",
        "source_stream": "kline.primary.close",
        "interval": "4h",
        "value_kind": "price",
        "output_type": "decimal",
        "params": params,
        "required": True,
    }
    spec = tsk.make_spec(features=[feature])
    manifest = _kline_primary_manifest(
        spec, requirement_interval="4h", warmup_bars=required - 1
    )
    with pytest.raises(StrategyContractError) as caught:
        compile_strategy(spec, manifest, capability_payload(REVISION))
    assert caught.value.code == ERR_SPEC_INVALID
    assert caught.value.path.endswith(".warmup_bars")
    assert caught.value.required == required
    assert caught.value.actual == required - 1

    manifest = _kline_primary_manifest(
        spec, requirement_interval="4h", warmup_bars=required
    )
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


@pytest.mark.parametrize("blank_field", ["revision", "checksum"])
def test_permitted_uses_golden_replay_false_when_a_stream_revision_or_checksum_is_blank(
    blank_field: str,
) -> None:
    """§7.4's golden-strict element "revision/checksum 固定" means a stream
    with nothing deterministic to pin a golden fixture to must not qualify,
    even when its own coverage range is otherwise exact-complete."""
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
    field_values = {"revision": "a" * 64, "checksum": "a" * 64}
    field_values[blank_field] = ""
    inputs = [
        CoverageInput(
            requirement=primary_requirement,
            checksum=field_values["checksum"],
            revision=field_values["revision"],
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
        # the primary stream's own checksum field is overridden from
        # data_manifest, not the CoverageInput, for primary specifically —
        # blank it here too so the "checksum" parametrization actually
        # exercises the primary stream's displayed checksum value.
        "checksum": field_values["checksum"] if blank_field == "checksum" else "c" * 64,
    }
    coverage = build_coverage_manifest(request, inputs, data_manifest)
    assert coverage["summary"]["permitted_uses"]["golden_replay"] is False
    assert coverage["summary"]["permitted_uses"] == {
        "backtest": True,
        "golden_replay": False,
        "paper": False,
    }
    primary_stream = next(
        s for s in coverage["streams"] if s["execution_role"] == "primary_execution_kline"
    )
    assert not primary_stream[blank_field]["value"]
    assert primary_stream["permitted_uses"]["golden_replay"] is False


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
