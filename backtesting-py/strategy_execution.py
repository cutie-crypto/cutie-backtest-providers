"""Frozen execution-wire validation and companion evidence builders for 62-2a."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

from canonical_json import canonical_decimal_str, canonical_json_sha256
from strategy_kernel import (
    ARTIFACT_DIGEST_SCHEMA,
    COMPILER_TOOL_ID,
    ERR_BINDING_MISMATCH,
    ERR_CAPABILITY_MISMATCH,
    ERR_COVERAGE_INCOMPLETE,
    ERR_SPEC_INVALID,
    CompiledPlan,
    StrategyContractError,
    capability_hash,
    compile_strategy,
)

EXECUTION_REQUEST_SCHEMA = "cutie.strategy_execution_request.v1"
EXECUTION_MAX_RANGE_DAYS = 365
_REQUEST_KEYS = {
    "schema",
    "execution_mode",
    "run_id",
    "artifact",
    "strategy_spec",
    "artifact_manifest",
    "execution_params",
    "expected_capability_hash",
    "expected_provider_revision",
    "dispatch_nonce",
    "result_contract",
}
_ARTIFACT_KEYS = {
    "artifact_id",
    "artifact_version_id",
    "version_no",
    "spec_hash",
    "manifest_hash",
    "artifact_hash",
}
_EXECUTION_PARAM_KEYS = {
    "schema_version",
    "symbol",
    "market",
    "timeframe",
    "start_at",
    "end_at",
    "initial_capital",
    "fee_bps",
    "slippage_bps",
    "provider_tool_id",
    "provider_params",
}
_RESULT_CONTRACT_KEYS = {
    "result_schema",
    "coverage_schema",
    "trace_schema",
    "evidence_schema",
}
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{7,64}$")
_DECIMAL_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?$")
_MAX_BIGINT = 9223372036854775807


@dataclass(frozen=True)
class ValidatedExecution:
    request: dict[str, Any]
    plan: CompiledPlan


@dataclass(frozen=True)
class CoverageInput:
    requirement: dict[str, Any]
    checksum: str
    revision: str
    point_count: int
    actual_start_at: int
    actual_end_at: int
    available_through: int


def _error(
    code: str,
    path: str,
    message: str,
    *,
    required: Any = None,
    actual: Any = None,
) -> None:
    raise StrategyContractError(code, path, message, required=required, actual=actual)


def _exact(value: Any, keys: set[str], path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _error(
            ERR_SPEC_INVALID,
            path,
            "must be an object",
            required=sorted(keys),
            actual=type(value).__name__,
        )
    actual = set(value)
    if actual != keys:
        _error(
            ERR_SPEC_INVALID,
            path,
            "exact keys required",
            required=sorted(keys),
            actual={"missing": sorted(keys - actual), "unknown": sorted(actual - keys)},
        )
    return value


def _decimal_id(value: Any, path: str) -> str:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdigit()
        or value.startswith("0")
        or len(value) > 19
        or int(value) > _MAX_BIGINT
    ):
        _error(
            ERR_SPEC_INVALID,
            path,
            "must be a positive signed-BIGINT decimal string",
            actual=value,
        )
    return value


def _hash(value: Any, path: str) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        _error(ERR_SPEC_INVALID, path, "must be 64 lowercase hex", actual=value)
    return value


def _canonical_decimal(
    value: Any, path: str, *, positive: bool = False, nonnegative: bool = False
) -> str:
    if (
        not isinstance(value, str)
        or _DECIMAL_RE.fullmatch(value) is None
        or value == "-0"
    ):
        _error(
            ERR_SPEC_INVALID, path, "must be a canonical Decimal string", actual=value
        )
    if canonical_decimal_str(value) != value:
        _error(ERR_SPEC_INVALID, path, "must be canonical", actual=value)
    if positive and (value.startswith("-") or value == "0"):
        _error(ERR_SPEC_INVALID, path, "must be positive", actual=value)
    if nonnegative and value.startswith("-"):
        _error(ERR_SPEC_INVALID, path, "must be non-negative", actual=value)
    return value


def is_strategy_execution_intent(value: Any) -> bool:
    """Fail closed for malformed/partial artifact requests instead of legacy fallback."""
    if not isinstance(value, dict):
        return False
    schema = value.get("schema")
    if isinstance(schema, str) and schema.startswith(
        "cutie.strategy_execution_request."
    ):
        return True
    return any(
        key in value
        for key in (
            "artifact",
            "strategy_spec",
            "artifact_manifest",
            "expected_capability_hash",
            "expected_provider_revision",
            "result_contract",
        )
    )


def validate_execution_request(
    value: Any,
    capability: dict[str, Any],
    advertised_capability_hash: str,
) -> ValidatedExecution:
    request = _exact(value, _REQUEST_KEYS, "$")
    if request["schema"] != EXECUTION_REQUEST_SCHEMA:
        _error(
            ERR_SPEC_INVALID,
            "$.schema",
            "unsupported execution request schema",
            actual=request["schema"],
        )
    if request["execution_mode"] != "historical_replay":
        _error(
            ERR_SPEC_INVALID, "$.execution_mode", "API only permits historical_replay"
        )
    _decimal_id(request["run_id"], "$.run_id")
    artifact = _exact(request["artifact"], _ARTIFACT_KEYS, "$.artifact")
    _decimal_id(artifact["artifact_id"], "$.artifact.artifact_id")
    _decimal_id(artifact["artifact_version_id"], "$.artifact.artifact_version_id")
    if (
        isinstance(artifact["version_no"], bool)
        or not isinstance(artifact["version_no"], int)
        or artifact["version_no"] <= 0
        or artifact["version_no"] > 2**53 - 1
    ):
        _error(
            ERR_SPEC_INVALID, "$.artifact.version_no", "must be a positive safe integer"
        )
    for key in ("spec_hash", "manifest_hash", "artifact_hash"):
        _hash(artifact[key], f"$.artifact.{key}")
    if not isinstance(request["strategy_spec"], dict) or not isinstance(
        request["artifact_manifest"], dict
    ):
        _error(
            ERR_SPEC_INVALID, "$", "strategy_spec and artifact_manifest must be objects"
        )
    if canonical_json_sha256(request["strategy_spec"]) != artifact["spec_hash"]:
        _error(
            ERR_BINDING_MISMATCH, "$.artifact.spec_hash", "does not match strategy_spec"
        )
    if canonical_json_sha256(request["artifact_manifest"]) != artifact["manifest_hash"]:
        _error(
            ERR_BINDING_MISMATCH,
            "$.artifact.manifest_hash",
            "does not match artifact_manifest",
        )
    digest = canonical_json_sha256(
        {
            "schema": ARTIFACT_DIGEST_SCHEMA,
            "spec_hash": artifact["spec_hash"],
            "manifest_hash": artifact["manifest_hash"],
        }
    )
    if digest != artifact["artifact_hash"]:
        _error(
            ERR_BINDING_MISMATCH,
            "$.artifact.artifact_hash",
            "does not match digest payload",
        )

    params = _exact(
        request["execution_params"], _EXECUTION_PARAM_KEYS, "$.execution_params"
    )
    if params["schema_version"] != "cutie.execution_params.v1":
        _error(
            ERR_SPEC_INVALID, "$.execution_params.schema_version", "unsupported schema"
        )
    if params["provider_tool_id"] != COMPILER_TOOL_ID:
        _error(
            ERR_BINDING_MISMATCH,
            "$.execution_params.provider_tool_id",
            "must route to compiler tool",
        )
    for key in ("symbol", "market", "timeframe"):
        if not isinstance(params[key], str) or not params[key]:
            _error(
                ERR_SPEC_INVALID,
                f"$.execution_params.{key}",
                "must be a non-empty string",
            )
    for key in ("start_at", "end_at"):
        if (
            isinstance(params[key], bool)
            or not isinstance(params[key], int)
            or not 0 <= params[key] <= 2**53 - 1
        ):
            _error(
                ERR_SPEC_INVALID,
                f"$.execution_params.{key}",
                "must be a non-negative safe integer",
            )
    if params["end_at"] <= params["start_at"]:
        _error(
            ERR_SPEC_INVALID,
            "$.execution_params",
            "end_at must be greater than start_at",
        )
    range_seconds = params["end_at"] - params["start_at"]
    max_range_seconds = EXECUTION_MAX_RANGE_DAYS * 24 * 60 * 60
    if range_seconds > max_range_seconds:
        _error(
            ERR_SPEC_INVALID,
            "$.execution_params",
            "range exceeds the advertised Provider maximum",
            required={"max_range_days": EXECUTION_MAX_RANGE_DAYS},
            actual={"range_seconds": range_seconds},
        )
    _canonical_decimal(
        params["initial_capital"], "$.execution_params.initial_capital", positive=True
    )
    _canonical_decimal(
        params["fee_bps"], "$.execution_params.fee_bps", nonnegative=True
    )
    _canonical_decimal(
        params["slippage_bps"], "$.execution_params.slippage_bps", nonnegative=True
    )
    if not isinstance(params["provider_params"], dict):
        _error(
            ERR_SPEC_INVALID, "$.execution_params.provider_params", "must be an object"
        )
    market = request["strategy_spec"].get("market")
    if (
        not isinstance(market, dict)
        or params["symbol"] not in market.get("symbols", [])
        or params["market"] != market.get("market_type")
        or params["timeframe"] != market.get("timeframe")
    ):
        _error(
            ERR_BINDING_MISMATCH,
            "$.execution_params",
            "symbol/market/timeframe differs from StrategySpec",
        )

    _hash(request["expected_capability_hash"], "$.expected_capability_hash")
    if request["expected_capability_hash"] != advertised_capability_hash:
        _error(
            ERR_CAPABILITY_MISMATCH,
            "$.expected_capability_hash",
            "does not match local capability",
            required=advertised_capability_hash,
            actual=request["expected_capability_hash"],
        )
    revision = request["expected_provider_revision"]
    if not isinstance(revision, str) or _REVISION_RE.fullmatch(revision) is None:
        _error(
            ERR_SPEC_INVALID,
            "$.expected_provider_revision",
            "must be an immutable lowercase revision",
        )
    if revision != capability.get("provider_revision"):
        _error(
            ERR_CAPABILITY_MISMATCH,
            "$.expected_provider_revision",
            "does not match local capability",
            required=capability.get("provider_revision"),
            actual=revision,
        )
    if capability_hash(capability) != advertised_capability_hash:
        _error(
            ERR_CAPABILITY_MISMATCH,
            "$.capability",
            "local payload/hash pair is inconsistent",
        )
    if (
        not isinstance(request["dispatch_nonce"], str)
        or not 1 <= len(request["dispatch_nonce"]) <= 64
    ):
        _error(ERR_SPEC_INVALID, "$.dispatch_nonce", "must be a 1..64 character string")
    contract = _exact(
        request["result_contract"], _RESULT_CONTRACT_KEYS, "$.result_contract"
    )
    expected_contract = {
        "result_schema": "cutie.backtest_result.v2",
        "coverage_schema": "cutie.strategy_coverage_manifest.v1",
        "trace_schema": "cutie.strategy_execution_trace.v1",
        "evidence_schema": "cutie.strategy_execution_evidence.v1",
    }
    if contract != expected_contract:
        _error(
            ERR_SPEC_INVALID,
            "$.result_contract",
            "unsupported result contract",
            required=expected_contract,
            actual=contract,
        )
    plan = compile_strategy(
        request["strategy_spec"], request["artifact_manifest"], capability
    )
    if (plan.spec_hash, plan.manifest_hash, plan.artifact_hash) != (
        artifact["spec_hash"],
        artifact["manifest_hash"],
        artifact["artifact_hash"],
    ):
        _error(
            ERR_BINDING_MISMATCH,
            "$.artifact",
            "compiled hashes differ from execution binding",
        )
    for index, requirement in enumerate(plan.artifact_manifest["data_requirements"]):
        step_seconds = _interval_seconds(requirement["interval"])
        effective_start = max(
            0,
            params["start_at"] - requirement["warmup_bars"] * step_seconds,
        )
        data_window_seconds = params["end_at"] - effective_start
        if data_window_seconds > max_range_seconds:
            _error(
                ERR_SPEC_INVALID,
                f"$.artifact_manifest.data_requirements[{index}].warmup_bars",
                "warmup expands adapter data window beyond the Provider maximum",
                required={"max_range_days": EXECUTION_MAX_RANGE_DAYS},
                actual={
                    "effective_start": effective_start,
                    "end_at": params["end_at"],
                    "range_seconds": data_window_seconds,
                },
            )
    return ValidatedExecution(copy.deepcopy(request), plan)


def build_coverage_manifest(
    request: dict[str, Any],
    inputs: list[CoverageInput],
    data_manifest: dict[str, Any],
) -> dict[str, Any]:
    requirements = request["artifact_manifest"]["data_requirements"]
    by_stream = {item.requirement["stream_id"]: item for item in inputs}
    if set(by_stream) != {item["stream_id"] for item in requirements}:
        _error(
            ERR_COVERAGE_INCOMPLETE,
            "$.coverage",
            "stream evidence does not equal requirement exact-set",
        )
    symbol = request["execution_params"]["symbol"]
    streams: list[dict[str, Any]] = []
    for requirement in requirements:
        item = by_stream[requirement["stream_id"]]
        if item.point_count <= 0:
            _error(
                ERR_COVERAGE_INCOMPLETE,
                f"$.coverage.{requirement['stream_id']}",
                "stream is empty",
            )
        primary = requirement["execution_role"] == "primary_execution_kline"
        step = _interval_seconds(requirement["interval"])
        required_start = max(
            0,
            request["execution_params"]["start_at"] - requirement["warmup_bars"] * step,
        )
        required_end = request["execution_params"]["end_at"]
        if (
            (required_end - required_start) % step != 0
            or item.actual_start_at > required_start
            or item.available_through < required_end
            or item.point_count != (required_end - required_start) // step
        ):
            _error(
                ERR_COVERAGE_INCOMPLETE,
                f"$.coverage.{requirement['stream_id']}",
                "stream does not cover the complete aligned required range",
            )
        actual_start = data_manifest["start_at"] if primary else item.actual_start_at
        actual_end = data_manifest["end_at"] if primary else item.available_through
        point_count = data_manifest["kline_count"] if primary else item.point_count
        checksum = data_manifest["checksum"] if primary else item.checksum
        streams.append(
            {
                "stream_id": f"{requirement['stream_id']}.{symbol}",
                "kind": requirement["kind"],
                "execution_role": requirement["execution_role"],
                "provider": requirement["provider"],
                "storage_source": requirement["storage_source"],
                "result_source": requirement["result_source"],
                "exchange": requirement["exchange"],
                "market": requirement["market"],
                "symbol": symbol,
                "timeframe": requirement["interval"],
                "required_range": {
                    "start_at": request["execution_params"]["start_at"],
                    "end_at": request["execution_params"]["end_at"],
                    "warmup_start_at": max(
                        0,
                        request["execution_params"]["start_at"]
                        - requirement["warmup_bars"]
                        * _interval_seconds(requirement["interval"]),
                    ),
                },
                "actual_range": {
                    "start_at": actual_start,
                    "end_at": actual_end,
                    "available_through": item.available_through,
                },
                "point_count": point_count,
                "granularity": requirement["interval"],
                "freshness": {
                    "checked_at": request["execution_params"]["end_at"],
                    "age_seconds": 0,
                    "max_age_seconds": requirement["max_freshness_seconds"],
                },
                "gaps": [],
                "revision": {"schema": "source_revision.v1", "value": item.revision},
                "checksum": {"algo": "sha256", "value": checksum},
                "permitted_uses": {
                    "backtest": True,
                    "golden_replay": False,
                    "paper": False,
                },
                "status": "complete",
            }
        )
    streams.sort(key=lambda stream: stream["stream_id"])
    return {
        "schema": "cutie.strategy_coverage_manifest.v1",
        "request_identity": {
            "artifact_version_id": request["artifact"]["artifact_version_id"],
            "artifact_hash": request["artifact"]["artifact_hash"],
            "run_id": request["run_id"],
            "symbol": symbol,
            "execution_mode": request["execution_mode"],
        },
        "streams": streams,
        "transforms": [],
        "summary": {
            "status": "complete",
            "strict_eligible": True,
            "degraded": False,
            "degraded_reasons": [],
            "permitted_uses": {
                "backtest": True,
                "golden_replay": False,
                "paper": False,
            },
        },
    }


def build_artifact_response(
    *,
    request: dict[str, Any],
    simulation: dict[str, Any],
    data_manifest: dict[str, Any],
    coverage_manifest: dict[str, Any],
    capability: dict[str, Any],
    provider_process_fingerprint: str,
    connector_version: str,
    provider_name: str,
) -> dict[str, Any]:
    result_v2 = {
        "schema_version": "cutie.backtest_result.v2",
        "trades": simulation["trades"],
        "equity_curve": simulation["equity_curve"],
        "metrics": simulation["metrics"],
        "data_manifest": data_manifest,
    }
    cap_hash = capability_hash(capability)
    coverage_hash = canonical_json_sha256(coverage_manifest)
    trace = {
        "schema": "cutie.strategy_execution_trace.v1",
        "run_id": request["run_id"],
        "artifact_version_id": request["artifact"]["artifact_version_id"],
        "artifact_hash": request["artifact"]["artifact_hash"],
        "symbol": request["execution_params"]["symbol"],
        "trades": simulation["trace_trades"],
    }
    trace_hash = canonical_json_sha256(trace)
    evidence = {
        "schema": "cutie.strategy_execution_evidence.v1",
        "run_id": request["run_id"],
        "artifact_version_id": request["artifact"]["artifact_version_id"],
        "expected_artifact_hash": request["artifact"]["artifact_hash"],
        "executed_artifact_hash": request["artifact"]["artifact_hash"],
        "capability_hash": cap_hash,
        "provider_revision": capability["provider_revision"],
        "provider_process_fingerprint": provider_process_fingerprint,
        "connector_version": connector_version,
        "kernel_api_version": "1",
        "coverage_manifest_hash": coverage_hash,
        "execution_trace_hash": trace_hash,
        "result_schema": "cutie.backtest_result.v2",
        "result_hash_normalized": canonical_json_sha256(result_v2),
        "data_manifest_hash": canonical_json_sha256(data_manifest),
        "executed_params_hash": canonical_json_sha256(request["execution_params"]),
    }
    return {
        "result_status": "success",
        "schema_version": result_v2["schema_version"],
        "provider_run_id": f"artifact_{request['run_id']}",
        "provider_name": provider_name,
        "provider_revision": capability["provider_revision"],
        "engine_name": "strategy-kernel",
        "engine_version": "1",
        "data_source": data_manifest["source"],
        "result_hash": canonical_json_sha256(result_v2),
        "metrics": result_v2["metrics"],
        "equity_curve": result_v2["equity_curve"],
        "trades": result_v2["trades"],
        "data_manifest": result_v2["data_manifest"],
        "assumptions": {
            "strategy_binding": "immutable_artifact",
            "execution_mode": request["execution_mode"],
            "funding": "excluded",
        },
        "limitations": {
            "no_live_trading": True,
            "paper_runner_deployed": False,
        },
        "raw_report": {
            "kernel_api_version": "1",
            "diagnostics": simulation["diagnostics"],
            "decision_ledger_hash": canonical_json_sha256(simulation["decisions"]),
            "fill_ledger_hash": canonical_json_sha256(simulation["fill_ledger"]),
            "cost_ledger_hash": canonical_json_sha256(simulation["cost_ledger"]),
        },
        "capability_snapshot": copy.deepcopy(capability),
        "capability_hash": cap_hash,
        "coverage_manifest": coverage_manifest,
        "coverage_manifest_hash": coverage_hash,
        "execution_trace": trace,
        "execution_trace_hash": trace_hash,
        "execution_evidence": evidence,
        "execution_evidence_hash": canonical_json_sha256(evidence),
    }


def _interval_seconds(interval: str) -> int:
    return (
        int(interval[:-1]) * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[interval[-1]]
    )


__all__ = [
    "CoverageInput",
    "EXECUTION_MAX_RANGE_DAYS",
    "EXECUTION_REQUEST_SCHEMA",
    "ValidatedExecution",
    "build_artifact_response",
    "build_coverage_manifest",
    "is_strategy_execution_intent",
    "validate_execution_request",
]
