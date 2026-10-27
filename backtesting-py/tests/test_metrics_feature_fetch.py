"""JES21-B3b 二期 K1: ``metrics.<scope>.<metric>`` feature streams are fetched
from the central ``/metrics`` with the scope mapped to a symbol and no
exchange (IMPL §3.4); the legacy ``coinglass.*`` query stays byte-identical."""

from __future__ import annotations

import io
import json
import sys
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cutie_backtesting_provider as provider  # noqa: E402


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _capture(monkeypatch, items: list[dict]) -> list[dict]:
    calls: list[dict] = []

    def fake_open(request, timeout=None):
        query = urllib.parse.urlsplit(request.full_url).query
        calls.append({"query": query, "params": dict(urllib.parse.parse_qsl(query))})
        body = {"err_code": 100, "data": {"items": items}}
        return _Response(json.dumps(body).encode("utf-8"))

    monkeypatch.setattr(provider, "CENTRAL_MARKET_DATA_URL", "http://center.test")
    monkeypatch.setattr(provider, "CENTRAL_MARKET_DATA_TOKEN", "t")
    monkeypatch.setattr(provider._CENTRAL_HTTP_OPENER, "open", fake_open)
    return calls


def _requirement_and_spec(source_stream: str, interval: str) -> tuple[dict, dict]:
    feature = {"source_stream": source_stream, "interval": interval}
    requirement = {
        "stream_id": f"{source_stream}.{interval}",
        "interval": interval,
        "exchange": "auto",
    }
    return requirement, {"features": [feature]}


@pytest.mark.parametrize(
    ("stream", "symbol"),
    [
        ("metrics.self.funding_rate", "SOL"),
        ("metrics.market.fear_greed_index", "MARKET"),
        ("metrics.btc.ahr999", "BTC"),
        ("metrics.eth.open_interest_aggregated", "ETH"),
    ],
)
def test_metrics_scope_maps_symbol_and_omits_exchange(monkeypatch, stream, symbol):
    calls = _capture(
        monkeypatch,
        [{"ts": 0, "value": "20"}, {"ts": 86400, "value": "30"}],
    )
    requirement, spec = _requirement_and_spec(stream, "1d")
    rows = provider._fetch_artifact_features(requirement, spec, "SOLUSDT", 0, 172800)
    assert len(calls) == 1
    params = calls[0]["params"]
    assert "exchange" not in params
    assert params["symbol"] == symbol
    assert params["metric"] == stream.split(".")[2]
    assert params["interval"] == "1d"
    assert [(row["ts"], row["value"], row["available_at"]) for row in rows] == [
        (0, "20", 86400),
        (86400, "30", 172800),
    ]


def test_legacy_coinglass_stream_query_is_unchanged(monkeypatch):
    calls = _capture(monkeypatch, [{"ts": 0, "value": "1"}, {"ts": 3600, "value": "2"}])
    requirement, spec = _requirement_and_spec("coinglass.futures_cvd", "1h")
    requirement["exchange"] = "binance"
    provider._fetch_artifact_features(requirement, spec, "BTCUSDT", 0, 7200)
    assert calls[0]["query"] == urllib.parse.urlencode(
        {
            "symbol": "BTC",
            "metric": "futures_cvd",
            "interval": "1h",
            "exchange": "Binance",
            "start_ts": 0,
            "end_ts": 7199,
            "limit": 5000,
        }
    )


def test_metrics_gap_fails_closed(monkeypatch):
    _capture(monkeypatch, [{"ts": 0, "value": "20"}, {"ts": 172800, "value": "30"}])
    requirement, spec = _requirement_and_spec("metrics.market.fear_greed_index", "1d")
    with pytest.raises(provider.StrategyContractError) as caught:
        provider._fetch_artifact_features(requirement, spec, "BTCUSDT", 0, 259200)
    assert caught.value.code == provider.ERR_COVERAGE_INCOMPLETE
