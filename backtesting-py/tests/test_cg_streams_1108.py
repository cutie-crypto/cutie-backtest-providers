"""PROV-CG-1108: v2 whitelist registers the Coinglass CG-A/CG-B streams.

CG-A (self scope): 大户持仓比、资金费率成交量加权、单交易对多空爆仓, plus the
server-side ``*_1h_stitched`` variants. CG-B (self scope): taker buy/sell volume
and basis close/change, which had no base stream before, plus the stitched
``futures_aggregated_cvd`` row. BTC-only daily flows: ``coinbase_premium_index``
and (PROV-ETF-FLOW-1108) ``etf_flow`` — the provider stamps
``available_at = ts + interval step``, so a 1d row is only visible from the next
UTC day (T+1 00:00 UTC), and both streams are 1d-only.

Anything outside the list (near names such as ``coinbase_premium``,
``etf_netflow``, ``metrics.eth.etf_flow``) stays unsupported: the capability gate
rejects the requirement.
"""

import json
from pathlib import Path

import pytest

import cutie_backtesting_provider as provider
import strategy_kernel as kernel

SELF_BASE = (
    "metrics.self.funding_rate_vol_weight",
    "metrics.self.futures_aggregated_taker_buy_volume",
    "metrics.self.futures_aggregated_taker_sell_volume",
    "metrics.self.futures_basis_change",
    "metrics.self.futures_basis_close",
    "metrics.self.liquidation_long_pair",
    "metrics.self.liquidation_short_pair",
    "metrics.self.top_long_short_position_ratio",
)
SELF_STITCHED = (
    "metrics.self.funding_rate_vol_weight_1h_stitched",
    "metrics.self.futures_aggregated_cvd_1h_stitched",
    "metrics.self.futures_aggregated_taker_buy_volume_1h_stitched",
    "metrics.self.futures_aggregated_taker_sell_volume_1h_stitched",
    "metrics.self.futures_basis_change_1h_stitched",
    "metrics.self.futures_basis_close_1h_stitched",
    "metrics.self.liquidation_long_pair_1h_stitched",
    "metrics.self.liquidation_short_pair_1h_stitched",
    "metrics.self.top_long_short_position_ratio_1h_stitched",
)
BTC_FLOWS = ("metrics.btc.coinbase_premium_index", "metrics.btc.etf_flow")
NEW_STREAMS = (*SELF_BASE, *SELF_STITCHED, *BTC_FLOWS)
NEAR_MISS = (
    "metrics.btc.coinbase_premium",
    "metrics.btc.etf_netflow",
    "metrics.self.top_long_short_position_ratio_1h",
    "metrics.btc.liquidation_long_pair",
    "metrics.eth.etf_flow",
    "metrics.btc.futures_aggregated_cvd_1h_stitched",
)
DAY = 86400
HOUR = 3600
START = 1767225600  # 2026-01-01 UTC


def _feature_requirement(stream, interval, value_kind="level"):
    return {
        "primitive": "rolling_extreme",
        "version": "1",
        "source_stream": stream,
        "interval": interval,
        "value_kind": value_kind,
        "output_type": "decimal",
    }


def _requirements(*features):
    return {
        "operators": [],
        "features": list(features),
        "data_sources": [],
        "cost_models": [],
        "data_transforms": [],
        "result_schemas": [],
        "coverage_schemas": [],
        "trace_schemas": [],
        "evidence_schemas": [],
    }


def _feature(source_stream, interval):
    return {
        "key": "m_cg",
        "primitive": "rolling_extreme",
        "primitive_version": "1",
        "source_stream": source_stream,
        "interval": interval,
        "value_kind": "level",
        "output_type": "decimal",
        "params": {"mode": "max", "window_bars": 1},
        "required": True,
    }


def test_new_streams_declared_at_the_expected_scope():
    declared = set(kernel.METRICS_SOURCE_STREAMS)
    assert set(NEW_STREAMS) <= declared
    for stream in NEW_STREAMS:
        assert kernel.metrics_stream_parts(stream) == tuple(stream.split(".", 2)[1:])


def test_btc_flows_are_btc_only_and_not_stitched():
    declared = set(kernel.METRICS_SOURCE_STREAMS)
    for metric in kernel._METRICS_BTC_FLOWS:
        assert f"metrics.btc.{metric}" in declared
        assert f"metrics.eth.{metric}" not in declared
        assert f"metrics.self.{metric}" not in declared
        assert f"metrics.btc.{metric}_1h_stitched" not in declared


def test_self_streams_are_self_only():
    declared = set(kernel.METRICS_SOURCE_STREAMS)
    for stream in SELF_BASE:
        metric = stream.rsplit(".", 1)[1]
        assert f"metrics.btc.{metric}" not in declared
        assert f"metrics.eth.{metric}" not in declared
        assert f"metrics.market.{metric}" not in declared


