"""Core validator: runs IMPL W3.9 §8 checks 1-10 against a provider.

Validates the **provider HTTP response layer**, so ``error_type`` is checked as
the UPPERCASE §6.3 protocol code (NO_DATA, PROVIDER_CONTRACT_VIOLATION, ...).
Lowercase snake_case normalization is the connector->server concern and is out
of scope here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .report_url import classify_report_url
from .schema_subset import (
    is_decimal_string,
    is_finite_number,
    validate_param_schema,
)
from .secrets import scan_for_secrets
from .transport import Response, ValidatorTransport

PROVIDER_CATALOG_SCHEMA = "cutie.backtest_provider_catalog.v1"
REQUEST_SCHEMA = "cutie.external_backtest.request.v1"
RESPONSE_SCHEMA = "cutie.external_backtest.response.v1"
BACKTEST_TASK_SCHEMA = "cutie.backtest_task.v1"

# §5.1: P0 main-line wrapper types. manual_export_parser / cloud_api_proxy are
# explicitly NOT part of the W3.9 main-line catalog enum (§8 check 9).
ALLOWED_WRAPPER_TYPES = frozenset({"python_inprocess", "local_cli", "local_http"})

# §6.3 standard error family (UPPERCASE protocol codes).
STANDARD_ERROR_CODES = frozenset(
    {
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
    }
)

# §6.2: money / quantity fields that MUST be decimal strings (not JSON floats).
MONEY_ASSUMPTION_FIELDS = ("fee_bps", "slippage_bps")
# §6.2: ratio/percentage metrics MAY be JSON numbers but not NaN/Infinity.
RATIO_METRIC_FIELDS = ("total_return_pct", "win_rate_pct", "max_drawdown_pct")

MAX_TOOLS = 10
MAX_PROVIDER_ID_LEN = 128
MAX_TOOL_ID_LEN = 128


@dataclass
class CheckResult:
    check_id: int
    name: str
    passed: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[Dict[str, str]] = field(default_factory=list)
    info: List[str] = field(default_factory=list)


@dataclass
class ValidationReport:
    ok: bool
    base_url: str
    provider_schema: Optional[str]
    tools_checked: int
    checks: List[CheckResult]
    warnings: List[Dict[str, str]]
    errors: List[str]

    def to_machine_json(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "base_url": self.base_url,
            "provider_schema": self.provider_schema,
            "tools_checked": self.tools_checked,
            "checks": [
                {
                    "check": c.check_id,
                    "name": c.name,
                    "passed": c.passed,
                    "errors": c.errors,
                    "warnings": c.warnings,
                    "info": c.info,
                }
                for c in self.checks
            ],
            "warnings": self.warnings,
            "errors": self.errors,
        }


@dataclass
class SmokeParams:
    tool_id: Optional[str]
    symbol: str
    timeframe: str
    market: str
    start_at: int
    end_at: int
    provider_params: Dict[str, Any]
    instruction: str


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


class ProviderValidator:
    def __init__(
        self,
        transport: ValidatorTransport,
        base_url: str,
        token: Optional[str],
        smoke: SmokeParams,
    ) -> None:
        self._t = transport
        self._base_url = base_url
        self._token = token
        self._smoke = smoke
        self._checks: List[CheckResult] = []
        self._catalog: Optional[Dict[str, Any]] = None
        self._selected_tool: Optional[Dict[str, Any]] = None

    # -- public entry -----------------------------------------------------

    def run(self) -> ValidationReport:
        self._check_1_health()
        self._check_2_catalog_auth()
        self._check_3_and_4_catalog_tools_and_secrets()
        self._check_9_wrapper_types()
        smoke_resp = self._check_5_smoke_request()
        self._check_6_7_8_response(smoke_resp)
        return self._build_report()

    # -- check 1: /health -------------------------------------------------

    def _check_1_health(self) -> None:
        result = CheckResult(1, "GET /health returns JSON object, no secret/path leak", True)
        try:
            resp = self._t.get("/health")
        except Exception as exc:  # noqa: BLE001
            result.passed = False
            result.errors.append(f"GET /health request failed: {exc}")
            self._checks.append(result)
            return
        if resp.json_error is not None:
            result.passed = False
            result.errors.append(f"/health body is not valid JSON: {resp.json_error}")
            self._checks.append(result)
            return
        if not isinstance(resp.json_body, dict):
            result.passed = False
            result.errors.append("/health must return a JSON object")
            self._checks.append(result)
            return
        leaks = scan_for_secrets(resp.json_body, "$.health")
        if leaks:
            result.passed = False
            for path, reason, detail in leaks:
                result.errors.append(f"/health leaks {reason} at {path}: {detail}")
        else:
            result.info.append("/health returned a clean JSON object")
        self._checks.append(result)

    # -- check 2: /catalog auth + schema ---------------------------------

    def _check_2_catalog_auth(self) -> None:
        result = CheckResult(
            2,
            "GET /catalog: no token -> 401/403; with token -> provider catalog schema",
            True,
        )

        # 2a: no-token must be rejected (only meaningful when a token is configured).
        if self._token:
            try:
                no_token_resp = self._t.get("/catalog", token=None)
                if no_token_resp.status_code not in (401, 403):
                    result.passed = False
                    result.errors.append(
                        f"/catalog without token returned {no_token_resp.status_code}, "
                        "expected 401/403"
                    )
                else:
                    result.info.append(
                        f"/catalog without token correctly returned {no_token_resp.status_code}"
                    )
            except Exception as exc:  # noqa: BLE001
                result.passed = False
                result.errors.append(f"/catalog (no token) request failed: {exc}")
        else:
            result.warnings.append(
                {
                    "code": "NO_TOKEN_CONFIGURED",
                    "message": "No --token provided; cannot verify that /catalog rejects "
                    "unauthenticated requests. Re-run with --token in production.",
                }
            )

        # 2b: with token must return the v1 catalog schema.
        try:
            resp = self._t.get("/catalog", token=self._token)
        except Exception as exc:  # noqa: BLE001
            result.passed = False
            result.errors.append(f"GET /catalog request failed: {exc}")
            self._checks.append(result)
            return
        if resp.status_code != 200:
            result.passed = False
            result.errors.append(
                f"GET /catalog with token returned {resp.status_code}, expected 200"
            )
            self._checks.append(result)
            return
        if resp.json_error is not None or not isinstance(resp.json_body, dict):
            result.passed = False
            result.errors.append("/catalog body must be a JSON object")
            self._checks.append(result)
            return
        schema = resp.json_body.get("schema")
        if schema != PROVIDER_CATALOG_SCHEMA:
            result.passed = False
            result.errors.append(
                f"/catalog schema is {schema!r}, expected {PROVIDER_CATALOG_SCHEMA!r}"
            )
        else:
            result.info.append(f"/catalog schema = {schema}")
            self._catalog = resp.json_body
        self._checks.append(result)

    # -- check 3 + 4: catalog tool fields + secret scrub -----------------

    def _check_3_and_4_catalog_tools_and_secrets(self) -> None:
        result3 = CheckResult(
            3,
            "Catalog tool field/length/array/param_schema/security validation",
            True,
        )
        result4 = CheckResult(
            4, "Catalog contains no secret-like keys / values / paths", True
        )

        if self._catalog is None:
            result3.passed = False
            result3.errors.append("no valid catalog available (see check 2)")
            self._checks.extend([result3, result4])
            return

        # provider block
        provider = _as_dict(self._catalog.get("provider"))
        provider_id = provider.get("provider_id")
        if not isinstance(provider_id, str) or not provider_id:
            result3.passed = False
            result3.errors.append("provider.provider_id missing or not a string")
        elif len(provider_id) > MAX_PROVIDER_ID_LEN:
            result3.passed = False
            result3.errors.append(
                f"provider.provider_id exceeds {MAX_PROVIDER_ID_LEN} chars"
            )

        tools = self._catalog.get("tools")
        if not isinstance(tools, list):
            result3.passed = False
            result3.errors.append("catalog.tools must be an array")
            self._checks.extend([result3, result4])
            return
        if len(tools) > MAX_TOOLS:
            result3.passed = False
            result3.errors.append(
                f"catalog.tools has {len(tools)} tools, max {MAX_TOOLS}"
            )
        if len(tools) == 0:
            result3.warnings.append(
                {
                    "code": "EMPTY_CATALOG",
                    "message": "catalog.tools is empty; UI will show this provider as unavailable",
                }
            )

        default_count = 0
        seen_tool_ids: set = set()
        for idx, tool in enumerate(tools):
            self._validate_tool_fields(tool, idx, result3, seen_tool_ids)
            if isinstance(tool, dict) and tool.get("is_default") is True:
                default_count += 1
        if default_count > 1:
            result3.passed = False
            result3.errors.append(
                f"{default_count} tools have is_default=true; at most one allowed per runtime"
            )

        # §4 secret scan over the whole catalog payload.
        leaks = scan_for_secrets(self._catalog, "$.catalog")
        if leaks:
            result4.passed = False
            for path, reason, detail in leaks:
                result4.errors.append(f"catalog leaks {reason} at {path}: {detail}")
        else:
            result4.info.append("catalog has no secret-like keys/values/paths")

        # Select the smoke target tool now that the catalog is validated.
        self._select_smoke_tool(tools)
        self._checks.extend([result3, result4])

    def _validate_tool_fields(
        self,
        tool: Any,
        idx: int,
        result: CheckResult,
        seen_tool_ids: set,
    ) -> None:
        prefix = f"tools[{idx}]"
        if not isinstance(tool, dict):
            result.passed = False
            result.errors.append(f"{prefix} must be a JSON object")
            return

        tool_id = tool.get("tool_id")
        if not isinstance(tool_id, str) or not tool_id:
            result.passed = False
            result.errors.append(f"{prefix}.tool_id missing or not a string")
        else:
            if len(tool_id) > MAX_TOOL_ID_LEN:
                result.passed = False
                result.errors.append(f"{prefix}.tool_id exceeds {MAX_TOOL_ID_LEN} chars")
            if tool_id in seen_tool_ids:
                result.passed = False
                result.errors.append(f"{prefix}.tool_id {tool_id!r} duplicated in catalog")
            seen_tool_ids.add(tool_id)
            prefix = f"tool '{tool_id}'"

        # kind: P0 only allows external_http (smoke is connector-internal only).
        kind = tool.get("kind")
        if kind != "external_http":
            result.passed = False
            result.errors.append(
                f"{prefix}.kind is {kind!r}, P0 catalog only allows 'external_http'"
            )

        # name required.
        if not isinstance(tool.get("name"), str) or not tool.get("name"):
            result.passed = False
            result.errors.append(f"{prefix}.name missing or not a string")

        # markets / timeframes: required non-empty arrays.
        for arr_field in ("markets", "timeframes"):
            value = tool.get(arr_field)
            if not isinstance(value, list) or len(value) == 0:
                result.passed = False
                result.errors.append(f"{prefix}.{arr_field} must be a non-empty array")
            elif not all(isinstance(v, str) for v in value):
                result.passed = False
                result.errors.append(f"{prefix}.{arr_field} must be an array of strings")

        # supported_symbols: optional but, if present, an array of strings.
        symbols = tool.get("supported_symbols")
        if symbols is not None and (
            not isinstance(symbols, list) or not all(isinstance(s, str) for s in symbols)
        ):
            result.passed = False
            result.errors.append(f"{prefix}.supported_symbols must be an array of strings")

        # execution.mode: P0 only sync.
        execution = _as_dict(tool.get("execution"))
        mode = execution.get("mode")
        if mode is not None and mode != "sync":
            result.passed = False
            result.errors.append(
                f"{prefix}.execution.mode is {mode!r}, P0 only supports 'sync'"
            )

        # report_capabilities.scope: P0 none | local_machine_only.
        report_caps = _as_dict(tool.get("report_capabilities"))
        scope = report_caps.get("scope")
        if scope is not None and scope not in ("none", "local_machine_only"):
            result.passed = False
            result.errors.append(
                f"{prefix}.report_capabilities.scope is {scope!r}, "
                "P0 only allows 'none' or 'local_machine_only'"
            )

        # security.live_trading must be falsey (true rejected).
        security = _as_dict(tool.get("security"))
        if security.get("live_trading") is True:
            result.passed = False
            result.errors.append(
                f"{prefix}.security.live_trading=true is not allowed for backtest providers"
            )
        if not isinstance(tool.get("security"), dict):
            result.passed = False
            result.errors.append(f"{prefix}.security must be present as an object")

        # param_schema subset (if present).
        if tool.get("param_schema") is not None:
            schema_errors = validate_param_schema(
                tool.get("param_schema"), f"{prefix}.param_schema"
            )
            if schema_errors:
                result.passed = False
                result.errors.extend(schema_errors)

    # -- check 9: wrapper_type -------------------------------------------

    def _check_9_wrapper_types(self) -> None:
        result = CheckResult(
            9,
            "Unknown wrapper_type fails; manual_export_parser/cloud_api_proxy off main line",
            True,
        )
        if self._catalog is None:
            result.passed = False
            result.errors.append("no valid catalog available (see check 2)")
            self._checks.append(result)
            return
        tools = self._catalog.get("tools") or []
        for idx, tool in enumerate(tools):
            if not isinstance(tool, dict):
                continue
            wrapper_type = tool.get("wrapper_type")
            label = tool.get("tool_id") or f"tools[{idx}]"
            if wrapper_type is None:
                result.passed = False
                result.errors.append(
                    f"tool '{label}' missing wrapper_type "
                    "(required for cutie.backtest_provider_catalog.v1)"
                )
            elif wrapper_type not in ALLOWED_WRAPPER_TYPES:
                result.passed = False
                result.errors.append(
                    f"tool '{label}' has unsupported wrapper_type {wrapper_type!r} "
                    "(allowed: python_inprocess, local_cli, local_http)"
                )
            else:
                result.info.append(f"tool '{label}' wrapper_type={wrapper_type}")
        self._checks.append(result)

    # -- check 5: smoke request ------------------------------------------

    def _select_smoke_tool(self, tools: List[Any]) -> None:
        dict_tools = [t for t in tools if isinstance(t, dict)]
        if self._smoke.tool_id:
            for tool in dict_tools:
                if tool.get("tool_id") == self._smoke.tool_id:
                    self._selected_tool = tool
                    return
            self._selected_tool = None
            return
        # default tool, else first.
        for tool in dict_tools:
            if tool.get("is_default") is True:
                self._selected_tool = tool
                return
        self._selected_tool = dict_tools[0] if dict_tools else None

    def _build_smoke_body(self) -> Dict[str, Any]:
        tool_id = self._smoke.tool_id
        if tool_id is None and self._selected_tool is not None:
            tool_id = self._selected_tool.get("tool_id")
        return {
            "schema": REQUEST_SCHEMA,
            "backtest": {
                "schema": BACKTEST_TASK_SCHEMA,
                "scene": "backtest_run",
                "task_type": "strategy.backtest.run",
                "run_id": "318265150907879424",
                "draft_id": "318239101302087680",
                "provider_tool_id": tool_id or "",
                "provider_params": self._smoke.provider_params,
                "instruction": self._smoke.instruction,
                "expected_outputs": ["metrics", "equity_curve", "trades", "report_url"],
                "strategy": {},
                "symbol": self._smoke.symbol,
                "market": self._smoke.market,
                "timeframe": self._smoke.timeframe,
                "start_at": self._smoke.start_at,
                "end_at": self._smoke.end_at,
                "initial_capital": "10000",
                "fee_bps": "10",
                "slippage_bps": "5",
                "constraints": {
                    "executor_type": "external_openclaw",
                    "verification_status": "external_unverified",
                    "cutie_verifies_result": False,
                    "must_report_assumptions_and_limitations": True,
                },
            },
            "provider": {
                "provider_name": "validator-smoke",
                "engine_name": "validator",
                "engine_version": "1.0.0",
                "data_source": "provider_reported",
            },
        }

    def _check_5_smoke_request(self) -> Optional[Response]:
        result = CheckResult(
            5,
            "Smoke request: provider returns success or standard business failed",
            True,
        )
        body = self._build_smoke_body()
        # The request body the validator sends is, by construction, a valid
        # cutie.external_backtest.request.v1 envelope.
        result.info.append(
            "sent cutie.external_backtest.request.v1 smoke envelope to /cutie/backtest"
        )
        try:
            resp = self._t.post_json("/cutie/backtest", body, token=self._token)
        except Exception as exc:  # noqa: BLE001
            result.passed = False
            result.errors.append(f"POST /cutie/backtest request failed: {exc}")
            self._checks.append(result)
            return None

        if resp.json_error is not None or not isinstance(resp.json_body, dict):
            result.passed = False
            result.errors.append(
                f"/cutie/backtest body must be a JSON object "
                f"(status={resp.status_code}, error={resp.json_error})"
            )
            self._checks.append(result)
            return resp

        status = resp.json_body.get("result_status")
        if status == "success":
            result.info.append("provider returned result_status=success")
        elif status == "failed":
            error_type = resp.json_body.get("error_type")
            if error_type in STANDARD_ERROR_CODES:
                result.info.append(
                    f"provider returned standard business failed (error_type={error_type})"
                )
            else:
                result.passed = False
                result.errors.append(
                    f"business failed error_type {error_type!r} is not a standard §6.3 code"
                )
        else:
            result.passed = False
            result.errors.append(
                f"result_status is {status!r}, expected 'success' or 'failed'"
            )
        self._checks.append(result)
        return resp

    # -- checks 6/7/8: response validation -------------------------------

    def _check_6_7_8_response(self, resp: Optional[Response]) -> None:
        result6 = CheckResult(
            6, "Success response: required fields, decimal money, finite non-money numbers", True
        )
        result7 = CheckResult(
            7, "Failed response: error_type/error_message + provider metadata", True
        )
        result8 = CheckResult(8, "report_url is relative or loopback/RFC1918 (no public host)", True)

        if resp is None or resp.json_error is not None or not isinstance(resp.json_body, dict):
            for r in (result6, result7, result8):
                r.passed = False
                r.errors.append("no parseable smoke response (see check 5)")
            self._checks.extend([result6, result7, result8])
            return

        body = resp.json_body
        status = body.get("result_status")

        if status == "success":
            self._validate_success(body, result6)
            result7.info.append("smoke produced success; failed-shape check skipped")
            self._validate_report_url(body, result8)
        elif status == "failed":
            self._validate_failed(body, result7)
            result6.info.append("smoke produced standard business failure; success-shape check skipped")
            # failed responses may still carry report_url; validate if present.
            self._validate_report_url(body, result8)
        else:
            for r in (result6, result7):
                r.info.append("result_status not classified (see check 5)")
            self._validate_report_url(body, result8)

        # Schema field must always be the response schema.
        if body.get("schema") != RESPONSE_SCHEMA:
            target = result6 if status == "success" else result7
            target.passed = False
            target.errors.append(
                f"response schema is {body.get('schema')!r}, expected {RESPONSE_SCHEMA!r}"
            )

        self._checks.extend([result6, result7, result8])

    def _validate_success(self, body: Dict[str, Any], result: CheckResult) -> None:
        required_objects = ("metrics", "assumptions", "limitations", "raw_report")
        required_arrays = ("equity_curve", "trades")
        required_strings = (
            "provider_name",
            "engine_name",
            "engine_version",
            "data_source",
        )
        for f in required_strings:
            if not isinstance(body.get(f), str) or not body.get(f):
                result.passed = False
                result.errors.append(f"success response missing required string field '{f}'")
        for f in required_objects:
            if not isinstance(body.get(f), dict):
                result.passed = False
                result.errors.append(f"success response field '{f}' must be an object")
        for f in required_arrays:
            if not isinstance(body.get(f), list):
                result.passed = False
                result.errors.append(f"success response field '{f}' must be an array")

        # §6.2 ratio metrics: a JSON number that is not NaN/Infinity, OR null
        # ("not applicable", e.g. win_rate with zero closed trades). A string
        # is rejected — ratio fields are numbers, not decimal strings.
        metrics = _as_dict(body.get("metrics"))
        for f in RATIO_METRIC_FIELDS:
            if f in metrics:
                val = metrics[f]
                if val is None:
                    continue  # null = not applicable, allowed for ratio metrics
                if isinstance(val, str):
                    result.passed = False
                    result.errors.append(
                        f"metric '{f}' should be a JSON number or null, got string {val!r}"
                    )
                elif not is_finite_number(val):
                    result.passed = False
                    result.errors.append(
                        f"metric '{f}' is not a finite number: {val!r}"
                    )

        # §6.2 money fields must be decimal strings, never JSON floats.
        assumptions = _as_dict(body.get("assumptions"))
        for f in MONEY_ASSUMPTION_FIELDS:
            if f in assumptions and not is_decimal_string(assumptions[f]):
                result.passed = False
                result.errors.append(
                    f"assumptions.{f} must be a decimal string, got {assumptions[f]!r}"
                )
        if "initial_capital" in body and not is_decimal_string(body["initial_capital"]):
            result.passed = False
            result.errors.append(
                f"initial_capital must be a decimal string, got {body['initial_capital']!r}"
            )

        # equity_curve points: equity is money -> decimal string.
        for i, point in enumerate(body.get("equity_curve") or []):
            if not isinstance(point, dict):
                result.passed = False
                result.errors.append(f"equity_curve[{i}] must be an object")
                continue
            if "equity" in point and not is_decimal_string(point["equity"]):
                result.passed = False
                result.errors.append(
                    f"equity_curve[{i}].equity must be a decimal string, got {point['equity']!r}"
                )

        # trades: pnl is money -> decimal string.
        for i, trade in enumerate(body.get("trades") or []):
            if not isinstance(trade, dict):
                result.passed = False
                result.errors.append(f"trades[{i}] must be an object")
                continue
            if "pnl" in trade and not is_decimal_string(trade["pnl"]):
                result.passed = False
                result.errors.append(
                    f"trades[{i}].pnl must be a decimal string, got {trade['pnl']!r}"
                )

        # raw_report must not leak secrets/paths.
        raw_report = body.get("raw_report")
        if isinstance(raw_report, dict):
            leaks = scan_for_secrets(raw_report, "$.raw_report")
            if leaks:
                result.passed = False
                for path, reason, detail in leaks:
                    result.errors.append(f"raw_report leaks {reason} at {path}: {detail}")

        if result.passed:
            result.info.append("success response shape and money-field precision valid")

    def _validate_failed(self, body: Dict[str, Any], result: CheckResult) -> None:
        error_type = body.get("error_type")
        if not isinstance(error_type, str) or not error_type:
            result.passed = False
            result.errors.append("failed response missing error_type")
        elif error_type not in STANDARD_ERROR_CODES:
            result.passed = False
            result.errors.append(
                f"failed response error_type {error_type!r} is not a standard §6.3 code"
            )
        error_message = body.get("error_message")
        if not isinstance(error_message, str) or not error_message:
            result.passed = False
            result.errors.append("failed response missing error_message")
        # provider metadata (provider_name required; engine info recommended).
        if not isinstance(body.get("provider_name"), str) or not body.get("provider_name"):
            result.passed = False
            result.errors.append("failed response missing provider_name")
        for f in ("engine_name", "engine_version", "data_source"):
            if not body.get(f):
                result.warnings.append(
                    {
                        "code": "FAILED_MISSING_METADATA",
                        "message": f"failed response omits provider metadata field '{f}'",
                    }
                )
        if result.passed:
            result.info.append(
                f"failed response has standard error_type={error_type} + metadata"
            )

    def _validate_report_url(self, body: Dict[str, Any], result: CheckResult) -> None:
        report_url = body.get("report_url")
        status, detail = classify_report_url(report_url)
        if status == "ok_relative":
            result.info.append(detail)
        elif status == "ok_local_url":
            result.warnings.append(
                {
                    "code": "LOCAL_REPORT_URL",
                    "message": detail
                    + "; connector must scrub to a relative path/ref before /external-result",
                }
            )
        elif status == "blocked_public":
            result.passed = False
            result.errors.append(detail)
        elif status == "blocked_path":
            result.passed = False
            result.errors.append(detail)

    # -- report -----------------------------------------------------------

    def _build_report(self) -> ValidationReport:
        all_errors: List[str] = []
        all_warnings: List[Dict[str, str]] = []
        for c in self._checks:
            all_errors.extend(c.errors)
            all_warnings.extend(c.warnings)
        ok = all(c.passed for c in self._checks)
        provider_schema = self._catalog.get("schema") if self._catalog else None
        tools_checked = len(self._catalog.get("tools") or []) if self._catalog else 0
        return ValidationReport(
            ok=ok,
            base_url=self._base_url,
            provider_schema=provider_schema,
            tools_checked=tools_checked,
            checks=self._checks,
            warnings=all_warnings,
            errors=all_errors,
        )
