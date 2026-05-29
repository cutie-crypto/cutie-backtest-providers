"""Contract compliance tests for catalog and response schemas (IMPL §5.1, §6)."""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from cutie_byo_provider import adapter
from cutie_byo_provider.contract import (
    ALLOWED_EXECUTION_MODES,
    ALLOWED_KINDS,
    ALLOWED_REPORT_SCOPES,
    ALLOWED_WRAPPER_TYPES,
    CATALOG_SCHEMA,
    ERROR_TYPES,
    RESPONSE_SCHEMA,
    BacktestRequest,
    BacktestResult,
    CatalogResponse,
    ProviderInfo,
    business_failure,
    decimal_str,
    json_safe,
    parse_decimal,
    success_response,
)
from cutie_byo_provider import security

MONEY_RESULT_FIELDS = ("equity",)  # money keys inside equity_curve points


def _build_catalog_payload() -> dict:
    response = CatalogResponse(
        schema=CATALOG_SCHEMA,
        provider=ProviderInfo(
            provider_id=adapter.PROVIDER_ID,
            provider_name=adapter.PROVIDER_NAME,
            provider_version=adapter.PROVIDER_VERSION,
        ),
        tools=adapter.list_tools(),
    )
    return response.model_dump(by_alias=True, exclude_none=True)


# ---------------------------------------------------------------------------
# Catalog compliance (IMPL §5.1)
# ---------------------------------------------------------------------------


def test_catalog_schema_id():
    payload = _build_catalog_payload()
    assert payload["schema"] == CATALOG_SCHEMA


def test_catalog_no_health_field():
    """health must NOT appear in provider catalog (connector derives it)."""
    payload = _build_catalog_payload()
    for tool in payload["tools"]:
        assert "health" not in tool


def test_catalog_uses_is_default_not_default():
    payload = _build_catalog_payload()
    for tool in payload["tools"]:
        assert "is_default" in tool
        assert "default" not in tool


def test_catalog_at_most_one_default():
    payload = _build_catalog_payload()
    defaults = [t for t in payload["tools"] if t.get("is_default")]
    assert len(defaults) <= 1


def test_catalog_max_ten_tools():
    payload = _build_catalog_payload()
    assert len(payload["tools"]) <= 10


def test_catalog_tool_field_enums():
    payload = _build_catalog_payload()
    for tool in payload["tools"]:
        assert tool["kind"] in ALLOWED_KINDS
        assert tool["wrapper_type"] in ALLOWED_WRAPPER_TYPES
        assert tool["execution"]["mode"] in ALLOWED_EXECUTION_MODES
        assert tool["report_capabilities"]["scope"] in ALLOWED_REPORT_SCOPES
        assert len(tool["tool_id"]) <= 128 and len(tool["tool_id"]) >= 1
        assert isinstance(tool["markets"], list) and tool["markets"]
        assert isinstance(tool["timeframes"], list) and tool["timeframes"]


def test_catalog_live_trading_must_be_false():
    payload = _build_catalog_payload()
    for tool in payload["tools"]:
        assert tool["security"]["live_trading"] is False


def test_catalog_param_schema_subset():
    """param_schema must use the allowed JSON Schema subset (IMPL §5.1)."""
    allowed_types = {"object", "string", "number", "integer", "boolean"}
    payload = _build_catalog_payload()
    for tool in payload["tools"]:
        schema = tool["param_schema"]
        assert schema.get("type") == "object"
        for prop in schema.get("properties", {}).values():
            assert prop.get("type") in allowed_types


def test_catalog_has_no_secrets_or_paths():
    payload = _build_catalog_payload()
    findings = security.scan_for_secrets(payload)
    assert findings == [], f"catalog leaked sensitive data: {findings}"


# ---------------------------------------------------------------------------
# Response compliance (IMPL §6.2)
# ---------------------------------------------------------------------------


