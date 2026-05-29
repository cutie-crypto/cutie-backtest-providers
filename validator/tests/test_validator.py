"""Validator regression tests (IMPL W3.9 §8).

A conforming mock provider must PASS all 10 checks. A set of deliberately
non-conforming mock providers must each FAIL the corresponding check — this
proves PASS is meaningful, not vacuous.

The validator drives FastAPI apps in-process via its ASGI transport, so no
sockets or external dependencies are required.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, Optional

import pytest
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from cutie_backtest_provider_validator.validator import ProviderValidator, SmokeParams

TOKEN = "test-token"

CONFORMING_TOOL: Dict[str, Any] = {
    "tool_id": "mock.backtester.default",
    "kind": "external_http",
    "name": "Mock Backtester",
    "description": "A conforming mock provider.",
    "wrapper_type": "local_cli",
    "provider_name": "Mock Provider",
    "engine_name": "MockEngine",
    "engine_version": "1.0.0",
    "data_source": {
        "type": "provider_reported",
        "name": "mock_data",
        "description": "Mock OHLCV.",
        "coverage_hint": "BTCUSDT 1h",
        "external_unverified": True,
    },
    "supported_symbols": ["BTCUSDT"],
    "markets": ["spot"],
    "timeframes": ["1h", "4h"],
    "is_default": True,
    "execution": {
        "mode": "sync",
        "timeout_ms": 120000,
        "max_range_days": 365,
        "max_parallel_runs": 1,
        "async_supported": False,
    },
    "adapter": {
        "requires_manual_export": False,
        "working_dir_policy": "ephemeral_or_provider_managed",
    },
    "param_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "strategy_name": {"type": "string", "default": "default"},
        },
    },
    "output_schema": {
        "metrics": ["total_return_pct"],
        "artifacts": ["report_url"],
        "series": ["equity_curve"],
        "tables": ["trades"],
    },
    "report_capabilities": {
        "report_url": True,
        "scope": "local_machine_only",
        "formats": ["html"],
        "retention_hint": "last_100_runs",
    },
    "failure_codes": ["INVALID_PARAMS", "NO_DATA"],
    "security": {
        "network_scope": "openclaw_hermes_local_or_private",
        "requires_user_secret": False,
        "secrets_stay_local": True,
        "live_trading": False,
        "filesystem_paths_exposed": False,
    },
}

CONFORMING_SUCCESS: Dict[str, Any] = {
    "schema": "cutie.external_backtest.response.v1",
    "result_status": "success",
    "provider_name": "Mock Provider",
    "provider_run_id": "mock_run_1",
    "engine_name": "MockEngine",
    "engine_version": "1.0.0",
    "data_source": "mock_data",
    "result_hash": "sha256:abc",
    "report_url": "/reports/mock_run_1.html",
    "report_url_scope": "local_machine_only",
    "metrics": {
        "total_return_pct": 7.25,
        "win_rate_pct": 61.9,
        "max_drawdown_pct": 4.8,
        "trade_count": 42,
    },
    "initial_capital": "10000",
    "equity_curve": [{"t": 1704067200, "equity": "10000"}],
    "trades": [{"side": "long", "entry_at": 1704067200, "exit_at": 1704153600, "pnl": "120.50"}],
    "assumptions": {"fee_bps": "10", "slippage_bps": "5", "real_market_data": True},
    "limitations": {"verification": "external_unverified", "verified_by_cutie": False},
    "raw_report": {"provider_summary": "ok"},
}


def make_provider(
    *,
    catalog_schema: str = "cutie.backtest_provider_catalog.v1",
    tool_overrides: Optional[Dict[str, Any]] = None,
    success_overrides: Optional[Dict[str, Any]] = None,
    health_body: Optional[Dict[str, Any]] = None,
    require_auth: bool = True,
    result_status: str = "success",
    failed_body: Optional[Dict[str, Any]] = None,
) -> FastAPI:
    app = FastAPI()
    tool = copy.deepcopy(CONFORMING_TOOL)
    if tool_overrides:
        tool.update(tool_overrides)
    success = copy.deepcopy(CONFORMING_SUCCESS)
    if success_overrides:
        success.update(success_overrides)
    health = health_body if health_body is not None else {
        "ok": True,
        "provider_id": "mock",
        "engine_name": "MockEngine",
        "engine_version": "1.0.0",
    }

    def check_auth(authorization: Optional[str]) -> None:
        if not require_auth:
            return
        if not authorization or authorization != f"Bearer {TOKEN}":
            raise HTTPException(status_code=401, detail="Invalid token")

    @app.get("/health")
    async def _health() -> Any:
        return JSONResponse(health)

    @app.get("/catalog")
    async def _catalog(authorization: Optional[str] = Header(default=None)) -> Any:
        check_auth(authorization)
        return JSONResponse(
            {
                "schema": catalog_schema,
                "provider": {
                    "provider_id": "mock",
                    "provider_name": "Mock Provider",
                    "provider_version": "1.0.0",
                },
                "tools": [tool],
            }
        )

    @app.post("/cutie/backtest")
    async def _backtest(
        request: Request, authorization: Optional[str] = Header(default=None)
    ) -> Any:
        check_auth(authorization)
        await request.json()
        if result_status == "failed":
            return JSONResponse(failed_body or {
                "schema": "cutie.external_backtest.response.v1",
                "result_status": "failed",
                "provider_name": "Mock Provider",
                "engine_name": "MockEngine",
                "engine_version": "1.0.0",
                "data_source": "mock_data",
                "error_type": "NO_DATA",
                "error_message": "no data",
                "assumptions": {},
                "limitations": {"reason": "data_missing"},
                "raw_report": {},
            })
        return JSONResponse(success)

    return app


def run_validator(app: FastAPI, token: Optional[str] = TOKEN, tool_id: Optional[str] = None):
    transport = _asgi_transport(app)
    smoke = SmokeParams(
        tool_id=tool_id,
        symbol="BTCUSDT",
        timeframe="1h",
        market="spot",
        start_at=1704067200,
        end_at=1704672000,
        provider_params={},
        instruction="",
    )
    validator = ProviderValidator(transport, "mock://provider", token, smoke)
    return validator.run()


def _asgi_transport(app: FastAPI) -> ValidatorTransport:
    # Build an ASGI transport directly from the in-memory app object.
    from cutie_backtest_provider_validator.transport import _AsgiTransport

    return _AsgiTransport(app, 30.0)


def check_by_id(report, check_id: int):
    return next(c for c in report.checks if c.check_id == check_id)


# -- positive ------------------------------------------------------------


def test_conforming_provider_passes_all_checks():
    report = run_validator(make_provider())
    assert report.ok, [e for e in report.errors]
    for c in report.checks:
        assert c.passed, (c.check_id, c.errors)


# -- check 2: auth -------------------------------------------------------


def test_catalog_without_auth_rejection_required():
    # Provider does NOT require auth -> no-token request returns 200 -> check 2 fails.
    report = run_validator(make_provider(require_auth=False))
    assert not check_by_id(report, 2).passed


def test_catalog_wrong_schema_fails():
    report = run_validator(make_provider(catalog_schema="cutie.backtest_provider_catalog.v2"))
    assert not check_by_id(report, 2).passed


# -- check 3: catalog fields --------------------------------------------


def test_missing_markets_fails_check_3():
    report = run_validator(make_provider(tool_overrides={"markets": []}))
    assert not check_by_id(report, 3).passed


def test_live_trading_true_fails_check_3():
    tool = copy.deepcopy(CONFORMING_TOOL)
    tool["security"] = {**tool["security"], "live_trading": True}
    report = run_validator(make_provider(tool_overrides={"security": tool["security"]}))
    assert not check_by_id(report, 3).passed


def test_param_schema_with_oneof_fails_check_3():
    report = run_validator(
        make_provider(tool_overrides={"param_schema": {"oneOf": [{"type": "string"}]}})
    )
    assert not check_by_id(report, 3).passed


def test_report_scope_invalid_fails_check_3():
    tool = copy.deepcopy(CONFORMING_TOOL)
    tool["report_capabilities"] = {**tool["report_capabilities"], "scope": "public"}
    report = run_validator(make_provider(tool_overrides={"report_capabilities": tool["report_capabilities"]}))
    assert not check_by_id(report, 3).passed


# -- check 4: secret scrub ----------------------------------------------


def test_catalog_with_string_secret_fails_check_4():
    report = run_validator(
        make_provider(tool_overrides={"vendor_api_key": "AKIAIOSFODNN7EXAMPLE1234aB9"})
    )
    assert not check_by_id(report, 4).passed


def test_catalog_with_local_path_fails_check_4():
    report = run_validator(
        make_provider(tool_overrides={"note": "results at /Users/kol/secret/out.json"})
    )
    assert not check_by_id(report, 4).passed


def test_boolean_requires_user_secret_does_not_fail_check_4():
    # requires_user_secret is a declared boolean schema flag, not a secret.
    report = run_validator(make_provider())
    assert check_by_id(report, 4).passed


# -- check 6: success response ------------------------------------------


def test_money_field_as_float_fails_check_6():
    report = run_validator(
        make_provider(success_overrides={"initial_capital": 10000.0})
    )
    assert not check_by_id(report, 6).passed


def test_equity_as_float_fails_check_6():
    bad = copy.deepcopy(CONFORMING_SUCCESS)
    bad["equity_curve"] = [{"t": 1704067200, "equity": 10000.0}]
    report = run_validator(make_provider(success_overrides={"equity_curve": bad["equity_curve"]}))
    assert not check_by_id(report, 6).passed


def test_ratio_metric_null_allowed_check_6():
    bad = copy.deepcopy(CONFORMING_SUCCESS)
    bad["metrics"] = {**bad["metrics"], "win_rate_pct": None}
    report = run_validator(make_provider(success_overrides={"metrics": bad["metrics"]}))
    assert check_by_id(report, 6).passed


def test_missing_required_field_fails_check_6():
    bad = copy.deepcopy(CONFORMING_SUCCESS)
    del bad["assumptions"]
    report = run_validator(make_provider(success_overrides={"assumptions": None}))
    # assumptions=None is not a dict -> fails
    assert not check_by_id(report, 6).passed


# -- check 7: failed response -------------------------------------------


def test_standard_business_failure_passes_check_7():
    report = run_validator(make_provider(result_status="failed"))
    assert check_by_id(report, 7).passed
    assert check_by_id(report, 5).passed


def test_nonstandard_error_type_fails_check_7():
    report = run_validator(
        make_provider(
            result_status="failed",
            failed_body={
                "schema": "cutie.external_backtest.response.v1",
                "result_status": "failed",
                "provider_name": "Mock Provider",
                "engine_name": "MockEngine",
                "engine_version": "1.0.0",
                "data_source": "mock_data",
                "error_type": "totally_made_up",
                "error_message": "x",
                "assumptions": {},
                "limitations": {},
                "raw_report": {},
            },
        )
    )
    assert not check_by_id(report, 7).passed


# -- check 8: report_url ------------------------------------------------


def test_public_report_url_fails_check_8():
    report = run_validator(
        make_provider(success_overrides={"report_url": "https://evil.example.com/r.html"})
    )
    assert not check_by_id(report, 8).passed


def test_loopback_report_url_warns_but_passes_check_8():
    report = run_validator(
        make_provider(success_overrides={"report_url": "http://127.0.0.1:8765/reports/x.html"})
    )
    c8 = check_by_id(report, 8)
    assert c8.passed
    assert any(w["code"] == "LOCAL_REPORT_URL" for w in c8.warnings)


def test_absolute_path_report_url_fails_check_8():
    report = run_validator(
        make_provider(success_overrides={"report_url": "/Users/kol/reports/x.html"})
    )
    assert not check_by_id(report, 8).passed


# -- check 9: wrapper_type ----------------------------------------------


def test_unknown_wrapper_type_fails_check_9():
    report = run_validator(make_provider(tool_overrides={"wrapper_type": "weird_wrapper"}))
    assert not check_by_id(report, 9).passed


def test_manual_export_parser_fails_check_9():
    report = run_validator(make_provider(tool_overrides={"wrapper_type": "manual_export_parser"}))
    assert not check_by_id(report, 9).passed


def test_cloud_api_proxy_fails_check_9():
    report = run_validator(make_provider(tool_overrides={"wrapper_type": "cloud_api_proxy"}))
    assert not check_by_id(report, 9).passed


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
