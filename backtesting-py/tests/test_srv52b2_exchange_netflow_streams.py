"""SRV52b-2b: metrics.btc/eth.exchange_netflow are declared v2 source streams.

The central /metrics carries <=3 day upstream gaps forward (balance carried,
netflow 0); longer gaps stay open and the contiguous rule still fails closed.
"""

import json
from pathlib import Path

import pytest

import cutie_backtesting_provider as provider
import strategy_kernel as kernel

NETFLOW = ("metrics.btc.exchange_netflow", "metrics.eth.exchange_netflow")
DAY = 86400
START = 1767225600  # 2026-01-01 UTC


def test_netflow_streams_declared_only_for_btc_eth():
    declared = set(kernel.METRICS_SOURCE_STREAMS)
    assert set(NETFLOW) <= declared
    assert "metrics.self.exchange_netflow" not in declared
    assert "metrics.market.exchange_netflow" not in declared
    payload = kernel.capability_payload("0" * 40)
    streams = {s for item in payload["feature_primitives"] for s in item.get("source_streams", ())}
    assert set(NETFLOW) <= streams


def test_capability_golden_lists_netflow():
    golden = json.loads((Path(__file__).parent / "fixtures" / "strategy_execution_capability_v1.json").read_text())
    text = json.dumps(golden)
    for stream in NETFLOW:
        assert stream in text


def _stub(monkeypatch, rows, calls):
    def fake_chunk(**kwargs):
        calls.append(kwargs)
        return [r for r in rows if kwargs["start_at"] <= r["ts"] <= kwargs["end_at"]]

    monkeypatch.setattr(provider, "_fetch_artifact_metric_chunk", fake_chunk)


def test_carried_series_passes_contiguity(monkeypatch):
    # Server-carried days (value 0, carried_forward) close the grid; D+1 as-of is kept.
    rows = [{"ts": START + i * DAY, "value": v} for i, v in enumerate(["5", "0", "0", "-12.5"])]
    calls = []
    _stub(monkeypatch, rows, calls)
    out = provider._fetch_metric_series(symbol="BTC", exchange=None, metric="exchange_netflow", interval="1d",
                                        start_at=START, end_at=START + 4 * DAY)
    assert [r["value"] for r in out] == ["5", "0", "0", "-12.5"]
    assert all(r["available_at"] == r["ts"] + DAY for r in out)
    assert calls[0]["exchange"] is None and calls[0]["symbol"] == "BTC"


def test_open_long_gap_fails_closed(monkeypatch):
    rows = [{"ts": START, "value": "1"}, {"ts": START + 5 * DAY, "value": "2"}]
    _stub(monkeypatch, rows, [])
    with pytest.raises(kernel.StrategyContractError):
        provider._fetch_metric_series(symbol="ETH", exchange=None, metric="exchange_netflow", interval="1d",
                                      start_at=START, end_at=START + 6 * DAY)
