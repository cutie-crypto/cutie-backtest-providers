"""Frozen execution-wire validation and companion evidence builders for 62-2a."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Optional

from canonical_json import (
    CanonicalJsonError,
    canonical_decimal_str,
    canonical_json_sha256,
)
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
    kline_primary_bucket_required_end,
    kline_primary_bucket_required_start,
)

EXECUTION_REQUEST_SCHEMA = "cutie.strategy_execution_request.v1"
EXECUTION_MAX_RANGE_DAYS = 365
# SPEC §17: paper_tick wire types, additive to the frozen 62-2 historical_replay
# request/result above -- independent schema/key sets, zero shared validation
# path with _REQUEST_KEYS/validate_execution_request (§17.5 "探测/校验独立于
# isStrategyExecutionRequestIntent").
PAPER_TICK_REQUEST_SCHEMA = "cutie.strategy_paper_tick_request.v1"
PAPER_TICK_RESULT_SCHEMA = "cutie.strategy_paper_tick_result.v1"
ERR_PAPER_STATE_MISMATCH = "ERR_STRATEGY_PAPER_STATE_MISMATCH"
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
# SPEC §17.3 cutie.strategy_paper_tick_request.v1 -- top-level exactly 14 keys.
_PAPER_TICK_REQUEST_KEYS = {
    "schema",
    "execution_mode",
    "paper_run_id",
    "artifact",
    "strategy_spec",
    "artifact_manifest",
    "execution_params",
    "tick",
    "state",
    "prev_state_hash",
    "expected_capability_hash",
    "expected_provider_revision",
    "dispatch_nonce",
    "result_contract",
}
_TICK_KEYS = {"bar_open_at", "window_start_at", "execution_start_at"}
_PAPER_RESULT_CONTRACT_KEYS = {"result_schema", "coverage_schema"}
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{7,64}$")
_DECIMAL_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?$")
_MAX_BIGINT = 9223372036854775807


@dataclass(frozen=True)
class ValidatedExecution:
    request: dict[str, Any]
    plan: CompiledPlan


@dataclass(frozen=True)
class ValidatedPaperTick:
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


@dataclass(frozen=True)
class PaperCoverageInput:
    """Companion to ``CoverageInput`` for §7 coverage math anchored on a
    paper_tick's shared [window_start_at, target_bar_close) window (§17.1)
    instead of historical_replay's per-requirement warmup_bars-relative
    ``execution_params.start_at``/``end_at`` math -- kept as its own
    dataclass rather than adding fields to ``CoverageInput`` so
    ``build_coverage_manifest``'s historical_replay behavior is untouched.
    """

    requirement: dict[str, Any]
    checksum: str
    revision: str
    point_count: int
    required_start_at: int
    required_end_at: int
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
    # Probe keys must be exclusive to the bound execution request. The legacy
    # backtest dispatch envelope has carried dispatch_nonce +
    # expected_provider_revision since 62-1 as integrity evidence, so
    # expected_provider_revision is not an artifact-intent signal (sibling of
    # the connector-side fix shipped in @cutie-crypto/connector 3.9.13; on Pre
    # 2026-07-19 the connector variant rejected every legacy one-click
    # backtest as AGENT_REJECTED).
    # execution_mode/execution_params are likewise bound-only top-level keys
    # (the legacy envelope uses params/provider_params), so a partial bound
    # payload stripped of schema and the five structural keys still fails
    # closed instead of falling through to legacy (Codex follow-up review M2).
    return any(
        key in value
        for key in (
            "artifact",
            "strategy_spec",
            "artifact_manifest",
            "execution_mode",
            "execution_params",
            "expected_capability_hash",
            "result_contract",
        )
    )


def is_strategy_paper_tick_intent(value: Any) -> bool:
    """SPEC §17.5: explicit schema-prefix + execution_mode=='paper_tick'
    probe, never a key-set heuristic (both the request's ``artifact``/
    ``strategy_spec``/... keys and its own tighter 14-key shape overlap with
    ``is_strategy_execution_intent``'s fallback heuristic, so this must be
    checked first by the caller -- historical_replay's own probe is left
    byte-for-byte unchanged, zero regression per the 2026-07-19 探测键回归
    lesson referenced in the SPEC)."""
    if not isinstance(value, dict):
        return False
    schema = value.get("schema")
    return (
        isinstance(schema, str)
        and schema.startswith("cutie.strategy_paper_tick_request.")
        and value.get("execution_mode") == "paper_tick"
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
    lag_frames = max_primary_lag_frames(request["strategy_spec"])
    # Final review (Codex HIGH-1 temporary guard + HIGH-3/MED-4): every
    # feature's declared warmup must cover how far back it is referenced --
    # the kernel serves lags from stream as-of values captured on warmup
    # frames, so a shallower warmup is a deterministic runtime death the
    # golden past-edge precheck cannot see. Coarse features referenced at
    # lag>0 are rejected outright until the frames-index vs
    # own-interval-bucket lag semantics divergence (SPEC:721, confirmed by
    # probe) is resolved -- silently computing a wrong signal is worse than
    # refusing to compile. The primary K-line is deliberately NOT held to
    # this floor: its lag history is widened at fetch time (S20's shipped
    # artifact has primary warmup_bars=0 with cvd lag 6 and must keep
    # executing bit-for-bit).
    feature_needs = per_feature_lag_needs(request["strategy_spec"])
    primary_timeframe = request["strategy_spec"]["market"]["timeframe"]
    for feature in request["strategy_spec"]["features"]:
        need = feature_needs.get(feature["key"], 0)
        if need <= 0:
            continue
        if feature["interval"] != primary_timeframe:
            _error(
                ERR_SPEC_INVALID,
                f"$.strategy_spec.features[{feature['key']}].lag_bars",
                "coarse-interval feature references with lag_bars>0 are not "
                "supported yet (frames-index vs bucket lag semantics is "
                "unresolved); rework the spec or wait for the lag-semantics "
                "revision",
            )
        # SPEC §5.5 warmup 推导公式（2026-07-17 增补）: 每个 feature 的历史需求
        # 按该 feature 自己的 interval 计数 -- primitive 自身下限（rsi_wilder=
        # 10x period / rolling_sum,rolling_extreme=window_bars / rolling_quantile
        # =min_periods）与 lag_bars 是加法关系，"另计" 不是二选一的独立地板。
        # primitive_floor 的取值来源与 strategy_kernel.py 的 A 层 compile 校验
        # 同源（_validate_rsi_warmup 的 10*period、_validate_kline_primary_
        # requirements 的 window_bars/min_periods 两分支），避免两处对 SPEC 的
        # 解读分叉。
        primitive_floor = _primitive_warmup_floor(feature)
        required_warmup = primitive_floor + need
        for requirement in plan.artifact_manifest["data_requirements"]:
            if requirement["execution_role"] != "feature_input":
                continue
            if not requirement["stream_id"].startswith(feature["source_stream"] + "."):
                continue
            if requirement["interval"] != feature["interval"]:
                continue
            if requirement["warmup_bars"] < required_warmup:
                _error(
                    ERR_SPEC_INVALID,
                    f"$.artifact_manifest.data_requirements[{requirement['stream_id']}].warmup_bars",
                    "feature warmup_bars must cover the primitive's own "
                    "warmup floor plus the deepest lag reference (lag_bars, "
                    "+1 inside cross) -- SPEC 另计 lag_bars is additive, not "
                    "an independent floor",
                    required={
                        "primitive_floor": primitive_floor,
                        "lag_need": need,
                        "total": required_warmup,
                    },
                    actual=requirement["warmup_bars"],
                )
    for index, requirement in enumerate(plan.artifact_manifest["data_requirements"]):
        step_seconds = _interval_seconds(requirement["interval"])
        # Phase 1 review (Codex HIGH-1): the provider widens the primary
        # fetch by the spec's max feature lag (frames history for the
        # kernel's index-based lag resolution) -- the range budget must
        # account for that same widening or a legal lag_bars value quietly
        # buys a fetch far beyond max_range_days.
        warmup_bars = requirement["warmup_bars"]
        if requirement["execution_role"] == "primary_execution_kline":
            warmup_bars = max(warmup_bars, lag_frames)
        effective_start = max(
            0,
            params["start_at"] - warmup_bars * step_seconds,
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


# SPEC §17.1.4: paper's window grows every tick and is bounded independently
# of historical_replay's EXECUTION_MAX_RANGE_DAYS -- 1h-bucket run life cap.
PAPER_WINDOW_MAX_BARS = 26280


def validate_paper_tick_request(
    value: Any,
    capability: dict[str, Any],
    advertised_capability_hash: str,
) -> ValidatedPaperTick:
    """SPEC §17.3 cutie.strategy_paper_tick_request.v1 wire validation.

    Deliberately independent of ``validate_execution_request``: separate
    exact-key set, separate result_contract, separate hash-mismatch code
    (``ERR_PAPER_STATE_MISMATCH``) -- the historical_replay validator above
    is untouched by this function.
    """
    request = _exact(value, _PAPER_TICK_REQUEST_KEYS, "$")
    if request["schema"] != PAPER_TICK_REQUEST_SCHEMA:
        _error(
            ERR_SPEC_INVALID,
            "$.schema",
            "unsupported paper tick request schema",
            actual=request["schema"],
        )
    if request["execution_mode"] != "paper_tick":
        _error(
            ERR_SPEC_INVALID, "$.execution_mode", "API only permits paper_tick"
        )
    _decimal_id(request["paper_run_id"], "$.paper_run_id")
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

    tick = _exact(request["tick"], _TICK_KEYS, "$.tick")
    for key in ("bar_open_at", "window_start_at", "execution_start_at"):
        if (
            isinstance(tick[key], bool)
            or not isinstance(tick[key], int)
            or not 0 <= tick[key] <= 2**53 - 1
        ):
            _error(
                ERR_SPEC_INVALID,
                f"$.tick.{key}",
                "must be a non-negative safe integer",
            )
    if not tick["window_start_at"] <= tick["execution_start_at"] <= tick["bar_open_at"]:
        _error(
            ERR_SPEC_INVALID,
            "$.tick",
            "must satisfy window_start_at <= execution_start_at <= bar_open_at",
        )
    # SPEC §17.2/§17.3: tick.* timestamps are epoch MILLISECONDS (Server
    # dispatches on a 3_600_000 hourly step) -- distinct from
    # execution_params.start_at/end_at, which reuse §6.2's execution_params
    # shape verbatim and therefore stay epoch SECONDS (the kernel's own
    # internal FeatureFrame/KernelState time axis is frozen at seconds, see
    # cutie_backtesting_provider.py's ``_canonical_kline_rows``). Every
    # tick.* value below is compared in milliseconds (execution_params
    # values scaled *1000 to match) -- never compared to a bare
    # seconds-typed quantity, and never handed to a seconds-typed kernel
    # call without an explicit //1000 conversion at the call site.
    step_seconds = _interval_seconds(params["timeframe"])
    step_ms = step_seconds * 1000
    if tick["bar_open_at"] % 1000 != 0 or tick["window_start_at"] % 1000 != 0:
        _error(
            ERR_SPEC_INVALID,
            "$.tick",
            "bar_open_at/window_start_at must be whole-second epoch "
            "millisecond timestamps",
        )
    target_bar_close_ms = tick["bar_open_at"] + step_ms
    if params["start_at"] * 1000 != tick["execution_start_at"]:
        _error(
            ERR_BINDING_MISMATCH,
            "$.execution_params.start_at",
            "must equal tick.execution_start_at (milliseconds, "
            "execution_params.start_at scaled *1000)",
        )
    if params["end_at"] * 1000 < target_bar_close_ms:
        _error(
            ERR_BINDING_MISMATCH,
            "$.execution_params.end_at",
            "must cover the tick's own target frame close",
            required={"minimum_ms": target_bar_close_ms},
            actual=params["end_at"] * 1000,
        )
    if (tick["bar_open_at"] - tick["window_start_at"]) % step_ms != 0:
        _error(
            ERR_SPEC_INVALID,
            "$.tick.window_start_at",
            "must align to the primary timeframe grid",
        )
    window_bars = (tick["bar_open_at"] - tick["window_start_at"]) // step_ms
    if window_bars > PAPER_WINDOW_MAX_BARS:
        _error(
            ERR_SPEC_INVALID,
            "$.tick.window_start_at",
            "window exceeds PAPER_WINDOW_MAX_BARS; the run must stop and a "
            "follow-on run must anchor a new window",
            required={"max_bars": PAPER_WINDOW_MAX_BARS},
            actual=window_bars,
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
        request["result_contract"], _PAPER_RESULT_CONTRACT_KEYS, "$.result_contract"
    )
    expected_contract = {
        "result_schema": PAPER_TICK_RESULT_SCHEMA,
        "coverage_schema": "cutie.strategy_coverage_manifest.v1",
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

    # SPEC §17.3: state=null iff prev_state_hash=null (first tick); otherwise
    # Provider must recompute state's canonical hash and reject a mismatch
    # without executing.
    state = request["state"]
    prev_state_hash = request["prev_state_hash"]
    if state is None:
        if prev_state_hash is not None:
            _error(
                ERR_SPEC_INVALID,
                "$.prev_state_hash",
                "must be null when state is null (first tick)",
            )
        # Pi medium hardening: a genuine first tick's target bar is always
        # the run's own execution_start_at (Server's cursor derivation
        # guarantees this) -- reject a state=null request whose bar_open_at
        # disagrees instead of silently treating a Server bug (a later,
        # non-first tick mis-dispatched with state=null) as if it were tick
        # one, which would discard real accumulated position/equity/
        # trade_seq history.
        if tick["bar_open_at"] != tick["execution_start_at"]:
            _error(
                ERR_SPEC_INVALID,
                "$.tick.bar_open_at",
                "first tick (state=null) must target the run's own "
                "execution_start_at bar",
                required=tick["execution_start_at"],
                actual=tick["bar_open_at"],
            )
    else:
        if not isinstance(state, dict):
            _error(ERR_SPEC_INVALID, "$.state", "must be an object or null")
        _hash(prev_state_hash, "$.prev_state_hash")
        try:
            state_hash = canonical_json_sha256(state)
        except CanonicalJsonError:
            _error(
                ERR_SPEC_INVALID,
                "$.state",
                "state is not canonical_json.v1 serializable",
            )
        if state_hash != prev_state_hash:
            _error(
                ERR_PAPER_STATE_MISMATCH,
                "$.state",
                "state hash does not match prev_state_hash",
                required=prev_state_hash,
                actual=state_hash,
            )

    return ValidatedPaperTick(copy.deepcopy(request), plan)


_KLINE_PRIMARY_PREFIX = "kline.primary."


def _primitive_warmup_floor(feature: dict[str, Any]) -> int:
    """SPEC §5.5 per-primitive warmup floor, counted in the feature's own
    interval's buckets -- the same three-way split as strategy_kernel.py's A
    layer (``_validate_rsi_warmup``'s ``10 * period``,
    ``_validate_kline_primary_requirements``'s ``window_bars``/
    ``min_periods`` branches for coarse kline.primary requirements), read
    here from the feature's own params so this preflight's additive lag
    floor (below) shares one source of truth with the compile-time floor
    instead of re-deriving it. ``cross``'s own ``+1`` and lag_bars are a
    separate, already-computed additive term (``per_feature_lag_needs``);
    this floor is the primitive's own lookback, orthogonal to lag.
    """
    primitive = feature["primitive"]
    params = feature["params"]
    if primitive in {"rolling_sum", "rolling_extreme"}:
        return params["window_bars"]
    if primitive == "rolling_quantile":
        return params["min_periods"]
    if primitive == "rsi_wilder":
        return 10 * params["period"]
    return 0


def per_feature_lag_needs(strategy_spec: Any) -> dict[str, int]:
    """Per-feature-key max history need (lag_bars, +1 inside cross) across
    every reference in the spec. Companion to ``max_primary_lag_frames``:
    that one sizes the primary frames history, this one drives the
    preflight floor "a feature's declared warmup must cover how far back it
    is ever referenced" (final review Codex HIGH-3/MED-4 -- without it a
    shallow-warmup feature referenced at deeper lag compiles green and dies
    deterministically at runtime, taking the whole run's evidence with it)."""

    needs: dict[str, int] = {}

    def walk(node: Any, inside_cross: bool) -> None:
        if isinstance(node, dict):
            if node.get("node") == "feature":
                lag = node.get("lag_bars")
                key = node.get("key")
                if isinstance(lag, int) and not isinstance(lag, bool) and isinstance(key, str):
                    need = lag + (1 if inside_cross else 0)
                    if need > needs.get(key, -1):
                        needs[key] = need
            child_inside = inside_cross or node.get("node") == "cross"
            for value in node.values():
                walk(value, child_inside)
        elif isinstance(node, list):
            for value in node:
                walk(value, inside_cross)

    walk(strategy_spec, False)
    return needs


