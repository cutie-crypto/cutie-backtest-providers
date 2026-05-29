"""End-to-end smoke tests driving /health /catalog /cutie/backtest.

Uses the FastAPI TestClient against the template app. The default adapter is a
deterministic "echo" backtest, which is enough to exercise the full request ->
response path. A fake-adapter test also forces a business failure.
"""

from __future__ import annotations

import importlib
import os

import pytest
from starlette.testclient import TestClient

from cutie_byo_provider.contract import CATALOG_SCHEMA, RESPONSE_SCHEMA

TOKEN = "test-token"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Isolate reports dir + set a token, then reload settings/app so they pick up env.
    monkeypatch.setenv("CUTIE_BACKTEST_PROVIDER_TOKEN", TOKEN)
    monkeypatch.setenv("CUTIE_BACKTEST_REPORTS_DIR", str(tmp_path / "reports"))

    import cutie_byo_provider.settings as settings_mod
    importlib.reload(settings_mod)
    import cutie_byo_provider.reports as reports_mod
    importlib.reload(reports_mod)
    import cutie_byo_provider.adapter as adapter_mod
    importlib.reload(adapter_mod)
    import cutie_byo_provider.app as app_mod
    importlib.reload(app_mod)

    with TestClient(app_mod.app) as c:
        c._app_mod = app_mod  # type: ignore[attr-defined]
        c._adapter_mod = adapter_mod  # type: ignore[attr-defined]
        yield c


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def _request_body():
    return {
        "schema": "cutie.external_backtest.request.v1",
        "backtest": {
            "schema": "cutie.backtest_task.v1",
            "scene": "backtest_run",
            "task_type": "strategy.backtest.run",
            "run_id": "318265150907879424",
            "provider_tool_id": "my.backtester.default",
            "provider_params": {},
            "symbol": "BTCUSDT",
            "market": "spot",
            "timeframe": "1h",
            "start_at": 1771996800,
            "end_at": 1772601600,
            "initial_capital": "10000",
            "fee_bps": "10",
            "slippage_bps": "5",
        },
        "provider": {},
    }


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


def test_health_no_auth_required(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "provider_id" in body


# ---------------------------------------------------------------------------
# /catalog
# ---------------------------------------------------------------------------


def test_catalog_requires_auth(client):
    resp = client.get("/catalog")
    assert resp.status_code == 401
    assert resp.json()["error_type"] == "AUTH_FAILED"


def test_catalog_with_auth(client):
    resp = client.get("/catalog", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["schema"] == CATALOG_SCHEMA
    assert body["tools"]
    tool = body["tools"][0]
    assert tool["is_default"] is True
    assert "health" not in tool


# ---------------------------------------------------------------------------
# /cutie/backtest
# ---------------------------------------------------------------------------


def test_backtest_requires_auth(client):
    resp = client.post("/cutie/backtest", json=_request_body())
    assert resp.status_code == 401


def test_backtest_success_smoke(client):
    resp = client.post("/cutie/backtest", json=_request_body(), headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["schema"] == RESPONSE_SCHEMA
    assert body["result_status"] == "success"
    # required fields present
    for field in (
        "metrics",
        "equity_curve",
        "trades",
        "assumptions",
        "limitations",
        "raw_report",
    ):
        assert field in body
    # money fields are decimal strings
    for point in body["equity_curve"]:
        assert isinstance(point["equity"], str)
    # report_url relative
    if body.get("report_url"):
        assert body["report_url"].startswith("/")
        assert "://" not in body["report_url"]


def test_backtest_invalid_json_is_invalid_request(client):
    resp = client.post(
        "/cutie/backtest",
        content=b"not json",
        headers={**_auth(), "Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json()["error_type"] == "INVALID_REQUEST"


def test_backtest_missing_symbol_is_business_failure(client):
    body = _request_body()
    body["backtest"]["symbol"] = ""
    resp = client.post("/cutie/backtest", json=body, headers=_auth())
    assert resp.status_code == 200
    out = resp.json()
    assert out["result_status"] == "failed"
    assert out["error_type"] == "INVALID_PARAMS"


def test_backtest_unsupported_timeframe(client):
    body = _request_body()
    body["backtest"]["timeframe"] = "7m"
    resp = client.post("/cutie/backtest", json=body, headers=_auth())
    out = resp.json()
    assert out["result_status"] == "failed"
    assert out["error_type"] == "TIMEFRAME_UNSUPPORTED"


# ---------------------------------------------------------------------------
# Fake adapter: force a business failure path through the real app
# ---------------------------------------------------------------------------


def test_fake_adapter_business_failure(client):
    from cutie_byo_provider.contract import business_failure

    adapter_mod = client._adapter_mod  # type: ignore[attr-defined]

    def fake_run_backtest(request):
        return business_failure(
            error_type="NO_DATA",
            error_message="No OHLCV data for requested range",
            provider_name=adapter_mod.PROVIDER_NAME,
            engine_name=adapter_mod.ENGINE_NAME,
            engine_version=adapter_mod.engine_version(),
            data_source=adapter_mod.DATA_SOURCE,
            reason="data_missing",
        )

    adapter_mod.run_backtest = fake_run_backtest
    resp = client.post("/cutie/backtest", json=_request_body(), headers=_auth())
    assert resp.status_code == 200
    out = resp.json()
    assert out["result_status"] == "failed"
    assert out["error_type"] == "NO_DATA"
    assert out["limitations"]["reason"] == "data_missing"


def test_fake_adapter_exception_is_engine_error(client):
    adapter_mod = client._adapter_mod  # type: ignore[attr-defined]

    def boom(request):
        raise RuntimeError("engine crashed")

    adapter_mod.run_backtest = boom
    resp = client.post("/cutie/backtest", json=_request_body(), headers=_auth())
    out = resp.json()
    assert out["result_status"] == "failed"
    assert out["error_type"] == "ENGINE_ERROR"


def test_report_retention(client):
    """Generating many runs prunes report files to MAX_REPORTS."""
    import cutie_byo_provider.settings as settings_mod

    settings_mod.MAX_REPORTS = 3
    reports_mod = importlib.import_module("cutie_byo_provider.reports")
    for i in range(6):
        reports_mod.write_report(f"r{i}.json", "{}")
    files = list(settings_mod.REPORTS_DIR.glob("*.json"))
    assert len(files) <= 3
