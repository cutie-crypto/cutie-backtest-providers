"""三期 K1: the five server-side 1d→1h stitched ``/metrics`` streams are in the
closed capability set for self/btc/eth only, accept interval 1h only, are
fetched without exchange, and tolerate the extra ``source_interval`` /
``source_ts`` row fields (IMPL §1.5.4)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import strategy_kernel as kernel  # noqa: E402
from strategy_kernel import StrategyContractError  # noqa: E402
from test_metrics_feature_fetch import _capture, _requirement_and_spec  # noqa: E402

import cutie_backtesting_provider as provider  # noqa: E402

STITCHED = (
    "funding_rate_1h_stitched",
    "open_interest_aggregated_1h_stitched",
    "global_long_short_ratio_1h_stitched",
    "liquidation_long_24h_stitched",
    "liquidation_short_24h_stitched",
)


def _feature(source_stream: str, interval: str) -> dict:
    return {
        "key": "m_stitched",
        "primitive": "rolling_extreme",
        "primitive_version": "1",
        "source_stream": source_stream,
        "interval": interval,
        "value_kind": "level",
        "output_type": "decimal",
        "params": {"mode": "max", "window_bars": 1},
        "required": True,
    }


@pytest.mark.parametrize("metric", STITCHED)
def test_capability_declares_stitched_streams_for_per_coin_scopes(metric):
    declared = set(kernel.METRICS_SOURCE_STREAMS)
    for scope in ("self", "btc", "eth"):
        assert f"metrics.{scope}.{metric}" in declared
    assert f"metrics.market.{metric}" not in declared
    assert len(metric) <= 40 and kernel.metrics_stream_parts(f"metrics.self.{metric}") == ("self", metric)
    payload = kernel.capability_payload("0" * 40)
    feature_streams = {
        stream
        for item in payload["feature_primitives"]
        for stream in item.get("source_streams", [])
    }
    assert f"metrics.self.{metric}" in feature_streams


@pytest.mark.parametrize("metric", STITCHED)
def test_stitched_stream_accepts_1h_only(metric):
    kernel._validate_feature(_feature(f"metrics.self.{metric}", "1h"), 0)
    for interval in ("1d", "4h", "15m"):
        with pytest.raises(StrategyContractError) as caught:
            kernel._validate_feature(_feature(f"metrics.btc.{metric}", interval), 0)
        assert caught.value.path == "$.strategy_spec.features[0].interval"


def test_plain_metric_streams_keep_any_interval():
    kernel._validate_feature(_feature("metrics.self.funding_rate", "1d"), 0)
    kernel._validate_feature(_feature("metrics.self.funding_rate", "1h"), 0)


@pytest.mark.parametrize("metric", STITCHED)
def test_stitched_fetch_omits_exchange_and_ignores_extra_row_fields(monkeypatch, metric):
    calls = _capture(
        monkeypatch,
        [
            {"ts": 0, "value": "1.5", "source_interval": "1d", "source_ts": 0},
            {"ts": 3600, "value": "2", "source_interval": "1h", "source_ts": 3600},
        ],
    )
    requirement, spec = _requirement_and_spec(f"metrics.self.{metric}", "1h")
    rows = provider._fetch_artifact_features(requirement, spec, "BTCUSDT", 0, 7200)
    assert len(calls) == 1
    params = calls[0]["params"]
    assert "exchange" not in params
    assert (params["symbol"], params["metric"], params["interval"]) == ("BTC", metric, "1h")
    assert [(row["ts"], row["value"], row["available_at"]) for row in rows] == [
        (0, "1.5", 3600),
        (3600, "2", 7200),
    ]
