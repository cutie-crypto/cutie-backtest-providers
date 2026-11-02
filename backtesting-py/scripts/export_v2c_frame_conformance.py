#!/usr/bin/env python3
"""K0 (四期 B3B-P4-K0): v2c per-frame conformance golden export.

Reads the server-owned condition-matrix spec fixture (an explicit path
argument -- this script is a dev tool, never imported by the provider
runtime), replays every case through the Provider's own
``compile_strategy`` + ``build_frames`` + condition loop, and writes
``tests/fixtures/strategy_kernel_conformance_v2c_frames.json``.

Each frame records ``{frame_close_at, entry_condition, exit_condition,
feature_values}``. ``entry_condition`` / ``exit_condition`` are the kernel's
own ``StrategyKernel._condition`` results on that frame's history, i.e. the
same predicate the replay loop evaluates in ``StrategyKernel.evaluate`` --
position/cooldown gating stays with the consumer (the server evaluator takes
``position`` and owns that decision). ``exit_condition`` is ``false`` for the
matrix specs because their exit is ``time_exit_bars`` with ``signal_exit``
null.

Feature values are resolved twice per frame: once by ``build_frames`` (the
runtime path, which is what lands in the golden) and once by
``_anchor_ts`` + the kernel primitives, which must agree bit-for-bit. That
cross-check is the mutation target: swapping the metric as-of anchor for
"the latest row at-or-before the frame close" makes the export fail closed
instead of silently shipping wrong golden values.

The fixture is self-contained: every case carries the ``provider_params`` it
was exported from and the shared synthetic market data, so the consumer
(server ``condition_kernel_evaluator``, E1) can replay it without the matrix
file. ``sha256`` is the canonical digest of ``{market_data, cases}``.

Regenerate (only when the server matrix specs change -- never to make a test
pass)::

    python3 scripts/export_v2c_frame_conformance.py \\
        --matrix /path/to/cutie-server/tests/fixtures/b3b_condition_matrix_specs.json

Rebuild from the fixture's own embedded specs (what the test does)::

    python3 scripts/export_v2c_frame_conformance.py \\
        --from-fixture tests/fixtures/strategy_kernel_conformance_v2c_frames.json \\
        --check
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from decimal import localcontext
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cutie_backtesting_provider as provider  # noqa: E402
from canonical_json import canonical_decimal_str, canonical_json_sha256  # noqa: E402
from strategy_execution import validate_execution_request  # noqa: E402
from strategy_kernel import (  # noqa: E402
    _DECIMAL_CONTEXT,
    _RECURSIVE_PRIMITIVES_V2,
    _evaluate_windowed_primitive,
    _interval_seconds,
    _recursive_series_v2,
    build_frames,
    capability_hash,
    capability_payload,
    initial_state,
    metrics_asof_anchor,
    metrics_stream_parts,
    StrategyKernel,
)

SCHEMA = "cutie.strategy_kernel_conformance.v2c_frames"
FIXTURE_VERSION = "1"
DEFAULT_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "fixtures"
    / "strategy_kernel_conformance_v2c_frames.json"
)
SOURCE_OF_TRUTH = (
    "backtesting-py/scripts/export_v2c_frame_conformance.py "
    "(--matrix <cutie-server b3b_condition_matrix_specs.json>)"
)

HOUR = 3600
DAY = 86400
# 30 decision frames cross one UTC midnight, so every 1d metric as-of anchor
# (and the 24-frame / 72-frame lag references) sees two distinct daily values
# while every atom still gets both a true and a false frame.
DECISION_FRAMES = 30
# Fixed seed for the deterministic hourly jitter (see ``PRICE_JITTER``); the
# price path itself is a 24h sawtooth plus an alternating daily drift so that
# every v2c atom gets both a true and a false frame (see the atom coverage
# test).
SEED = 1102
PRICE_JITTER_COUNT = 97
# Capability binding is not part of the golden; any frozen revision works as
# long as the manifest and the capability payload agree. The provider runtime
# resolves its own revision from CUTIE_PROVIDER_REVISION.
GOLDEN_PROVIDER_REVISION = "0" * 40
INSTRUMENT_RULES = {
    "symbol": "BTCUSDT",
    "price_tick": "0.1",
    "qty_step": "0.001",
    "min_qty": "0.001",
    "min_notional": "5",
}
METRIC_REVISION = "r1"

# Per-stream metric values. A 2-day (1d streams) / 3-hour (1h stitched streams)
# cycle of extreme values makes each atom's ``lt``/``lte``/``gt``/``gte``
# threshold take both branches; ``stablecoin_mcap_total`` uses a 4-day cycle
# because its condition is a 72-frame (3-day) ratio -- a 3-day lag against a
# 2-day cycle would alias to a constant ratio.
METRIC_CYCLES = {
    "metrics.self.funding_rate_1h_stitched": ("1h", ("-1", "1", "0.5")),
    "metrics.self.global_long_short_ratio_1h_stitched": ("1h", ("0.2", "3", "1")),
    "metrics.self.liquidation_long_24h_stitched": (
        "1h",
        ("0", "5000000", "2000000"),
    ),
    "metrics.self.open_interest_aggregated_1h_stitched": (
        "1h",
        ("1000000", "1200000", "2000000"),
    ),
    "metrics.market.stablecoin_mcap_total": (
        "1d",
        ("1000000", "2000000", "1100000", "3000000"),
    ),
}
DEFAULT_METRIC_CYCLE = ("1d", ("-1000000", "1000000"))


class FrameConformanceError(RuntimeError):
    """The exporter's own value resolution disagrees with ``build_frames``."""