def max_primary_lag_frames(strategy_spec: Any) -> int:
    """Largest number of *primary frames* of history any feature reference in
    the (already-validated) spec needs. The kernel resolves feature lags as
    frames-index offsets (StrategyKernel._value: frames[index - lag_bars]),
    and ``cross`` additionally evaluates each operand at t-1, so an operand
    inside a cross needs lag_bars + 1 frames (Phase 1 review Codex HIGH-3).
    This is the minimum primary warmup depth for every lag reference to be
    resolvable from the first evaluation frame -- shared by the preflight
    range budget (a lag-widened fetch must not bypass max_range_days, Codex
    HIGH-1) and the provider's actual fetch widening."""

    best = 0

    def walk(node: Any, inside_cross: bool) -> None:
        nonlocal best
        if isinstance(node, dict):
            if node.get("node") == "feature":
                lag = node.get("lag_bars")
                if isinstance(lag, int) and not isinstance(lag, bool):
                    need = lag + (1 if inside_cross else 0)
                    if need > best:
                        best = need
            child_inside = inside_cross or node.get("node") == "cross"
            for value in node.values():
                walk(value, child_inside)
        elif isinstance(node, list):
            for value in node:
                walk(value, inside_cross)

    walk(strategy_spec, False)
    return best


def _matching_feature(
    strategy_spec: dict[str, Any], requirement: dict[str, Any]
) -> Optional[dict[str, Any]]:
    return next(
        (
            feature
            for feature in strategy_spec["features"]
            if requirement["stream_id"].startswith(feature["source_stream"] + ".")
            and feature["interval"] == requirement["interval"]
        ),
        None,
    )


