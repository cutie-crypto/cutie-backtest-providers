"""JES21-B3b 二期 K1d: Codex review fixes for daily as-of ``metrics.*`` streams
and kline.primary passthrough history primitives referenced at lag.

1. Daily metrics are fetched back to the as-of point of the *first primary
   warmup frame*, not just the declared warmup below ``start_at``.
2. Coverage of a daily as-of metric stream is checked on day-aligned
   boundaries (an hourly ``start_at`` / ``end_at`` is not a day edge).
3. A passthrough history primitive's primary warmup floor adds its deepest
   lag reference, rejected at compile time instead of dying at runtime.
4. ``metrics.*`` daily as-of features may be referenced at ``lag_bars>0``
   (counted in primary frames); other coarse streams stay banned (pinned by
   test_strategy_kernel_primitives.py::test_coarse_kline_primary_feature_lag_bars_rejected_at_preflight_before_additive_floor).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cutie_backtesting_provider as provider  # noqa: E402
import test_strategy_kernel as tsk  # noqa: E402
from canonical_json import canonical_json_sha256  # noqa: E402
from strategy_kernel import (  # noqa: E402
    ERR_SPEC_INVALID,
    StrategyContractError,
    capability_payload,
    compile_strategy,
)

DAY = 86400
HOUR = 3600
METRIC_STREAM = "metrics.market.fear_greed_index"
METRIC_REQ = f"{METRIC_STREAM}.1d"


def _metric_feature() -> dict:
    return {
        "key": "fg",
        "primitive": "rolling_extreme",
        "primitive_version": "1",
        "source_stream": METRIC_STREAM,
        "interval": "1d",
        "value_kind": "level",
        "output_type": "decimal",
        "params": {"window_bars": 1, "mode": "max"},
        "required": True,
    }


def _ema_feature(window: int = 3) -> dict:
    return {
        "key": "ema",
        "primitive": "ema",
        "primitive_version": "1",
        "source_stream": "kline.primary.close",
        "interval": "1h",
        "value_kind": "price",
        "output_type": "decimal",
        "params": {"window_bars": window},
        "required": True,
    }


def _gt(key: str, lag: int = 0) -> dict:
    return {
        "node": "compare",
        "op": "gt",
        "left": {"node": "feature", "key": key, "lag_bars": lag},
        "right": tsk._literal("0.5"),
    }


def _manifest(spec: dict, *, primary_warmup: int, metric_warmup: int | None) -> dict:
    manifest = tsk.make_manifest(spec)
    requirements = []
    for item in manifest["data_requirements"]:
        if item["kind"] == "kline":
            item["warmup_bars"] = primary_warmup
            requirements.append(item)
        elif metric_warmup is not None:
            item.update(
                {
                    "stream_id": METRIC_REQ,
                    "provider": "cutie",
                    "storage_source": "market_metrics_history",
                    "exchange": "auto",
                    "interval": "1d",
                    "warmup_bars": metric_warmup,
                }
            )
            requirements.append(item)
    manifest["data_requirements"] = sorted(requirements, key=lambda r: r["stream_id"])
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
    return manifest


def _request(spec: dict, manifest: dict, start_at: int, end_at: int) -> dict:
    request = tsk.build_request(spec)
    request["artifact_manifest"] = manifest
    request["artifact"]["manifest_hash"] = canonical_json_sha256(manifest)
    request["artifact"]["artifact_hash"] = canonical_json_sha256(
        {
            "schema": "cutie.strategy_artifact_digest.v1",
            "spec_hash": request["artifact"]["spec_hash"],
            "manifest_hash": request["artifact"]["manifest_hash"],
        }
    )
    request["execution_params"]["start_at"] = start_at
    request["execution_params"]["end_at"] = end_at
    return request


def _install_complete_data(monkeypatch) -> list[dict]:
    """Complete, contiguous hourly K-lines and daily metric points."""
    monkeypatch.setattr(provider, "PROVIDER_REVISION", tsk.REVISION)
    metric_calls: list[dict] = []

    def fetch_klines(exchange, market, symbol, timeframe, start_ms, end_ms):
        return [
            [ts * 1000, 100, 101, 99, 100, 1]
            for ts in range(start_ms // 1000, end_ms // 1000, HOUR)
        ]

    def fetch_metric_chunk(*, symbol, metric, interval, exchange, start_at, end_at):
        metric_calls.append({"start_at": start_at, "end_at": end_at})
        first = -(-start_at // DAY) * DAY
        return [{"ts": ts, "value": "50"} for ts in range(first, end_at + 1, DAY)]

    monkeypatch.setattr(provider, "_fetch_from_central", fetch_klines)
    monkeypatch.setattr(provider, "_fetch_artifact_metric_chunk", fetch_metric_chunk)
    return metric_calls


def _post(request: dict) -> dict:
    response = TestClient(provider.app).post(
        "/cutie/backtest",
        json=request,
        headers={"X-Cutie-Connector-Version": "1.2.3"},
    )
    return response.json()


def _metric_stream(body: dict) -> dict:
    return next(
        stream
        for stream in body["coverage_manifest"]["streams"]
        if stream["stream_id"].startswith(METRIC_REQ)
    )


# --- finding 1: metrics cover the primary warmup frames' as-of history ------


def test_k1d_daily_metric_fetch_covers_first_primary_warmup_frame(monkeypatch) -> None:
    """start=172800, EMA(3) primary warmup 3, metric warmup 1 day: the first
    warmup frame (open 162000, close 165600) as-of reads day ts=0, which the
    declared-only fetch (ts >= 86400) never retrieved."""
    calls = _install_complete_data(monkeypatch)
    spec = tsk.make_spec(
        features=[_ema_feature(3), _metric_feature()],
        condition={"node": "all", "args": [_gt("ema"), _gt("fg")]},
    )
    manifest = _manifest(spec, primary_warmup=3, metric_warmup=1)
    compile_strategy(spec, manifest, capability_payload(tsk.REVISION))
    body = _post(_request(spec, manifest, 2 * DAY, 3 * DAY))
    assert body["result_status"] == "success", body.get("error_detail")
    assert min(call["start_at"] for call in calls) == 0
    # Coverage still reports the declared (day-aligned) slice.
    assert _metric_stream(body)["required_range"]["warmup_start_at"] == DAY


# --- finding 2: day-aligned coverage for an hourly window -------------------


def test_k1d_daily_metric_coverage_uses_day_aligned_bounds(monkeypatch) -> None:
    """1h primary, 1d fear & greed, start=176400 / end=266400 (both mid-day):
    complete data must pass, with the coverage range on the day grid."""
    _install_complete_data(monkeypatch)
    spec = tsk.make_spec(features=[_metric_feature()], condition=_gt("fg"))
    manifest = _manifest(spec, primary_warmup=0, metric_warmup=1)
    body = _post(_request(spec, manifest, 2 * DAY + HOUR, 3 * DAY + 2 * HOUR))
    assert body["result_status"] == "success", body.get("error_detail")
    stream = _metric_stream(body)
    assert stream["required_range"]["warmup_start_at"] == DAY
    assert stream["actual_range"]["available_through"] == 3 * DAY
    assert stream["point_count"] == 2


# --- finding 3: passthrough warmup floor adds the lag reference -------------


def test_k1d_passthrough_history_warmup_must_cover_lag_at_compile() -> None:
    """EMA(3) first resolves at row 2; warmup 3 with lag_bars=5 makes the
    first decision frame (row 5) read row 0 -- reject at compile time."""
    spec = tsk.make_spec(features=[_ema_feature(3)], condition=_gt("ema", lag=5))
    with pytest.raises(StrategyContractError) as caught:
        compile_strategy(
            spec,
            _manifest(spec, primary_warmup=3, metric_warmup=None),
            capability_payload(tsk.REVISION),
        )
    assert caught.value.code == ERR_SPEC_INVALID
    assert caught.value.path.endswith(".warmup_bars")
    assert caught.value.required == 3 - 1 + 5


def test_k1d_passthrough_history_warmup_covering_lag_executes(monkeypatch) -> None:
    _install_complete_data(monkeypatch)
    spec = tsk.make_spec(features=[_ema_feature(3)], condition=_gt("ema", lag=5))
    manifest = _manifest(spec, primary_warmup=7, metric_warmup=None)
    compile_strategy(spec, manifest, capability_payload(tsk.REVISION))
    body = _post(_request(spec, manifest, 7 * HOUR, 10 * HOUR))
    assert body["result_status"] == "success", body.get("error_detail")


# --- finding 4: N-day change on a daily metric (lag in primary frames) -----


def test_k1d_daily_metric_lag_in_primary_frames_executes(monkeypatch) -> None:
    """IMPL §3.3 N 日变化: 1h primary referencing the 1d metric at
    lag_bars=24 compiles and executes; the lag reads the as-of value of the
    frame 24 primary bars earlier, so its day point must be fetched too."""
    calls = _install_complete_data(monkeypatch)
    spec = tsk.make_spec(
        features=[_metric_feature()],
        condition={"node": "all", "args": [_gt("fg"), _gt("fg", lag=24)]},
    )
    manifest = _manifest(spec, primary_warmup=0, metric_warmup=1)
    compile_strategy(spec, manifest, capability_payload(tsk.REVISION))
    body = _post(_request(spec, manifest, 3 * DAY, 3 * DAY + 2 * HOUR))
    assert body["result_status"] == "success", body.get("error_detail")
    # First frame (open 2d, close 2d+1h) as-of reads day 1.
    assert min(call["start_at"] for call in calls) == DAY
