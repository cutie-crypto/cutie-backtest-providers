"""Cutie BYO Backtest Provider contract (Pydantic models + helpers).

This module fixes the v1 wire contract so a wrapper author never has to guess
field names or precision rules. It covers:

- Catalog response  : ``cutie.backtest_provider_catalog.v1`` (IMPL §5.1)
- Backtest request  : ``cutie.external_backtest.request.v1``  (IMPL §6.1)
- Success response  : ``cutie.external_backtest.response.v1`` (IMPL §6.2)
- Failure response  : ``cutie.external_backtest.response.v1`` (IMPL §6.3)

Precision contract (IMPL §6.2):
- Money / quantity fields (capital, equity, price, qty, cost, fee, pnl, bps)
  are **decimal strings**. Use :func:`decimal_str`.
- Ratio / percentage metrics (``*_pct``) are JSON numbers, never NaN/Infinity.

Protocol codes (IMPL §6.3) are **UPPERCASE** at the provider HTTP layer. The
connector lowercases them before persisting; the provider/validator use upper.
"""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Schema identifiers (do not change — these are the v1 wire contract)
# ---------------------------------------------------------------------------

CATALOG_SCHEMA = "cutie.backtest_provider_catalog.v1"
REQUEST_SCHEMA = "cutie.external_backtest.request.v1"
RESPONSE_SCHEMA = "cutie.external_backtest.response.v1"

# Standard error family (IMPL §6.3). Provider HTTP layer uses UPPERCASE.
ERROR_TYPES = (
    "INVALID_REQUEST",
    "AUTH_FAILED",
    "TOOL_NOT_FOUND",
    "INVALID_PARAMS",
    "SYMBOL_UNSUPPORTED",
    "MARKET_UNSUPPORTED",
    "TIMEFRAME_UNSUPPORTED",
    "NO_DATA",
    "INSUFFICIENT_DATA",
    "RATE_LIMITED",
    "ENGINE_ERROR",
    "REPORT_UNAVAILABLE",
    "PROVIDER_CONTRACT_VIOLATION",
)

# Allowed enum values for catalog fields (IMPL §5.1 field rules).
ALLOWED_KINDS = ("external_http",)
ALLOWED_WRAPPER_TYPES = ("python_inprocess", "local_cli", "local_http")
ALLOWED_EXECUTION_MODES = ("sync",)
ALLOWED_REPORT_SCOPES = ("none", "local_machine_only")


# ---------------------------------------------------------------------------
# Catalog models (IMPL §5.1)
# ---------------------------------------------------------------------------


class DataSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = "provider_reported"
    name: str
    description: str
    coverage_hint: Optional[str] = None
    external_unverified: bool = True


class Execution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # P0 only allows "sync" (IMPL §5.1).
    mode: str = "sync"
    timeout_ms: int = 120000
    max_range_days: int = 365
    max_parallel_runs: int = 1
    async_supported: bool = False


class Adapter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requires_manual_export: bool = False
    working_dir_policy: str = "ephemeral_or_provider_managed"
    result_file_patterns: List[str] = Field(default_factory=list)
    upstream_auth_local_only: bool = True


class OutputSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metrics: List[str] = Field(default_factory=list)
    artifacts: List[str] = Field(default_factory=list)
    series: List[str] = Field(default_factory=list)
    tables: List[str] = Field(default_factory=list)


class ReportCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_url: bool = False
    # P0: "none" or "local_machine_only" only.
    scope: str = "local_machine_only"
    formats: List[str] = Field(default_factory=list)
    retention_hint: Optional[str] = None


class Security(BaseModel):
    model_config = ConfigDict(extra="forbid")

    network_scope: str = "openclaw_hermes_local_or_private"
    requires_user_secret: bool = False
    secrets_stay_local: bool = True
    # P0 must be False; live-trading providers must not register (IMPL §5.1).
    live_trading: bool = False
    filesystem_paths_exposed: bool = False