def _is_kline_primary_derived(
    strategy_spec: dict[str, Any], requirement: dict[str, Any]
) -> bool:
    """A ``kind=feature`` requirement backing a §5.5 coarse ``kline.primary.*``
    stream (resampled from the primary K-line via ``ohlcv_resample.v1``, never
    fetched from an external source, never reconciled against result.v2)."""
    if requirement["kind"] != "feature" or not requirement["stream_id"].startswith(
        _KLINE_PRIMARY_PREFIX
    ):
        return False
    feature = _matching_feature(strategy_spec, requirement)
    return feature is not None and feature["source_stream"].startswith(
        _KLINE_PRIMARY_PREFIX
    )


def build_coverage_manifest(
    request: dict[str, Any],
    inputs: list[CoverageInput],
    data_manifest: dict[str, Any],
) -> dict[str, Any]:
    requirements = request["artifact_manifest"]["data_requirements"]
    strategy_spec = request["strategy_spec"]
    by_stream = {item.requirement["stream_id"]: item for item in inputs}
    if set(by_stream) != {item["stream_id"] for item in requirements}:
        _error(
            ERR_COVERAGE_INCOMPLETE,
            "$.coverage",
            "stream evidence does not equal requirement exact-set",
        )
    symbol = request["execution_params"]["symbol"]
    primary_requirement = next(
        item for item in requirements if item["execution_role"] == "primary_execution_kline"
    )
    primary_item = by_stream[primary_requirement["stream_id"]]
    streams: list[dict[str, Any]] = []
    transforms: list[dict[str, Any]] = []
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
        derived = not primary and _is_kline_primary_derived(strategy_spec, requirement)
        if derived:
            # A coarse kline.primary requirement's required_start must be
            # bucket-aligned, not a naive offset from an unaligned start_at:
            # the as-of anchor for the first decision frame is the bucket
            # that already closed at-or-before the bucket *containing*
            # start_at, which can be more than warmup_bars*step earlier than
            # start_at itself when start_at falls partway through a bucket.
            # This must match the Provider's own fetch range exactly (see
            # kline_primary_bucket_required_start), or a correctly-fetched
            # stream fails this exact-count check.
            required_start = kline_primary_bucket_required_start(
                request["execution_params"]["start_at"], requirement["warmup_bars"], step
            )
        else:
            required_start = max(
                0,
                request["execution_params"]["start_at"]
                - requirement["warmup_bars"] * step,
            )
        if derived:
            # Symmetric to required_start: execution_params.end_at is a
            # primary-granularity boundary that need not land on a coarse
            # bucket edge, and the last bucket any decision frame closing
            # at-or-before end_at could ever as-of read is the one that
            # closed at-or-before it (floored to the bucket grid) — comparing
            # against the raw end_at would demand a fractional/impossible
            # bucket count even when the stream is genuinely complete for
            # every frame that actually needs it.
            required_end = kline_primary_bucket_required_end(
                request["execution_params"]["end_at"], step
            )
        else:
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

        if derived:
            feature = _matching_feature(strategy_spec, requirement)
            # §5.5: derived stream_id is `kline.primary.<field>@<interval>`
            # (never the generic `.{symbol}` suffix); revision is inherited
            # verbatim from the primary stream it was resampled from.
            stream_id = f"{feature['source_stream']}@{requirement['interval']}"
            revision_value = primary_item.revision
        else:
            stream_id = f"{requirement['stream_id']}.{symbol}"
            revision_value = item.revision

        streams.append(
            {
                "stream_id": stream_id,
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
                    "warmup_start_at": required_start,
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
                "revision": {"schema": "source_revision.v1", "value": revision_value},
                "checksum": {"algo": "sha256", "value": checksum},
                "permitted_uses": {"backtest": True, "golden_replay": False, "paper": False},
                "status": "complete",
            }
        )
        if derived:
            transforms.append(
                {
                    "output_stream_id": stream_id,
                    "input_stream_ids": [f"{primary_requirement['stream_id']}.{symbol}"],
                    "transform": "ohlcv_resample.v1",
                    "transform_version": "1",
                    "params": {"target_interval": requirement["interval"]},
                    "synthetic_ranges": [],
                    "checksum": {"algo": "sha256", "value": checksum},
                }
            )
    streams.sort(key=lambda stream: stream["stream_id"])
    transforms.sort(key=lambda transform: transform["output_stream_id"])

    # §7.4: deterministic reduction, not Provider-free-filled. All streams
    # this Provider ever returns are already held to exact-complete coverage
    # (any incompleteness raised above); ohlcv_resample.v1 is the only
    # transform implemented and its output is never synthetic (§7.3), so the
    # reduction only needs to stay honest as future degraded transforms land.
    # The fourth §7.4 golden-strict element — "revision/checksum 固定" — is
    # checked explicitly rather than assumed: a stream with a blank revision
    # or checksum has nothing deterministic to pin a golden fixture to, even
    # if its coverage happens to be exact-complete.
    golden_replay = (
        all(stream["status"] == "complete" for stream in streams)
        and all(not transform["synthetic_ranges"] for transform in transforms)
        and all(
            stream["revision"]["value"] and stream["checksum"]["value"]
            for stream in streams
        )
    )
    permitted_uses = {
        "backtest": True,
        "golden_replay": golden_replay,
        "paper": False,
    }
    for stream in streams:
        stream["permitted_uses"] = dict(permitted_uses)
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
        "transforms": transforms,
        "summary": {
            "status": "complete",
            "strict_eligible": True,
            "degraded": False,
            "degraded_reasons": [],
            "permitted_uses": permitted_uses,
        },
    }