def _example_success_payload() -> dict:
    result = BacktestResult(
        provider_run_id="run_1",
        initial_capital=decimal_str(Decimal("10000"), places=2),
        report_url="/reports/run_1.json",
        metrics={
            "total_return_pct": 7.25,
            "win_rate_pct": 61.9,
            "max_drawdown_pct": 4.8,
            "trade_count": 42,
        },
        equity_curve=[{"t": 1771996800, "equity": "10000.00"}],
        trades=[{"side": "long", "entry_at": 1, "exit_at": 2, "pnl": "12.50"}],
        assumptions={"fee_bps": "10", "slippage_bps": "5"},
        limitations={"verification": "external_unverified", "verified_by_cutie": False},
        raw_report={"provider_summary": "ok"},
    )
    return success_response(
        provider_name="P",
        engine_name="E",
        engine_version="1",
        data_source="d",
        result=result,
    )


def test_success_response_schema_id():
    payload = _example_success_payload()
    assert payload["schema"] == RESPONSE_SCHEMA
    assert payload["result_status"] == "success"


def test_success_response_required_fields():
    payload = _example_success_payload()
    required = [
        "result_status",
        "provider_name",
        "engine_name",
        "engine_version",
        "data_source",
        "metrics",
        "equity_curve",
        "trades",
        "assumptions",
        "limitations",
        "raw_report",
    ]
    for field in required:
        assert field in payload, f"missing required field {field}"
    assert isinstance(payload["metrics"], dict)
    assert isinstance(payload["equity_curve"], list)
    assert isinstance(payload["trades"], list)
    assert isinstance(payload["assumptions"], dict)
    assert isinstance(payload["limitations"], dict)
    assert isinstance(payload["raw_report"], dict)


def test_success_money_fields_are_decimal_strings():
    payload = _example_success_payload()
    # equity inside equity_curve must be a string
    for point in payload["equity_curve"]:
        assert isinstance(point["equity"], str)
    # trade pnl must be a string
    for trade in payload["trades"]:
        if trade.get("pnl") is not None:
            assert isinstance(trade["pnl"], str)
    # initial_capital must be a string
    assert isinstance(payload["initial_capital"], str)


def test_success_pct_metrics_are_numbers_not_nan():
    payload = _example_success_payload()
    for key, value in payload["metrics"].items():
        if key.endswith("_pct"):
            assert isinstance(value, (int, float))
            assert not isinstance(value, bool)
            assert math.isfinite(value)


def test_report_url_is_relative():
    payload = _example_success_payload()
    url = payload["report_url"]
    assert url.startswith("/")
    assert "://" not in url


# ---------------------------------------------------------------------------
# Business failure compliance (IMPL §6.3)
# ---------------------------------------------------------------------------


def test_business_failure_uses_uppercase_protocol_code():
    payload = business_failure(
        error_type="NO_DATA",
        error_message="no data",
        provider_name="P",
        engine_name="E",
        engine_version="1",
        data_source="d",
        reason="data_missing",
    )
    assert payload["schema"] == RESPONSE_SCHEMA
    assert payload["result_status"] == "failed"
    assert payload["error_type"] == "NO_DATA"
    assert payload["error_type"] == payload["error_type"].upper()
    assert payload["error_type"] in ERROR_TYPES
    assert payload["limitations"]["reason"] == "data_missing"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_decimal_str_no_scientific_notation():
    assert decimal_str(Decimal("10000")) == "10000"
    assert decimal_str("0.00000001", places=8) == "0.00000001"
    assert decimal_str(float("nan")) == "0"
    assert decimal_str(float("inf")) == "0"
    assert decimal_str("not-a-number") == "0"


def test_parse_decimal_rejects_non_finite_and_garbage():
    assert parse_decimal("123.5") == Decimal("123.5")
    assert parse_decimal(None, default=Decimal("1")) == Decimal("1")
    assert parse_decimal("") is None
    with pytest.raises(ValueError):
        parse_decimal("nan")
    with pytest.raises(ValueError):
        parse_decimal("abc")


def test_json_safe_nulls_non_finite():
    out = json_safe({"a": float("nan"), "b": [float("inf"), 1.5], "c": "x"})
    assert out["a"] is None
    assert out["b"][0] is None
    assert out["b"][1] == 1.5
    assert out["c"] == "x"


def test_request_ignores_unknown_fields():
    req = BacktestRequest.model_validate(
        {
            "schema": "cutie.external_backtest.request.v1",
            "backtest": {"symbol": "BTCUSDT", "timeframe": "1h", "totally_new": 1},
            "provider": {},
            "future_top_level_field": True,
        }
    )
    assert req.backtest.symbol == "BTCUSDT"