def _lcg_jitter() -> tuple[int, ...]:
    """Fixed-seed jitter, independent of the Python version's PRNG."""
    state = SEED
    out = []
    for _ in range(PRICE_JITTER_COUNT):
        state = (state * 1103515245 + 12345) % (2**31)
        out.append(state % 3 - 1)
    return tuple(out)


PRICE_JITTER = _lcg_jitter()


def _close_at(open_time: int, start_at: int) -> int:
    """Deterministic 1h close path.

    A 24-point shape (16 up, one spike, 5 down of -6, 2 flat) gives
    rsi_wilder / ema / macd / rolling_zscore both sides of their thresholds
    inside a single day; a four-point alternating daily drift makes the
    24-frame ``change_24h`` ratio cross ``±1%`` across the midnight; the
    seeded ±1 jitter keeps the recursive series off round numbers.
    """
    hour = open_time // HOUR
    phase = hour % 24
    drift = 4 * ((open_time // DAY) % 2)
    jitter = PRICE_JITTER[hour % PRICE_JITTER_COUNT]
    if phase < 16:
        base = 100 + phase
    elif phase == 16:
        base = 121
    elif phase <= 21:
        base = 121 - 6 * (phase - 16)
    else:
        base = 90 + (phase - 21)
    return drift + jitter + base


def _kline_row(open_time: int, start_at: int) -> list:
    close = _close_at(open_time, start_at)
    open_price = close - 1
    return [
        open_time,
        str(open_price),
        str(max(open_price, close) + 2),
        str(min(open_price, close) - 2),
        str(close),
        "1",
    ]


def _metric_cycle(source_stream: str) -> tuple[str, tuple[str, ...]]:
    return METRIC_CYCLES.get(source_stream, DEFAULT_METRIC_CYCLE)


def _metric_rows(
    source_stream: str, interval: str, first_ts: int, last_ts: int
) -> list[list]:
    step = DAY if interval == "1d" else HOUR
    values = _metric_cycle(source_stream)[1]
    rows = []
    index = 0
    ts = first_ts
    while ts <= last_ts:
        rows.append([ts, values[index % len(values)], ts + step])
        index += 1
        ts += step
    return rows


def _feature_requirements(cases: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """``stream_id -> {"interval", "warmup_bars" (max across cases)}``."""
    streams: dict[str, dict[str, int]] = {}
    for case in cases:
        for requirement in case["provider_params"]["data_requirements"]:
            if requirement["kind"] != "feature":
                continue
            current = streams.get(requirement["stream_id"])
            if current is None:
                streams[requirement["stream_id"]] = {
                    "interval": requirement["interval"],
                    "warmup_bars": requirement["warmup_bars"],
                    "order": len(streams),
                }
            else:
                if current["interval"] != requirement["interval"]:
                    raise FrameConformanceError(
                        f"{requirement['stream_id']}: mixed intervals in matrix"
                    )
                current["warmup_bars"] = max(
                    current["warmup_bars"], requirement["warmup_bars"]
                )
    return streams


def _max_primary_warmup(cases: list[dict[str, Any]]) -> int:
    return max(
        requirement["warmup_bars"]
        for case in cases
        for requirement in case["provider_params"]["data_requirements"]
        if requirement["execution_role"] == "primary_execution_kline"
    )


def build_market_data(
    cases: list[dict[str, Any]],
    matrix: dict[str, Any],
    decision_frames: int = DECISION_FRAMES,
) -> dict[str, Any]:
    """The shared synthetic inputs every case slices from.

    ``klines`` rows are ``[open_time, open, high, low, close, volume]``;
    feature rows are ``[ts, value, available_at]`` with a fixed revision.
    """
    start_at = int(matrix["start_at"])
    step = _interval_seconds(matrix["timeframe"])
    end_at = start_at + decision_frames * step
    warmup = _max_primary_warmup(cases)
    klines = [
        _kline_row(open_time, start_at)
        for open_time in range(start_at - warmup * step, end_at, step)
    ]
    feature_rows: dict[str, list[list]] = {}
    for stream_id, info in sorted(
        _feature_requirements(cases).items(), key=lambda item: item[1]["order"]
    ):
        stream_step = _interval_seconds(info["interval"])
        source_stream = stream_id.rsplit(".", 1)[0]
        if info["interval"] == "1d":
            first = (start_at // DAY) * DAY - (info["warmup_bars"] + 4) * DAY
            last = ((end_at // DAY) + 1) * DAY
        else:
            first = start_at - (info["warmup_bars"] + 4) * stream_step
            last = end_at + stream_step
        feature_rows[stream_id] = _metric_rows(
            source_stream, info["interval"], first, last
        )
    return {
        "symbol": matrix["symbol"],
        "timeframe": matrix["timeframe"],
        "start_at": start_at,
        "end_at": end_at,
        "primary_step_seconds": step,
        "klines": klines,
        "feature_row_columns": ["ts", "value", "available_at"],
        "feature_revision": METRIC_REVISION,
        "feature_rows": feature_rows,
    }


def _capability_pair() -> tuple[dict[str, Any], str]:
    payload = capability_payload(GOLDEN_PROVIDER_REVISION)
    return payload, capability_hash(payload)


def _plan_for(provider_params: dict[str, Any], meta: dict[str, Any], end_at: int):
    pair = _capability_pair()
    request = provider._condition_kernel_request(
        strategy_spec=provider_params["strategy_spec"],
        data_requirements=provider_params["data_requirements"],
        symbol=meta["symbol"],
        market=provider_params["strategy_spec"]["market"]["market_type"],
        timeframe=meta["timeframe"],
        start_at=int(meta["start_at"]),
        end_at=end_at,
        initial_capital="10000",
        cost_bps={
            "fee_bps": str(meta["fee_bps"]),
            "slippage_bps": str(meta["slippage_bps"]),
        },
        instrument_rules=dict(INSTRUMENT_RULES),
        capability_pair=pair,
    )
    validated = validate_execution_request(request, pair[0], pair[1])
    # ``initial_state`` and the frame loop need the validated request's
    # execution params; the plan alone cannot rebuild them.
    return validated.plan, request


def _slice_rows(rows: list[list], first_ts: int, last_ts: int) -> list[list]:
    return [row for row in rows if first_ts <= row[0] <= last_ts]


def _data_streams(plan: Any, market: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    start_at = market["start_at"]
    end_at = market["end_at"]
    streams: dict[str, list[dict[str, Any]]] = {}
    for requirement in plan.artifact_manifest["data_requirements"]:
        step = _interval_seconds(requirement["interval"])
        first = start_at - requirement["warmup_bars"] * step
        if requirement["execution_role"] == "primary_execution_kline":
            rows = _slice_rows(market["klines"], first, end_at - step)
            if any(len(row) != 6 for row in rows):
                raise FrameConformanceError("primary kline row shape drifted")
            streams[requirement["stream_id"]] = [
                {
                    "open_time": row[0],
                    "open": row[1],
                    "high": row[2],
                    "low": row[3],
                    "close": row[4],
                    "volume": row[5],
                }
                for row in rows
            ]
            continue
        source = _slice_rows(
            market["feature_rows"][requirement["stream_id"]], first, end_at
        )
        streams[requirement["stream_id"]] = [
            {
                "ts": row[0],
                "value": row[1],
                "available_at": row[2],
                "revision": market["feature_revision"],
            }
            for row in source
        ]
    return streams


def _anchor_ts(feature: dict[str, Any], frame: Any, timeframe: str) -> int:
    """The stream row a frame reads for ``feature`` (build_frames §5.5/§3.4).

    MUTATION TARGET: returning "the latest row at-or-before the frame close"
    for a ``metrics.*`` feature breaks the ``build_frames`` cross-check
    (``_resolved_value`` below) instead of shipping wrong golden values.
    """
    source_stream = feature["source_stream"]
    step = _interval_seconds(feature["interval"])
    if source_stream.startswith("kline.primary."):
        if feature["interval"] == timeframe:
            # Passthrough: the frame's own bar.
            return frame.bar_open_at
        # Coarse kline as-of: the most recent bucket that closed at-or-before
        # this frame's close.
        bucket_close = (frame.bar_close_at // step) * step
        return bucket_close - step
    if metrics_stream_parts(source_stream) is not None:
        return metrics_asof_anchor(frame.bar_close_at, step)
    return frame.bar_open_at


def _rows_by_ts(
    feature: dict[str, Any],
    data_streams: dict[str, list[dict[str, Any]]],
    primary_stream_id: str,
) -> tuple[dict[int, tuple[Any, int, str]], list[int]]:
    source_stream = feature["source_stream"]
    if source_stream.startswith("kline.primary."):
        field = source_stream.rsplit(".", 1)[1]
        primary = data_streams[primary_stream_id]
        step = _interval_seconds(feature["interval"])
        rows = {
            row["open_time"]: (row[field], row["open_time"] + step, "")
            for row in primary
        }
        return rows, sorted(rows)
    stream_id = f"{source_stream}.{feature['interval']}"
    rows = {
        row["ts"]: (row["value"], row["available_at"], row["revision"])
        for row in data_streams[stream_id]
    }
    return rows, sorted(rows)


def _canonical(value: Any) -> str:
    with localcontext(_DECIMAL_CONTEXT):
        return canonical_decimal_str(value)


def _resolved_value(
    feature: dict[str, Any],
    frame: Any,
    timeframe: str,
    rows_by_ts: dict[int, tuple[Any, int, str]],
    ordered_timestamps: list[int],
    recursive_series: Optional[dict[int, tuple[Any, int]]],
) -> Optional[str]:
    anchor = _anchor_ts(feature, frame, timeframe)
    step = _interval_seconds(feature["interval"])
    path = f"$.data_streams.{feature['source_stream']}"
    if recursive_series is not None:
        point = recursive_series.get(anchor)
        return None if point is None or point[0] is None else _canonical(point[0])
    outcome = _evaluate_windowed_primitive(
        feature, rows_by_ts, ordered_timestamps, anchor, step, path
    )
    return None if outcome is None else _canonical(outcome[0])


def _verify_frame_values(
    plan: Any,
    data_streams: dict[str, list[dict[str, Any]]],
    frames: list[Any],
    timeframe: str,
) -> None:
    """Every feature value in ``build_frames`` must equal our own resolution.

    This is the mechanical cross-check that makes replacing the as-of anchor
    with "the latest row" fail red instead of passing silently.
    """
    primary_stream_id = next(
        requirement["stream_id"]
        for requirement in plan.artifact_manifest["data_requirements"]
        if requirement["execution_role"] == "primary_execution_kline"
    )
    prepared = {}
    for feature in plan.strategy_spec["features"]:
        rows_by_ts, ordered_timestamps = _rows_by_ts(
            feature, data_streams, primary_stream_id
        )
        recursive = None
        if feature["primitive"] in _RECURSIVE_PRIMITIVES_V2:
            recursive = _recursive_series_v2(
                feature,
                rows_by_ts,
                ordered_timestamps,
                f"$.data_streams.{feature['source_stream']}",
            )
        prepared[feature["key"]] = (feature, rows_by_ts, ordered_timestamps, recursive)
    for frame in frames:
        for key, (feature, rows_by_ts, ordered, recursive) in prepared.items():
            expected = _resolved_value(
                feature, frame, timeframe, rows_by_ts, ordered, recursive
            )
            actual = frame.values.get(key)
            if expected != actual:
                raise FrameConformanceError(
                    "feature value disagrees with build_frames "
                    f"key={key} close_at={frame.bar_close_at} "
                    f"build_frames={actual!r} exporter={expected!r}"
                )


def _frames_for_case(
    case: dict[str, Any], market: dict[str, Any], meta: dict[str, Any]
) -> dict[str, Any]:
    provider_params = case["provider_params"]
    plan, request = _plan_for(provider_params, meta, market["end_at"])
    data_streams = _data_streams(plan, market)
    coverage = {
        "summary": {"strict_eligible": True},
        "request_identity": {"symbol": market["symbol"]},
    }
    frames = build_frames(data_streams, coverage, plan)
    _verify_frame_values(plan, data_streams, frames, market["timeframe"])

    spec = plan.strategy_spec
    feature_keys = [feature["key"] for feature in spec["features"]]
    exit_condition = spec["exit"]["signal_exit"]
    kernel = StrategyKernel(plan)
    state = initial_state(plan, copy.deepcopy(request["execution_params"]))
    # Columnar frames: every list below is aligned by frame index. The four
    # fields are exactly the IMPL's per-frame record
    # {frame_close_at, entry_condition, exit_condition, feature_values};
    # columns keep the 462x30 golden reviewable instead of 5 MB of repeated
    # JSON keys.
    exported: dict[str, Any] = {
        "frame_close_at": [],
        "entry_condition": [],
        "exit_condition": [],
        "feature_values": {key: [] for key in feature_keys},
    }
    for index, frame in enumerate(frames):
        if frame.bar_open_at < market["start_at"]:
            continue
        state.frames = frames[: index + 1]
        entry = kernel._condition(spec["entry"]["condition"], state, index)
        exit_ = (
            False
            if exit_condition is None
            else kernel._condition(exit_condition, state, index)
        )
        exported["frame_close_at"].append(frame.bar_close_at)
        exported["entry_condition"].append(bool(entry))
        exported["exit_condition"].append(bool(exit_))
        for key in feature_keys:
            exported["feature_values"][key].append(frame.values[key])
    return {
        "case_id": case["case_id"],
        "spec_hash": plan.spec_hash,
        "provider_params": copy.deepcopy(provider_params),
        "frames": exported,
    }


def build_case_frames(
    case: dict[str, Any], market: dict[str, Any], meta: dict[str, Any]
) -> dict[str, Any]:
    return _frames_for_case(case, market, meta)


def matrix_meta(matrix: dict[str, Any], case_count: int) -> dict[str, Any]:
    """The source matrix identity recorded in the fixture header.

    ``sha256`` is the canonical digest of the parsed server matrix fixture, so
    the golden says exactly which matrix revision it was exported from.
    """
    return {
        "schema": matrix.get("schema"),
        "sha256": canonical_json_sha256(matrix),
        "case_count": case_count,
        "symbol": matrix["symbol"],
        "timeframe": matrix["timeframe"],
        "start_at": int(matrix["start_at"]),
        "fee_bps": matrix["fee_bps"],
        "slippage_bps": matrix["slippage_bps"],
        "position_margin": matrix.get("position_margin"),
    }


def build_fixture(
    cases: list[dict[str, Any]],
    market: dict[str, Any],
    meta: dict[str, Any],
    decision_frames: int = DECISION_FRAMES,
) -> dict[str, Any]:
    ordered = sorted(cases, key=lambda case: case["case_id"])
    exported = [build_case_frames(case, market, meta) for case in ordered]
    payload = {"market_data": market, "cases": exported}
    return {
        "schema": SCHEMA,
        "fixture_version": FIXTURE_VERSION,
        "source_of_truth": SOURCE_OF_TRUTH,
        "note": (
            "Every frame is one v2c decision bar: entry_condition / "
            "exit_condition are StrategyKernel._condition on that frame's "
            "history (position/cooldown gating belongs to the consumer), "
            "feature_values are the build_frames values as cutie.decimal128.v1 "
            "canonical strings. Frames are columnar: every list in `frames` "
            "(frame_close_at, entry_condition, exit_condition and each "
            "feature_values[key]) is aligned by frame index. exit_condition "
            "is false throughout because "
            "the matrix specs exit by time_exit_bars with signal_exit null. "
            "The synthetic market data is deterministic; see `generator`. "
            "sha256 = canonical_json_sha256({market_data, cases})."
        ),
        "matrix": meta,
        "generator": {
            "decision_frames": decision_frames,
            "seed": SEED,
            "price_series": (
                "24h shape (16 up, spike, 5 down of -6, 2 flat) + 4-point "
                "alternating daily drift + LCG(seed) hourly jitter in [-1, 1]"
            ),
            "instrument_rules": dict(INSTRUMENT_RULES),
            "golden_provider_revision": GOLDEN_PROVIDER_REVISION,
        },
        "market_data": market,
        "cases": exported,
        "sha256": canonical_json_sha256(payload),
    }


def _matrix_cases(matrix: dict[str, Any]) -> list[dict[str, Any]]:
    cases = matrix.get("cases")
    if not isinstance(cases, list) or not cases:
        raise FrameConformanceError("matrix fixture has no cases")
    return cases


def _write(fixture: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(fixture, ensure_ascii=False, indent=1, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--matrix",
        type=Path,
        help="server b3b_condition_matrix_specs.json to export every case from",
    )
    source.add_argument(
        "--from-fixture",
        type=Path,
        help="rebuild from an existing fixture's embedded provider_params",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 if the committed fixture differs",
    )
    args = parser.parse_args(argv)

    if args.matrix is not None:
        matrix = json.loads(args.matrix.read_text(encoding="utf-8"))
        cases = _matrix_cases(matrix)
        meta = matrix_meta(matrix, len(cases))
    else:
        committed = json.loads(args.from_fixture.read_text(encoding="utf-8"))
        cases = committed["cases"]
        matrix = committed["matrix"]
        meta = matrix
    market = build_market_data(cases, matrix)
    fixture = build_fixture(cases, market, meta)
    if args.check:
        target = args.from_fixture or args.out
        committed = json.loads(target.read_text(encoding="utf-8"))
        if committed != fixture:
            print(f"MISMATCH {target}", file=sys.stderr)
            return 1
        print(f"OK {target} sha256={fixture['sha256']}")
        return 0
    _write(fixture, args.out)
    print(f"wrote {args.out} cases={len(fixture['cases'])} sha256={fixture['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