def build_paper_tick_coverage_manifest(
    request: dict[str, Any],
    inputs: list[PaperCoverageInput],
) -> dict[str, Any]:
    """SPEC §7 coverage manifest for a paper_tick request.

    Every stream in a paper_tick request shares one required window,
    [tick.window_start_at, target_bar_close) -- §17.1: window_start_at is
    frozen at run creation to already cover every requirement's warmup/lag
    need, so (unlike ``build_coverage_manifest``'s historical_replay path)
    there is no per-requirement ``warmup_bars``-relative math here; callers
    pass each stream's already-computed required/actual range via
    ``PaperCoverageInput`` instead.
    """
    requirements = request["artifact_manifest"]["data_requirements"]
    strategy_spec = request["strategy_spec"]
    by_stream = {item.requirement["stream_id"]: item for item in inputs}
    if set(by_stream) != {item["stream_id"] for item in requirements}:
        _error(
            ERR_COVERAGE_INCOMPLETE,
            "$.coverage",
            "stream evidence does not equal requirement exact-set",
        )
    symbol = request["execution_params"]["symbol"]
    primary_requirement = next(
        item for item in requirements if item["execution_role"] == "primary_execution_kline"
    )
    primary_item = by_stream[primary_requirement["stream_id"]]
    streams: list[dict[str, Any]] = []
    transforms: list[dict[str, Any]] = []
    for requirement in requirements:
        item = by_stream[requirement["stream_id"]]
        if item.point_count <= 0:
            _error(
                ERR_COVERAGE_INCOMPLETE,
                f"$.coverage.{requirement['stream_id']}",
                "stream is empty",
            )
        step = _interval_seconds(requirement["interval"])
        derived = (
            requirement["execution_role"] != "primary_execution_kline"
            and _is_kline_primary_derived(strategy_spec, requirement)
        )
        if (
            (item.required_end_at - item.required_start_at) % step != 0
            or item.actual_start_at > item.required_start_at
            or item.available_through < item.required_end_at
            or item.point_count != (item.required_end_at - item.required_start_at) // step
        ):
            _error(
                ERR_COVERAGE_INCOMPLETE,
                f"$.coverage.{requirement['stream_id']}",
                "stream does not cover the complete aligned required range",
            )
        if derived:
            feature = _matching_feature(strategy_spec, requirement)
            stream_id = f"{feature['source_stream']}@{requirement['interval']}"
            revision_value = primary_item.revision
        else:
            stream_id = f"{requirement['stream_id']}.{symbol}"
            revision_value = item.revision
        streams.append(
            {
                "stream_id": stream_id,
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
                    # §7.2's coverage schema is frozen at epoch SECONDS
                    # (shared with historical_replay's build_coverage_manifest,
                    # itself keyed off execution_params.start_at/end_at) --
                    # tick.window_start_at/bar_open_at are wire MILLISECONDS
                    # (§17.2/§17.3), so both are //1000'd here at the one
                    # point this function reads them directly. Every other
                    # field below (required_start_at/actual_range/available_
                    # through) already arrives pre-converted to seconds via
                    # PaperCoverageInput (computed from the orchestration's
                    # seconds-typed fetch loop).
                    "start_at": request["tick"]["window_start_at"] // 1000,
                    "end_at": request["tick"]["bar_open_at"] // 1000
                    + _interval_seconds(request["strategy_spec"]["market"]["timeframe"]),
                    "warmup_start_at": item.required_start_at,
                },
                "actual_range": {
                    "start_at": item.actual_start_at,
                    "end_at": item.actual_end_at,
                    "available_through": item.available_through,
                },
                "point_count": item.point_count,
                "granularity": requirement["interval"],
                "freshness": {
                    "checked_at": request["tick"]["bar_open_at"] // 1000,
                    "age_seconds": 0,
                    "max_age_seconds": requirement["max_freshness_seconds"],
                },
                "gaps": [],
                "revision": {"schema": "source_revision.v1", "value": revision_value},
                "checksum": {"algo": "sha256", "value": item.checksum},
                "permitted_uses": {"backtest": False, "golden_replay": False, "paper": True},
                "status": "complete",
            }
        )
        if derived:
            transforms.append(
                {
                    "output_stream_id": stream_id,
                    "input_stream_ids": [f"{primary_requirement['stream_id']}.{symbol}"],
                    "transform": "ohlcv_resample.v1",
                    "transform_version": "1",
                    "params": {"target_interval": requirement["interval"]},
                    "synthetic_ranges": [],
                    "checksum": {"algo": "sha256", "value": item.checksum},
                }
            )
    streams.sort(key=lambda stream: stream["stream_id"])
    transforms.sort(key=lambda transform: transform["output_stream_id"])
    permitted_uses = {"backtest": False, "golden_replay": False, "paper": True}
    for stream in streams:
        stream["permitted_uses"] = dict(permitted_uses)
    return {
        "schema": "cutie.strategy_coverage_manifest.v1",
        "request_identity": {
            "artifact_version_id": request["artifact"]["artifact_version_id"],
            "artifact_hash": request["artifact"]["artifact_hash"],
            "run_id": request["paper_run_id"],
            "symbol": symbol,
            "execution_mode": request["execution_mode"],
        },
        "streams": streams,
        "transforms": transforms,
        "summary": {
            "status": "complete",
            "strict_eligible": True,
            "degraded": False,
            "degraded_reasons": [],
            "permitted_uses": permitted_uses,
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
    "ERR_PAPER_STATE_MISMATCH",
    "EXECUTION_MAX_RANGE_DAYS",
    "EXECUTION_REQUEST_SCHEMA",
    "PAPER_TICK_REQUEST_SCHEMA",
    "PAPER_TICK_RESULT_SCHEMA",
    "PAPER_WINDOW_MAX_BARS",
    "PaperCoverageInput",
    "ValidatedExecution",
    "ValidatedPaperTick",
    "build_artifact_response",
    "build_coverage_manifest",
    "build_paper_tick_coverage_manifest",
    "is_strategy_execution_intent",
    "is_strategy_paper_tick_intent",
    "validate_execution_request",
    "validate_paper_tick_request",
]