def test_capability_declaration_and_golden_list_the_new_streams():
    payload = kernel.capability_payload("0" * 40)
    streams = {s for item in payload["feature_primitives"] for s in item.get("source_streams", ())}
    assert set(NEW_STREAMS) <= streams
    golden_path = Path(__file__).parent / "fixtures" / "strategy_execution_capability_v1.json"
    golden = json.loads(golden_path.read_text())
    text = json.dumps(golden["capability"])
    for stream in NEW_STREAMS:
        assert stream in text
    assert "metrics.btc.etf_flow" in streams
    assert "metrics.btc.etf_flow" in text
    assert golden["sha256"] == kernel.capability_hash(golden["capability"])


def test_etf_flow_registered_at_btc_scope_only():
    stream = "metrics.btc.etf_flow"
    assert stream in kernel.METRICS_SOURCE_STREAMS
    payload = kernel.capability_payload("0" * 40)
    streams = {s for item in payload["feature_primitives"] for s in item.get("source_streams", ())}
    assert stream in streams
    assert "metrics.eth.etf_flow" not in kernel.METRICS_SOURCE_STREAMS
    assert "metrics.self.etf_flow" not in kernel.METRICS_SOURCE_STREAMS
    requirement = _requirements(_feature_requirement(stream, "1d", "flow"))
    kernel._check_capability(requirement, payload)


def test_capability_gate_accepts_new_streams():
    capability = kernel.capability_payload("0" * 40)
    features = [
        _feature_requirement(stream, "1h" if stream.endswith("_1h_stitched") else "1d")
        for stream in NEW_STREAMS
    ]
    kernel._check_capability(_requirements(*features), capability)


@pytest.mark.parametrize("stream", NEAR_MISS)
def test_near_miss_names_stay_rejected_by_capability_gate(stream):
    capability = kernel.capability_payload("0" * 40)
    requirement = _requirements(_feature_requirement(stream, "1d"))
    with pytest.raises(kernel.StrategyContractError) as excinfo:
        kernel._check_capability(requirement, capability)
    assert excinfo.value.code == kernel.ERR_SPEC_UNSUPPORTED


def test_stitched_streams_are_1h_only():
    for stream in SELF_STITCHED:
        kernel._validate_feature(_feature(stream, "1h"), 0)
        with pytest.raises(kernel.StrategyContractError) as caught:
            kernel._validate_feature(_feature(stream, "1d"), 0)
        assert caught.value.path == "$.strategy_spec.features[0].interval"


def test_btc_daily_flow_rejects_1h():
    stream = "metrics.btc.coinbase_premium_index"
    kernel._validate_feature(_feature(stream, "1d"), 0)
    with pytest.raises(kernel.StrategyContractError) as caught:
        kernel._validate_feature(_feature(stream, "1h"), 0)
    assert caught.value.code == kernel.ERR_SPEC_INVALID
    assert caught.value.path == "$.strategy_spec.features[0].interval"


def test_etf_flow_rejects_1h():
    stream = "metrics.btc.etf_flow"
    kernel._validate_feature(_feature(stream, "1d"), 0)
    with pytest.raises(kernel.StrategyContractError) as caught:
        kernel._validate_feature(_feature(stream, "1h"), 0)
    assert caught.value.code == kernel.ERR_SPEC_INVALID
    assert caught.value.path == "$.strategy_spec.features[0].interval"


def _stub(monkeypatch, rows, calls):
    def fake_chunk(**kwargs):
        calls.append(kwargs)
        return [r for r in rows if kwargs["start_at"] <= r["ts"] <= kwargs["end_at"]]

    monkeypatch.setattr(provider, "_fetch_artifact_metric_chunk", fake_chunk)


def test_btc_daily_flow_is_t_plus_1_asof(monkeypatch):
    # 美股收盘后才有值：1d 行只在 ts + 1d 之后可见，取数层不给逐流特例。
    rows = [{"ts": START + i * DAY, "value": v} for i, v in enumerate(["12.5", "-3", "0"])]
    calls = []
    _stub(monkeypatch, rows, calls)
    out = provider._fetch_metric_series(
        symbol="BTC",
        exchange=None,
        metric="coinbase_premium_index",
        interval="1d",
        start_at=START,
        end_at=START + 3 * DAY,
    )
    assert [r["value"] for r in out] == ["12.5", "-3", "0"]
    assert all(r["available_at"] == r["ts"] + DAY for r in out)
    assert calls[0]["exchange"] is None and calls[0]["symbol"] == "BTC"


def test_self_scope_stream_resolves_to_base_asset(monkeypatch):
    rows = [{"ts": START + i * HOUR, "value": str(i)} for i in range(3)]
    calls = []
    _stub(monkeypatch, rows, calls)
    out = provider._fetch_metric_series(
        symbol="SOL",
        exchange=None,
        metric="futures_basis_close",
        interval="1h",
        start_at=START,
        end_at=START + 3 * HOUR,
    )
    assert [r["value"] for r in out] == ["0", "1", "2"]
    assert all(r["available_at"] == r["ts"] + HOUR for r in out)
    assert calls[0]["symbol"] == "SOL" and calls[0]["exchange"] is None