class CatalogTool(BaseModel):
    """A single tool entry in the provider catalog (IMPL §5.1).

    ``param_schema`` is a JSON Schema subset (object/string/number/integer/
    boolean/enum/default/min/max/required) and is left as a free-form dict so
    adapters can describe their parameters; the validator checks the subset.
    """

    model_config = ConfigDict(extra="forbid")

    tool_id: str = Field(..., max_length=128, min_length=1)
    kind: str = "external_http"
    name: str
    description: str
    wrapper_type: str
    provider_name: str
    engine_name: str
    engine_version: str
    data_source: DataSource
    supported_symbols: Optional[List[str]] = None
    markets: List[str]
    timeframes: List[str]
    is_default: bool = False  # IMPL: use is_default, NOT "default"
    execution: Execution = Field(default_factory=Execution)
    adapter: Adapter = Field(default_factory=Adapter)
    param_schema: Dict[str, Any] = Field(default_factory=dict)
    output_schema: OutputSchema = Field(default_factory=OutputSchema)
    report_capabilities: ReportCapabilities = Field(default_factory=ReportCapabilities)
    failure_codes: List[str] = Field(default_factory=list)
    security: Security = Field(default_factory=Security)


class ProviderInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str = Field(..., max_length=128, min_length=1)
    provider_name: str
    provider_version: str
    homepage_url: Optional[str] = None
    maintainer: Optional[str] = None


class CatalogResponse(BaseModel):
    """``GET /catalog`` response (IMPL §5.1).

    Note: ``health`` must NOT appear here — the connector derives health by
    probing ``/health`` and smoke/catalog checks (IMPL §5.1).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: str = Field(default=CATALOG_SCHEMA, alias="schema")
    provider: ProviderInfo
    # Max 10 tools (IMPL §5.1); empty array is legal (UI shows unavailable).
    tools: List[CatalogTool] = Field(default_factory=list, max_length=10)


# ---------------------------------------------------------------------------
# Request models (IMPL §6.1)
# ---------------------------------------------------------------------------


class BacktestConstraints(BaseModel):
    # Connector/server-fixed; provider must not override (IMPL §4.2).
    model_config = ConfigDict(extra="allow")

    executor_type: Optional[str] = None
    verification_status: Optional[str] = None
    cutie_verifies_result: Optional[bool] = None
    must_report_assumptions_and_limitations: Optional[bool] = None


class BacktestTask(BaseModel):
    # Provider must ignore unknown fields for forward-compat (IMPL §6.1).
    model_config = ConfigDict(extra="allow")

    schema_: Optional[str] = Field(default=None, alias="schema")
    scene: Optional[str] = None
    task_type: Optional[str] = None
    run_id: Optional[str] = None
    draft_id: Optional[str] = None
    provider_tool_id: Optional[str] = None
    provider_params: Dict[str, Any] = Field(default_factory=dict)
    instruction: Optional[str] = ""
    expected_outputs: List[str] = Field(default_factory=list)
    strategy: Dict[str, Any] = Field(default_factory=dict)
    symbol: Optional[str] = None
    market: Optional[str] = "spot"
    timeframe: Optional[str] = None
    start_at: Optional[int] = None
    end_at: Optional[int] = None
    # Money / bps fields arrive as decimal strings (IMPL §6.1).
    initial_capital: Optional[str] = None
    fee_bps: Optional[str] = None
    slippage_bps: Optional[str] = None
    constraints: BacktestConstraints = Field(default_factory=BacktestConstraints)


class ProviderEcho(BaseModel):
    model_config = ConfigDict(extra="allow")

    provider_name: Optional[str] = None
    engine_name: Optional[str] = None
    engine_version: Optional[str] = None
    data_source: Optional[str] = None


class BacktestRequest(BaseModel):
    # Provider must ignore unknown top-level fields (IMPL §6.1).
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    schema_: Optional[str] = Field(default=None, alias="schema")
    backtest: BacktestTask = Field(default_factory=BacktestTask)
    provider: ProviderEcho = Field(default_factory=ProviderEcho)


# ---------------------------------------------------------------------------
# Response models (IMPL §6.2 / §6.3)
# ---------------------------------------------------------------------------


class EquityPoint(BaseModel):
    model_config = ConfigDict(extra="allow")

    t: int
    equity: str  # money -> decimal string


class Trade(BaseModel):
    model_config = ConfigDict(extra="allow")

    side: Optional[str] = None
    entry_at: Optional[int] = None
    exit_at: Optional[int] = None
    pnl: Optional[str] = None  # money -> decimal string


class BacktestResult(BaseModel):
    """Normalized result an adapter returns to be serialized into a response.

    The adapter builds this; ``app.py`` serializes it into the v1 response
    envelope. Money fields are decimal strings; ``*_pct`` metrics are numbers.
    """

    model_config = ConfigDict(extra="forbid")

    provider_run_id: Optional[str] = None
    result_hash: Optional[str] = None
    report_url: Optional[str] = None  # relative path/ref only (IMPL §7)
    report_url_scope: str = "local_machine_only"
    # Ratio/percentage metrics -> JSON numbers (IMPL §6.2).
    metrics: Dict[str, Union[int, float]] = Field(default_factory=dict)
    initial_capital: Optional[str] = None  # money -> decimal string
    equity_curve: List[Dict[str, Any]] = Field(default_factory=list)
    trades: List[Dict[str, Any]] = Field(default_factory=list)
    assumptions: Dict[str, Any] = Field(default_factory=dict)
    limitations: Dict[str, Any] = Field(default_factory=dict)
    raw_report: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers: Decimal parse, JSON safe, business_failure
# ---------------------------------------------------------------------------


def decimal_str(value: Any, places: int = 8) -> str:
    """Render a money / quantity value as a decimal string (IMPL §6.2).

    Money/quantity fields (capital, equity, price, qty, cost, fee, pnl, bps)
    must be serialized as decimal strings, never JSON floats. Non-finite or
    unparseable values fall back to ``"0"``. Avoids scientific notation.
    """
    if isinstance(value, Decimal):
        dec = value
    else:
        if isinstance(value, float) and not math.isfinite(value):
            return "0"
        try:
            dec = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return "0"
    if not dec.is_finite():
        return "0"
    quantized = dec.quantize(Decimal(1).scaleb(-places))
    normalized = quantized.normalize()
    return f"{normalized:f}"


def parse_decimal(value: Any, *, default: Optional[Decimal] = None) -> Optional[Decimal]:
    """Parse a request decimal-string field into a :class:`Decimal`.

    Returns ``default`` when the value is missing; raises ``ValueError`` when a
    non-empty value cannot be parsed as a finite decimal (so the caller can map
    it to ``INVALID_PARAMS``).
    """
    if value is None or value == "":
        return default
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"not a valid decimal: {value!r}") from exc
    if not dec.is_finite():
        raise ValueError(f"decimal must be finite: {value!r}")
    return dec


def json_safe(value: Any) -> Any:
    """Convert non-finite floats (NaN/Infinity) into JSON-safe ``None``.

    Ratio/percentage metrics must never be NaN/Infinity (IMPL §6.2).
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    return value


def success_response(
    *,
    provider_name: str,
    engine_name: str,
    engine_version: str,
    data_source: str,
    result: BacktestResult,
) -> Dict[str, Any]:
    """Build a ``result_status=success`` response envelope (IMPL §6.2)."""
    payload: Dict[str, Any] = {
        "schema": RESPONSE_SCHEMA,
        "result_status": "success",
        "provider_name": provider_name,
        "engine_name": engine_name,
        "engine_version": engine_version,
        "data_source": data_source,
        "metrics": result.metrics,
        "equity_curve": result.equity_curve,
        "trades": result.trades,
        "assumptions": result.assumptions,
        "limitations": result.limitations,
        "raw_report": result.raw_report,
    }
    if result.provider_run_id is not None:
        payload["provider_run_id"] = result.provider_run_id
    if result.result_hash is not None:
        payload["result_hash"] = result.result_hash
    if result.report_url is not None:
        payload["report_url"] = result.report_url
        payload["report_url_scope"] = result.report_url_scope
    if result.initial_capital is not None:
        payload["initial_capital"] = result.initial_capital
    return json_safe(payload)


def business_failure(
    *,
    error_type: str,
    error_message: str,
    provider_name: str,
    engine_name: Optional[str] = None,
    engine_version: Optional[str] = None,
    data_source: Optional[str] = None,
    provider_run_id: Optional[str] = None,
    reason: Optional[str] = None,
    assumptions: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a ``result_status=failed`` business-failure envelope (IMPL §6.3).

    ``error_type`` must be one of the UPPERCASE standard codes in
    :data:`ERROR_TYPES` (provider may add custom UPPERCASE codes too).
    """
    limitations: Dict[str, Any] = {}
    if reason:
        limitations["reason"] = reason
    payload: Dict[str, Any] = {
        "schema": RESPONSE_SCHEMA,
        "result_status": "failed",
        "provider_name": provider_name,
        "error_type": error_type,
        "error_message": error_message,
        "assumptions": assumptions or {},
        "limitations": limitations,
        "raw_report": {},
    }
    if provider_run_id is not None:
        payload["provider_run_id"] = provider_run_id
    if engine_name is not None:
        payload["engine_name"] = engine_name
    if engine_version is not None:
        payload["engine_version"] = engine_version
    if data_source is not None:
        payload["data_source"] = data_source
    return payload
