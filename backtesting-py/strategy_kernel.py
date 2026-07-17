"""StrategySpec v2 compiler and deterministic execution kernel.

This module is deliberately pure: it does not fetch data, import strategy code, or
call backtesting.py.  Historical replay and the future paper runner both advance
state through :meth:`StrategyKernel.evaluate`; ``simulate`` is only an ordered
loop plus the explicit end-of-data close required by result.v2.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from decimal import (
    ROUND_CEILING,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    Underflow,
    localcontext,
)
from typing import Any, Iterable, Optional

from canonical_json import (
    CanonicalJsonError,
    canonical_decimal_str,
    canonical_json,
    canonical_json_sha256,
    normalize_numbers_for_hash,
)

STRATEGY_SPEC_SCHEMA = "cutie.strategy_spec.v2"
ARTIFACT_MANIFEST_SCHEMA = "cutie.strategy_artifact_manifest.v1"
ARTIFACT_DIGEST_SCHEMA = "cutie.strategy_artifact_digest.v1"
CAPABILITY_SCHEMA = "cutie.strategy_execution_capabilities.v1"
RESULT_SCHEMA = "cutie.backtest_result.v2"
COMPILER_TOOL_ID = "local.strategy_spec_v2.compiler"

ERR_SPEC_INVALID = "ERR_STRATEGY_SPEC_INVALID"
ERR_SPEC_UNSUPPORTED = "ERR_STRATEGY_SPEC_UNSUPPORTED"
ERR_CAPABILITY_MISMATCH = "ERR_STRATEGY_CAPABILITY_MISMATCH"
ERR_COVERAGE_INCOMPLETE = "ERR_STRATEGY_COVERAGE_INCOMPLETE"
ERR_BINDING_MISMATCH = "ERR_STRATEGY_EXECUTION_BINDING_MISMATCH"

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{7,64}$")
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SCHEMA_KEY_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_CAPABILITY_VALUE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_INTERVAL_RE = re.compile(r"^[1-9][0-9]*(?:m|h|d|w)$")
_DECIMAL_CONTEXT = Context(prec=34, rounding=ROUND_HALF_EVEN)
_DECIMAL_CONTEXT.traps[InvalidOperation] = True
_DECIMAL_CONTEXT.traps[DivisionByZero] = True
_DECIMAL_CONTEXT.traps[Overflow] = True
_DECIMAL_CONTEXT.traps[Underflow] = True
_JS_SAFE_INT = 2**53 - 1

# SPEC §5.5 (2026-07-17 增补, 62-2b Phase 1): kline-sourced feature namespace.
_KLINE_PRIMARY_PREFIX = "kline.primary."
_KLINE_PRIMARY_FIELDS = {"open", "high", "low", "close"}
_KLINE_PRIMARY_COARSE_INTERVALS = {"4h", "1d"}
_KLINE_PRIMARY_COARSE_BASE_TIMEFRAME = "1h"
# The four feature primitives §5.5 registers; param shapes are frozen cross-provider.
_KNOWN_PRIMITIVES = {"rolling_sum", "rolling_quantile", "rsi_wilder", "rolling_extreme"}

_SPEC_KEYS = {
    "schema",
    "strategy_family",
    "market",
    "parameters",
    "features",
    "entry",
    "exit",
    "risk",
    "execution",
}
_MARKET_KEYS = {"market_type", "exchange", "symbols", "timeframe"}
_PARAMETER_KEYS = {"key", "type", "value", "mutable", "bounds", "allowed_values"}
_FEATURE_KEYS = {
    "key",
    "primitive",
    "primitive_version",
    "source_stream",
    "interval",
    "value_kind",
    "output_type",
    "params",
    "required",
}
_MANIFEST_KEYS = {
    "schema",
    "artifact_kind",
    "strategy_spec_schema",
    "spec_hash",
    "compiler",
    "kernel_contract",
    "capability_requirements",
    "data_requirements",
    "source_materials",
    "provenance_policy",
}
_CAPABILITY_REQUIREMENT_KEYS = {
    "operators",
    "features",
    "data_sources",
    "cost_models",
    "data_transforms",
    "result_schemas",
    "coverage_schemas",
    "trace_schemas",
    "evidence_schemas",
}
_DATA_REQUIREMENT_KEYS = {
    "stream_id",
    "kind",
    "execution_role",
    "provider",
    "storage_source",
    "result_source",
    "exchange",
    "market",
    "symbols",
    "interval",
    "warmup_bars",
    "max_freshness_seconds",
    "gap_policy",
    "allowed_transforms",
}
_SOURCE_KEYS = {
    "source_artifact_id",
    "role",
    "source_sha256",
    "content_sha256",
    "parser_version",
    "ingestion_schema",
    "ingestion_signature_version",
    "ingestion_key_id",
    "ingestion_signature",
}
_CAPABILITY_KEYS = {
    "schema",
    "provider_revision",
    "spec_schemas",
    "kernel_api_versions",
    "execution_modes",
    "operators",
    "feature_primitives",
    "data_sources",
    "cost_models",
    "data_transforms",
    "result_schemas",
    "coverage_schemas",
    "trace_schemas",
    "evidence_schemas",
}


class StrategyContractError(ValueError):
    """Structured, redaction-safe artifact failure."""

    def __init__(
        self,
        code: str,
        path: str,
        message: str,
        *,
        required: Any = None,
        actual: Any = None,
    ) -> None:
        super().__init__(f"{path}: {message}")
        self.code = code
        self.path = path
        self.message = message
        self.required = required
        self.actual = actual

    def detail(self) -> dict[str, Any]:
        return {
            "schema": STRATEGY_SPEC_SCHEMA,
            "path": self.path,
            "required": self.required,
            "actual": self.actual,
        }


class KernelExecutionError(StrategyContractError):
    pass


@dataclass(frozen=True)
class CompiledPlan:
    strategy_spec: dict[str, Any]
    artifact_manifest: dict[str, Any]
    spec_hash: str
    manifest_hash: str
    artifact_hash: str
    parameter_types: dict[str, str]
    feature_types: dict[str, str]


@dataclass(frozen=True)
class FeatureFrame:
    bar_open_at: int
    bar_close_at: int
    available_at: int
    symbol: str
    values: dict[str, Any]
    stream_revisions: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "bar_open_at": self.bar_open_at,
            "bar_close_at": self.bar_close_at,
            "available_at": self.available_at,
            "symbol": self.symbol,
            "values": copy.deepcopy(self.values),
            "stream_revisions": copy.deepcopy(self.stream_revisions),
        }


@dataclass
class PendingEntry:
    signal_index: int
    stop_value: Optional[Decimal]
    take_value: Optional[Decimal]


@dataclass
class Position:
    side: str
    qty: Decimal
    entry_price: Decimal
    opened_at: int
    stop_loss: Optional[Decimal]
    take_profit: Optional[Decimal]
    bars_held: int = 0
    pending_signal_exit: bool = False


@dataclass
class KernelState:
    """Mutable replay/paper state advanced one frame at a time by evaluate().

    ``frames`` holds every frame StrategyKernel.evaluate() has accepted,
    including warmup frames (``bar_open_at < execution_start_at``) appended
    for lagged feature/cross lookback. Any ``frame_index`` recorded in
    decisions/diagnostics/fill_ledger/cost_ledger is the physical index into
    this list, not an index into the evaluation-only window: the first
    evaluation-window frame has ``frame_index == warmup_bars`` (the number of
    warmup frames ahead of it), not 0. Warmup frames themselves can never
    appear in a frame_index there, since evaluate() returns before any
    decision logic runs for them.
    """

    equity: Decimal
    initial_capital: Decimal
    instrument_rules: dict[str, str]
    execution_start_at: int
    execution_end_at: int
    frames: list[FeatureFrame] = field(default_factory=list)
    pending_entry: Optional[PendingEntry] = None
    position: Optional[Position] = None
    last_exit_index: Optional[int] = None
    trades: list[dict[str, Any]] = field(default_factory=list)
    trace_trades: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    fill_ledger: list[dict[str, Any]] = field(default_factory=list)
    cost_ledger: list[dict[str, Any]] = field(default_factory=list)


def _raise(
    path: str,
    message: str,
    *,
    code: str = ERR_SPEC_INVALID,
    required: Any = None,
    actual: Any = None,
) -> None:
    raise StrategyContractError(code, path, message, required=required, actual=actual)


def _exact(value: Any, keys: set[str], path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _raise(
            path,
            "must be an object",
            required=sorted(keys),
            actual=type(value).__name__,
        )
    actual = set(value)
    if actual != keys:
        _raise(
            path,
            "exact keys required",
            required=sorted(keys),
            actual={"missing": sorted(keys - actual), "unknown": sorted(actual - keys)},
        )
    return value


def _safe_int(
    value: Any, path: str, minimum: int = 0, maximum: int = _JS_SAFE_INT
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        _raise(
            path,
            "must be a safe integer in range",
            required=[minimum, maximum],
            actual=value,
        )
    return value


def _canonical_decimal(
    value: Any,
    path: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    if not isinstance(value, str):
        _raise(path, "must be a canonical Decimal string", actual=type(value).__name__)
    try:
        parsed = Decimal(value)
        normalized = canonical_decimal_str(value)
    except (ArithmeticError, CanonicalJsonError, ValueError):
        _raise(path, "invalid canonical Decimal string", actual=value)
    if normalized != value:
        _raise(
            path,
            "must be exponent-free without redundant zeros or negative zero",
            actual=value,
        )
    if positive and parsed <= 0:
        _raise(path, "must be positive", actual=value)
    if nonnegative and parsed < 0:
        _raise(path, "must be non-negative", actual=value)
    return parsed


def _utf16_sorted_unique(
    values: Any, path: str, *, nonempty: bool = False
) -> list[str]:
    if not isinstance(values, list) or any(
        not isinstance(item, str) for item in values
    ):
        _raise(path, "must be a string array")
    expected = sorted(set(values), key=lambda item: item.encode("utf-16-be"))
    if values != expected or (nonempty and not values):
        _raise(
            path,
            "must be UTF-16 sorted and duplicate-free",
            required=expected,
            actual=values,
        )
    return values


def _canonical_scalar_tree(value: Any, path: str, depth: int = 0) -> None:
    if depth > 8:
        _raise(path, "exceeds maximum depth 8")
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int) and not isinstance(value, bool):
        _safe_int(value, path, minimum=-_JS_SAFE_INT)
        return
    if isinstance(value, list):
        if len(value) > 256:
            _raise(path, "array exceeds 256 items")
        for index, child in enumerate(value):
            _canonical_scalar_tree(child, f"{path}[{index}]", depth + 1)
        return
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                _raise(path, "object keys must be strings")
            _canonical_scalar_tree(child, f"{path}.{key}", depth + 1)
        return
    _raise(path, "contains a non-canonical scalar", actual=type(value).__name__)


def _sort_objects(
    items: Iterable[dict[str, Any]], fields: tuple[str, ...]
) -> list[dict[str, Any]]:
    unique = {canonical_json(item): item for item in items}

    def key(item: dict[str, Any]) -> tuple[Any, ...]:
        out: list[Any] = []
        for field_name in fields:
            value = item[field_name]
            if value is None:
                out.append((0, b""))
            elif isinstance(value, list):
                out.append((1, tuple(str(part).encode("utf-16-be") for part in value)))
            else:
                out.append((1, str(value).encode("utf-16-be")))
        return tuple(out)

    return sorted(unique.values(), key=key)


def _validate_parameter(item: Any, index: int) -> tuple[str, str]:
    path = f"$.strategy_spec.parameters[{index}]"
    obj = _exact(item, _PARAMETER_KEYS, path)
    key = obj["key"]
    if not isinstance(key, str) or _KEY_RE.fullmatch(key) is None:
        _raise(f"{path}.key", "invalid parameter key", actual=key)
    value_type = obj["type"]
    if value_type not in {"integer", "decimal", "boolean", "enum", "string"}:
        _raise(f"{path}.type", "unknown parameter type", actual=value_type)
    if not isinstance(obj["mutable"], bool):
        _raise(f"{path}.mutable", "must be boolean")
    if value_type == "integer":
        value = _safe_int(obj["value"], f"{path}.value", minimum=-_JS_SAFE_INT)
        bounds = _exact(obj["bounds"], {"min", "max"}, f"{path}.bounds")
        lower = _safe_int(bounds["min"], f"{path}.bounds.min", minimum=-_JS_SAFE_INT)
        upper = _safe_int(bounds["max"], f"{path}.bounds.max", minimum=-_JS_SAFE_INT)
        if not lower <= value <= upper:
            _raise(f"{path}.value", "outside declared bounds")
    elif value_type == "decimal":
        value = _canonical_decimal(obj["value"], f"{path}.value")
        bounds = _exact(obj["bounds"], {"min", "max"}, f"{path}.bounds")
        lower = _canonical_decimal(bounds["min"], f"{path}.bounds.min")
        upper = _canonical_decimal(bounds["max"], f"{path}.bounds.max")
        if not lower <= value <= upper:
            _raise(f"{path}.value", "outside declared bounds")
    elif value_type == "boolean":
        if not isinstance(obj["value"], bool) or obj["bounds"] is not None:
            _raise(path, "boolean requires a boolean value and null bounds")
    else:
        if not isinstance(obj["value"], str) or obj["bounds"] is not None:
            _raise(path, "string/enum requires a string value and null bounds")
    allowed = _utf16_sorted_unique(obj["allowed_values"], f"{path}.allowed_values")
    if value_type == "enum":
        if not allowed or obj["value"] not in allowed:
            _raise(f"{path}.value", "must be a declared enum value")
    elif allowed:
        _raise(f"{path}.allowed_values", "must be empty for non-enum")
    return key, "string" if value_type == "enum" else value_type


def _validate_feature(item: Any, index: int) -> tuple[str, str]:
    path = f"$.strategy_spec.features[{index}]"
    obj = _exact(item, _FEATURE_KEYS, path)
    key = obj["key"]
    if not isinstance(key, str) or _KEY_RE.fullmatch(key) is None:
        _raise(f"{path}.key", "invalid feature key", actual=key)
    for field_name in ("primitive", "source_stream"):
        value = obj[field_name]
        if not isinstance(value, str) or _SCHEMA_KEY_RE.fullmatch(value) is None:
            _raise(f"{path}.{field_name}", "invalid canonical key", actual=value)
    if not isinstance(obj["primitive_version"], str) or not obj["primitive_version"]:
        _raise(f"{path}.primitive_version", "must be a non-empty string")
    if (
        not isinstance(obj["interval"], str)
        or _INTERVAL_RE.fullmatch(obj["interval"]) is None
    ):
        _raise(f"{path}.interval", "invalid interval", actual=obj["interval"])
    if obj["value_kind"] not in {"level", "flow", "event", "price"}:
        _raise(f"{path}.value_kind", "unknown value kind", actual=obj["value_kind"])
    if obj["output_type"] not in {"integer", "decimal", "boolean", "string"}:
        _raise(f"{path}.output_type", "unknown output type", actual=obj["output_type"])
    if obj["required"] is not True:
        _raise(
            f"{path}.required",
            "StrategySpec v2 only permits true",
            actual=obj["required"],
        )
    if not isinstance(obj["params"], dict):
        _raise(f"{path}.params", "must be an object")
    _canonical_scalar_tree(obj["params"], f"{path}.params")
    primitive = obj["primitive"]
    if primitive == "rolling_sum":
        params = _exact(obj["params"], {"window_bars"}, f"{path}.params")
        _safe_int(
            params["window_bars"],
            f"{path}.params.window_bars",
            minimum=1,
            maximum=10000,
        )
    elif primitive == "rolling_quantile":
        params = _exact(
            obj["params"], {"window_bars", "quantile", "min_periods"}, f"{path}.params"
        )
        window_bars = _safe_int(
            params["window_bars"],
            f"{path}.params.window_bars",
            minimum=2,
            maximum=10000,
        )
        quantile = _canonical_decimal(params["quantile"], f"{path}.params.quantile")
        if not (Decimal(0) < quantile < Decimal(1)):
            _raise(
                f"{path}.params.quantile",
                "must be strictly between 0 and 1",
                actual=params["quantile"],
            )
        _safe_int(
            params["min_periods"],
            f"{path}.params.min_periods",
            minimum=1,
            maximum=window_bars,
        )
    elif primitive == "rsi_wilder":
        params = _exact(obj["params"], {"period"}, f"{path}.params")
        _safe_int(params["period"], f"{path}.params.period", minimum=2, maximum=1000)
    elif primitive == "rolling_extreme":
        params = _exact(obj["params"], {"window_bars", "mode"}, f"{path}.params")
        _safe_int(
            params["window_bars"],
            f"{path}.params.window_bars",
            minimum=1,
            maximum=10000,
        )
        if params["mode"] not in {"min", "max"}:
            _raise(f"{path}.params.mode", "must be min or max", actual=params["mode"])
    source_stream = obj["source_stream"]
    if source_stream.startswith(_KLINE_PRIMARY_PREFIX):
        field = source_stream[len(_KLINE_PRIMARY_PREFIX) :]
        if field not in _KLINE_PRIMARY_FIELDS:
            _raise(
                f"{path}.source_stream",
                "kline.primary field must be open/high/low/close",
                actual=source_stream,
            )
        if obj["value_kind"] != "price":
            _raise(
                f"{path}.value_kind",
                "kline.primary source requires price value_kind",
                actual=obj["value_kind"],
            )
    return key, obj["output_type"]


def _operator(
    node: str, op: str, arg_types: list[str], return_type: str
) -> dict[str, Any]:
    return {
        "node": node,
        "op": op,
        "version": "1",
        "arg_types": arg_types,
        "return_type": return_type,
    }


def _infer_value(
    expr: Any,
    parameters: dict[str, str],
    features: dict[str, str],
    operators: list[dict[str, Any]],
    path: str,
) -> str:
    if not isinstance(expr, dict):
        _raise(path, "must be a ValueExpr object")
    node = expr.get("node")
    if node == "literal":
        obj = _exact(expr, {"node", "value_type", "value"}, path)
        value_type = obj["value_type"]
        if value_type not in {"integer", "decimal", "boolean", "string"}:
            _raise(f"{path}.value_type", "unknown literal type", actual=value_type)
        value = obj["value"]
        if value_type == "integer":
            _safe_int(value, f"{path}.value", minimum=-_JS_SAFE_INT)
        elif value_type == "decimal":
            _canonical_decimal(value, f"{path}.value")
        elif value_type == "boolean" and not isinstance(value, bool):
            _raise(f"{path}.value", "must be boolean")
        elif value_type == "string" and not isinstance(value, str):
            _raise(f"{path}.value", "must be string")
        return value_type
    if node == "parameter":
        obj = _exact(expr, {"node", "key"}, path)
        if obj["key"] not in parameters:
            _raise(f"{path}.key", "unknown parameter", actual=obj["key"])
        return parameters[obj["key"]]
    if node == "feature":
        obj = _exact(expr, {"node", "key", "lag_bars"}, path)
        if obj["key"] not in features:
            _raise(f"{path}.key", "unknown feature", actual=obj["key"])
        _safe_int(obj["lag_bars"], f"{path}.lag_bars", maximum=10000)
        return features[obj["key"]]
    if node == "arithmetic":
        obj = _exact(expr, {"node", "op", "args"}, path)
        op = obj["op"]
        if op not in {"add", "sub", "mul", "div", "min", "max", "abs"}:
            _raise(f"{path}.op", "unknown arithmetic operator", actual=op)
        args = obj["args"]
        valid_arity = isinstance(args, list) and (
            (op == "abs" and len(args) == 1)
            or (op in {"sub", "div"} and len(args) == 2)
            or (op in {"add", "mul", "min", "max"} and len(args) >= 2)
        )
        if not valid_arity:
            _raise(f"{path}.args", "invalid arithmetic arity")
        arg_types = [
            _infer_value(arg, parameters, features, operators, f"{path}.args[{index}]")
            for index, arg in enumerate(args)
        ]
        if len(set(arg_types)) != 1 or arg_types[0] not in {"integer", "decimal"}:
            _raise(path, "arithmetic requires one identical numeric type")
        if op == "div" and arg_types[0] != "decimal":
            _raise(path, "div only accepts decimal")
        operators.append(_operator("arithmetic", op, arg_types, arg_types[0]))
        return arg_types[0]
    _raise(path, "unknown ValueExpr node", actual=node)


def _validate_condition(
    expr: Any,
    parameters: dict[str, str],
    features: dict[str, str],
    operators: list[dict[str, Any]],
    path: str,
) -> None:
    if not isinstance(expr, dict):
        _raise(path, "must be a ConditionExpr object")
    node = expr.get("node")
    if node in {"compare", "cross"}:
        obj = _exact(expr, {"node", "op", "left", "right"}, path)
        allowed = (
            {"gt", "gte", "lt", "lte", "eq", "neq"}
            if node == "compare"
            else {"crosses_above", "crosses_below"}
        )
        if obj["op"] not in allowed:
            _raise(f"{path}.op", "unknown condition operator", actual=obj["op"])
        left = _infer_value(
            obj["left"], parameters, features, operators, f"{path}.left"
        )
        right = _infer_value(
            obj["right"], parameters, features, operators, f"{path}.right"
        )
        if left != right:
            _raise(
                path,
                "implicit type conversion is forbidden",
                required=left,
                actual=right,
            )
        if (
            node == "cross" or obj["op"] in {"gt", "gte", "lt", "lte"}
        ) and left not in {"integer", "decimal"}:
            _raise(path, "ordered comparisons require numeric operands")
        operators.append(_operator(node, obj["op"], [left, right], "boolean"))
        return
    if node in {"all", "any"}:
        obj = _exact(expr, {"node", "args"}, path)
        if not isinstance(obj["args"], list) or not obj["args"]:
            _raise(f"{path}.args", "must be a non-empty array")
        for index, child in enumerate(obj["args"]):
            _validate_condition(
                child, parameters, features, operators, f"{path}.args[{index}]"
            )
        return
    if node == "not":
        obj = _exact(expr, {"node", "arg"}, path)
        _validate_condition(obj["arg"], parameters, features, operators, f"{path}.arg")
        return
    _raise(path, "unknown ConditionExpr node", actual=node)


def _validate_spec(
    spec: Any,
) -> tuple[dict[str, str], dict[str, str], dict[str, list[dict[str, Any]]]]:
    obj = _exact(spec, _SPEC_KEYS, "$.strategy_spec")
    if obj["schema"] != STRATEGY_SPEC_SCHEMA:
        _raise(
            "$.strategy_spec.schema",
            "unsupported schema",
            required=STRATEGY_SPEC_SCHEMA,
            actual=obj["schema"],
        )
    if (
        not isinstance(obj["strategy_family"], str)
        or _SCHEMA_KEY_RE.fullmatch(obj["strategy_family"]) is None
    ):
        _raise("$.strategy_spec.strategy_family", "invalid canonical key")
    market = _exact(obj["market"], _MARKET_KEYS, "$.strategy_spec.market")
    if market["market_type"] not in {"spot", "futures"}:
        _raise("$.strategy_spec.market.market_type", "must be spot or futures")
    if (
        not isinstance(market["exchange"], str)
        or _SCHEMA_KEY_RE.fullmatch(market["exchange"]) is None
    ):
        _raise("$.strategy_spec.market.exchange", "invalid exchange key")
    symbols = _utf16_sorted_unique(
        market["symbols"], "$.strategy_spec.market.symbols", nonempty=True
    )
    if any(not symbol or len(symbol) > 30 for symbol in symbols):
        _raise("$.strategy_spec.market.symbols", "symbols must be 1..30 characters")
    if (
        not isinstance(market["timeframe"], str)
        or _INTERVAL_RE.fullmatch(market["timeframe"]) is None
    ):
        _raise("$.strategy_spec.market.timeframe", "invalid timeframe")

    if not isinstance(obj["parameters"], list) or not isinstance(obj["features"], list):
        _raise("$.strategy_spec", "parameters and features must be arrays")
    parameter_pairs = [
        _validate_parameter(item, index) for index, item in enumerate(obj["parameters"])
    ]
    feature_pairs = [
        _validate_feature(item, index) for index, item in enumerate(obj["features"])
    ]
    if [item[0] for item in parameter_pairs] != sorted(
        {item[0] for item in parameter_pairs}, key=lambda x: x.encode("utf-16-be")
    ):
        _raise("$.strategy_spec.parameters", "must be sorted and duplicate-free by key")
    if [item[0] for item in feature_pairs] != sorted(
        {item[0] for item in feature_pairs}, key=lambda x: x.encode("utf-16-be")
    ):
        _raise("$.strategy_spec.features", "must be sorted and duplicate-free by key")
    for index, feature in enumerate(obj["features"]):
        source_stream = feature["source_stream"]
        if not source_stream.startswith(_KLINE_PRIMARY_PREFIX):
            continue
        if feature["interval"] == market["timeframe"]:
            continue
        # §5.5 as-of rule: coarse kline.primary features are Phase-1 gated to a
        # 1h primary decision timeframe resampled to 4h/1d targets only.
        if (
            market["timeframe"] != _KLINE_PRIMARY_COARSE_BASE_TIMEFRAME
            or feature["interval"] not in _KLINE_PRIMARY_COARSE_INTERVALS
        ):
            _raise(
                f"$.strategy_spec.features[{index}].interval",
                "kline.primary coarse feature requires a 1h primary timeframe "
                "and a 4h/1d target interval",
                required={
                    "primary_timeframe": _KLINE_PRIMARY_COARSE_BASE_TIMEFRAME,
                    "target_intervals": sorted(_KLINE_PRIMARY_COARSE_INTERVALS),
                },
                actual={
                    "primary_timeframe": market["timeframe"],
                    "feature_interval": feature["interval"],
                },
            )
    parameters = dict(parameter_pairs)
    features = dict(feature_pairs)
    operators: list[dict[str, Any]] = []

    entry = _exact(
        obj["entry"],
        {"condition", "side", "order_model", "cooldown_bars"},
        "$.strategy_spec.entry",
    )
    if entry["side"] not in {"long", "short"}:
        _raise("$.strategy_spec.entry.side", "must be long or short")
    if entry["order_model"] != "next_bar_open":
        _raise("$.strategy_spec.entry.order_model", "must be next_bar_open")
    _safe_int(entry["cooldown_bars"], "$.strategy_spec.entry.cooldown_bars")
    _validate_condition(
        entry["condition"],
        parameters,
        features,
        operators,
        "$.strategy_spec.entry.condition",
    )

    exit_spec = _exact(
        obj["exit"],
        {"stop_loss", "take_profit", "time_exit_bars", "signal_exit"},
        "$.strategy_spec.exit",
    )
    for field_name in ("stop_loss", "take_profit"):
        rule = _exact(
            exit_spec[field_name],
            {"model", "value"},
            f"$.strategy_spec.exit.{field_name}",
        )
        allowed = (
            {"fixed_percent", "feature_expression"}
            if field_name == "stop_loss"
            else {"fixed_percent", "r_multiple", "feature_expression"}
        )
        if rule["model"] not in allowed:
            _raise(
                f"$.strategy_spec.exit.{field_name}.model",
                "unsupported model",
                actual=rule["model"],
            )
        if rule["model"] == "feature_expression":
            value_type = _infer_value(
                rule["value"],
                parameters,
                features,
                operators,
                f"$.strategy_spec.exit.{field_name}.value",
            )
            if value_type != "decimal":
                _raise(f"$.strategy_spec.exit.{field_name}.value", "must infer decimal")
        else:
            value = _canonical_decimal(
                rule["value"], f"$.strategy_spec.exit.{field_name}.value", positive=True
            )
            if rule["model"] == "fixed_percent" and value >= 1:
                _raise(
                    f"$.strategy_spec.exit.{field_name}.value",
                    "fixed_percent must be less than 1",
                )
    if exit_spec["time_exit_bars"] is not None:
        _safe_int(
            exit_spec["time_exit_bars"],
            "$.strategy_spec.exit.time_exit_bars",
            minimum=1,
        )
    if exit_spec["signal_exit"] is not None:
        _validate_condition(
            exit_spec["signal_exit"],
            parameters,
            features,
            operators,
            "$.strategy_spec.exit.signal_exit",
        )

    risk = _exact(
        obj["risk"],
        {"position_sizing", "max_open_positions", "allow_pyramiding", "leverage"},
        "$.strategy_spec.risk",
    )
    sizing = _exact(
        risk["position_sizing"],
        {"model", "value"},
        "$.strategy_spec.risk.position_sizing",
    )
    if sizing["model"] not in {"fixed_notional", "fixed_margin", "risk_fraction"}:
        _raise(
            "$.strategy_spec.risk.position_sizing.model",
            "unsupported model",
            actual=sizing["model"],
        )
    sizing_value = _canonical_decimal(
        sizing["value"], "$.strategy_spec.risk.position_sizing.value", positive=True
    )
    if sizing["model"] == "risk_fraction" and sizing_value > 1:
        _raise(
            "$.strategy_spec.risk.position_sizing.value", "risk_fraction must be <= 1"
        )
    if risk["max_open_positions"] != 1 or risk["allow_pyramiding"] is not False:
        _raise(
            "$.strategy_spec.risk", "v2 requires one open position and no pyramiding"
        )
    leverage = _canonical_decimal(
        risk["leverage"], "$.strategy_spec.risk.leverage", positive=True
    )
    if market["market_type"] == "spot" and leverage != 1:
        _raise("$.strategy_spec.risk.leverage", "spot leverage must equal 1")

    execution = _exact(
        obj["execution"],
        {
            "decision_clock",
            "signal_effective_at",
            "intrabar_priority",
            "position_mode",
            "cost_model",
            "missing_data_policy",
            "kernel_api_version",
        },
        "$.strategy_spec.execution",
    )
    if (
        execution["decision_clock"] != "closed_bar"
        or execution["signal_effective_at"] != "next_bar_open"
    ):
        _raise("$.strategy_spec.execution", "unsupported decision clock/effective time")
    priority = execution["intrabar_priority"]
    allowed_priority = {"stop_loss", "take_profit", "time_exit", "signal_exit"}
    if (
        not isinstance(priority, list)
        or len(priority) != 4
        or set(priority) != allowed_priority
    ):
        _raise(
            "$.strategy_spec.execution.intrabar_priority",
            "must be an exact permutation of four exits",
        )
    if (
        execution["position_mode"] != "one_way"
        or execution["missing_data_policy"] != "fail"
    ):
        _raise("$.strategy_spec.execution", "unsupported position/missing-data policy")
    if execution["kernel_api_version"] != "1":
        _raise("$.strategy_spec.execution.kernel_api_version", "must equal 1")
    cost = _exact(
        execution["cost_model"],
        {"schema", "fee_bps", "slippage_bps", "funding"},
        "$.strategy_spec.execution.cost_model",
    )
    if cost["schema"] != "cutie.execution_cost.v1":
        _raise("$.strategy_spec.execution.cost_model.schema", "unsupported cost schema")
    _canonical_decimal(
        cost["fee_bps"],
        "$.strategy_spec.execution.cost_model.fee_bps",
        nonnegative=True,
    )
    _canonical_decimal(
        cost["slippage_bps"],
        "$.strategy_spec.execution.cost_model.slippage_bps",
        nonnegative=True,
    )
    if cost["funding"] not in {"excluded", "included"}:
        _raise(
            "$.strategy_spec.execution.cost_model.funding",
            "must be excluded or included",
        )

    feature_requirements = [
        {
            "primitive": feature["primitive"],
            "version": feature["primitive_version"],
            "source_stream": feature["source_stream"],
            "interval": feature["interval"],
            "value_kind": feature["value_kind"],
            "output_type": feature["output_type"],
        }
        for feature in obj["features"]
    ]
    return (
        parameters,
        features,
        {
            "operators": _sort_objects(
                operators, ("node", "op", "version", "arg_types", "return_type")
            ),
            "features": _sort_objects(
                feature_requirements,
                (
                    "primitive",
                    "version",
                    "source_stream",
                    "interval",
                    "value_kind",
                    "output_type",
                ),
            ),
        },
    )


def _validate_sources(sources: Any) -> list[dict[str, Any]]:
    if not isinstance(sources, list) or not 1 <= len(sources) <= 100:
        _raise("$.artifact_manifest.source_materials", "must contain 1..100 items")
    seen: set[tuple[str, str]] = set()
    for index, source in enumerate(sources):
        path = f"$.artifact_manifest.source_materials[{index}]"
        obj = _exact(source, _SOURCE_KEYS, path)
        source_id = obj["source_artifact_id"]
        if (
            not isinstance(source_id, str)
            or not source_id.isascii()
            or not source_id.isdigit()
            or source_id.startswith("0")
        ):
            _raise(
                f"{path}.source_artifact_id", "must be a canonical positive decimal ID"
            )
        if obj["role"] != "strategy_definition":
            _raise(f"{path}.role", "unsupported source role", actual=obj["role"])
        for field_name in ("source_sha256", "content_sha256", "ingestion_signature"):
            if (
                not isinstance(obj[field_name], str)
                or _HASH_RE.fullmatch(obj[field_name]) is None
            ):
                _raise(f"{path}.{field_name}", "must be 64 lowercase hex")
        for field_name in (
            "parser_version",
            "ingestion_schema",
            "ingestion_signature_version",
            "ingestion_key_id",
        ):
            if not isinstance(obj[field_name], str) or not obj[field_name]:
                _raise(f"{path}.{field_name}", "must be a non-empty string")
        identity = (source_id, obj["role"])
        if identity in seen:
            _raise(path, "duplicate source identity")
        seen.add(identity)
    expected = sorted(
        sources,
        key=lambda item: (
            item["role"].encode("utf-16-be"),
            len(item["source_artifact_id"]),
            item["source_artifact_id"],
        ),
    )
    if sources != expected:
        _raise(
            "$.artifact_manifest.source_materials",
            "must be sorted by role and decimal ID",
        )
    return sources


def _validate_data_requirements(
    spec: dict[str, Any], requirements: Any
) -> list[dict[str, Any]]:
    if not isinstance(requirements, list) or not 1 <= len(requirements) <= 100:
        _raise("$.artifact_manifest.data_requirements", "must contain 1..100 items")
    stream_ids: list[str] = []
    primary_count = 0
    for index, requirement in enumerate(requirements):
        path = f"$.artifact_manifest.data_requirements[{index}]"
        obj = _exact(requirement, _DATA_REQUIREMENT_KEYS, path)
        for field_name in ("stream_id", "provider", "storage_source", "exchange"):
            if (
                not isinstance(obj[field_name], str)
                or _SCHEMA_KEY_RE.fullmatch(obj[field_name]) is None
            ):
                _raise(f"{path}.{field_name}", "invalid canonical key")
        stream_ids.append(obj["stream_id"])
        if obj["kind"] not in {"kline", "feature"}:
            _raise(f"{path}.kind", "must be kline or feature")
        valid_role = (
            obj["kind"] == "kline"
            and obj["execution_role"] in {"primary_execution_kline", "auxiliary_kline"}
        ) or (obj["kind"] == "feature" and obj["execution_role"] == "feature_input")
        if not valid_role:
            _raise(f"{path}.execution_role", "does not match kind")
        if obj["market"] not in {"spot", "futures"}:
            _raise(f"{path}.market", "must be spot or futures")
        if (
            not isinstance(obj["interval"], str)
            or _INTERVAL_RE.fullmatch(obj["interval"]) is None
        ):
            _raise(f"{path}.interval", "invalid interval")
        if obj["symbols"] != spec["market"]["symbols"]:
            _raise(f"{path}.symbols", "must equal StrategySpec symbol domain")
        _safe_int(obj["warmup_bars"], f"{path}.warmup_bars")
        _safe_int(
            obj["max_freshness_seconds"], f"{path}.max_freshness_seconds", minimum=1
        )
        if obj["gap_policy"] != "none":
            _raise(f"{path}.gap_policy", "v1 only permits none")
        transforms = _utf16_sorted_unique(
            obj["allowed_transforms"], f"{path}.allowed_transforms"
        )
        if any(
            item
            not in {
                "combine_first.v1",
                "ffill_after_close.v1",
                "flow_dilution_shifted.v1",
                "ohlcv_resample.v1",
            }
            for item in transforms
        ):
            _raise(f"{path}.allowed_transforms", "unknown transform")
        if obj["execution_role"] == "primary_execution_kline":
            primary_count += 1
            market = spec["market"]
            if (obj["exchange"], obj["market"], obj["interval"]) != (
                market["exchange"],
                market["market_type"],
                market["timeframe"],
            ):
                _raise(path, "primary market identity differs from StrategySpec")
            if (
                not isinstance(obj["result_source"], str)
                or _SCHEMA_KEY_RE.fullmatch(obj["result_source"]) is None
            ):
                _raise(
                    f"{path}.result_source",
                    "primary requires a canonical result source",
                )
        elif obj["result_source"] is not None:
            _raise(
                f"{path}.result_source", "auxiliary/feature result_source must be null"
            )
    expected = sorted(set(stream_ids), key=lambda value: value.encode("utf-16-be"))
    if stream_ids != expected:
        _raise(
            "$.artifact_manifest.data_requirements",
            "must be sorted and duplicate-free by stream_id",
        )
    if primary_count != 1:
        _raise(
            "$.artifact_manifest.data_requirements",
            "requires exactly one primary K-line",
        )
    return requirements


def _matched_requirement(
    spec: dict[str, Any],
    requirements: list[dict[str, Any]],
    feature: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """The physical data_requirement backing `feature`'s source_stream/
    interval: the primary K-line itself for a kline.primary passthrough
    feature (interval == primary timeframe, §5.5 "不产生新 stream"), or the
    matching feature-kind requirement otherwise (same convention build_frames
    uses: stream_id prefixed by source_stream + ".", exact interval match)."""
    if (
        feature["source_stream"].startswith(_KLINE_PRIMARY_PREFIX)
        and feature["interval"] == spec["market"]["timeframe"]
    ):
        return next(
            (
                item
                for item in requirements
                if item["execution_role"] == "primary_execution_kline"
            ),
            None,
        )
    return next(
        (
            item
            for item in requirements
            if item["stream_id"].startswith(feature["source_stream"] + ".")
            and item["interval"] == feature["interval"]
        ),
        None,
    )


def _validate_rsi_warmup(
    spec: dict[str, Any], requirements: list[dict[str, Any]]
) -> None:
    """SPEC §5.5 rsi_wilder: "warmup 必须 ≥10×period 个源 bar（种子效应收敛要求，
    compile 期校验）" — the seed-convergence requirement is on the *source*
    bars feeding the primitive, i.e. the physical requirement's own
    warmup_bars (in that requirement's own interval units), not a separate
    per-feature field.
    """
    for index, feature in enumerate(spec["features"]):
        if feature["primitive"] != "rsi_wilder":
            continue
        requirement = _matched_requirement(spec, requirements, feature)
        if requirement is None:
            continue
        period = feature["params"]["period"]
        if requirement["warmup_bars"] < 10 * period:
            _raise(
                f"$.strategy_spec.features[{index}].params.period",
                "rsi_wilder requires warmup_bars >= 10x period for seed "
                "convergence",
                required=10 * period,
                actual=requirement["warmup_bars"],
            )


def _validate_kline_primary_requirements(
    spec: dict[str, Any], requirements: list[dict[str, Any]]
) -> None:
    """SPEC §5.5 binds two exact requirement shapes for kline.primary
    features that must be rejected at compile, not left for the Provider's
    runtime predicate to misclassify:

    - a passthrough feature (interval == primary timeframe) must never have
      a data_requirement of its own ("不产生新 stream，不需要 transform"); the
      Provider's fetch-time predicate (matching by source_stream prefix and
      interval, same convention as here) cannot distinguish an illegally
      declared one from a legitimate coarse requirement once it exists, so an
      illegal one must never reach execution. Its primitive is also gated
      here: a passthrough feature reads straight off the primary K-line
      series (``build_frames``' ``_primary_field_series``), which has no
      lookback beyond whatever the primary requirement itself fetched — the
      array's very first row can never have prior rows *within that same
      array* no matter how large ``warmup_bars`` is set. A window primitive
      needing more than the anchor bar itself, or ``rsi_wilder`` (whose seed
      is always ``None`` for its first ``period`` timestamps, see
      ``_rsi_wilder_series``), therefore compiles green but is guaranteed to
      fail closed on the very first frame every single run — a config that
      must never reach execution.
    - a coarse feature (interval != primary timeframe)'s matched requirement
      must declare ``ohlcv_resample.v1`` in its own ``allowed_transforms``:
      §4.2 binds transform selection into the artifact hash via spec_hash,
      so a requirement missing the declaration must fail closed rather than
      have the Provider resample it anyway at runtime. Its data source
      identity (provider/storage_source/exchange/market) must also match the
      primary requirement's own — a coarse kline.primary requirement is never
      independently fetched, so a mismatched identity would be a label the
      Provider's resample-from-primary execution can never actually honor.
      Its warmup_bars must cover the primitive's own lookback (one whole
      bucket more than a same-interval feature needs: the as-of anchor is
      always the bucket *before* the decision frame's own, which cannot be
      complete yet), or the first decision frame in range compiles green but
      always fails closed for the identical "no lookback" reason.
    """
    market_timeframe = spec["market"]["timeframe"]
    primary_requirement = next(
        item for item in requirements if item["execution_role"] == "primary_execution_kline"
    )
    for index, feature in enumerate(spec["features"]):
        source_stream = feature["source_stream"]
        if not source_stream.startswith(_KLINE_PRIMARY_PREFIX):
            continue
        matching = [
            item
            for item in requirements
            if item["kind"] == "feature"
            and item["stream_id"].startswith(source_stream + ".")
            and item["interval"] == feature["interval"]
        ]
        if feature["interval"] == market_timeframe:
            if matching:
                _raise(
                    f"$.artifact_manifest.data_requirements[{matching[0]['stream_id']}]",
                    "kline.primary passthrough feature must not declare a "
                    "separate data requirement",
                    actual=matching[0]["stream_id"],
                )
            primitive = feature["primitive"]
            params = feature["params"]
            if primitive == "rsi_wilder":
                _raise(
                    f"$.strategy_spec.features[{index}].primitive",
                    "kline.primary passthrough feature cannot use rsi_wilder: "
                    "its seed is always unresolved for the array's first "
                    "fetched row, so every run fails closed regardless of "
                    "warmup_bars",
                    code=ERR_SPEC_UNSUPPORTED,
                    actual=primitive,
                )
            if (
                primitive in {"rolling_sum", "rolling_extreme"}
                and params["window_bars"] != 1
            ):
                _raise(
                    f"$.strategy_spec.features[{index}].params.window_bars",
                    "kline.primary passthrough feature only supports "
                    "window_bars=1: a wider window has no prior bar before "
                    "the array's first fetched row and always fails closed "
                    "at runtime",
                    code=ERR_SPEC_UNSUPPORTED,
                    required=1,
                    actual=params["window_bars"],
                )
            if primitive == "rolling_quantile" and params["min_periods"] != 1:
                _raise(
                    f"$.strategy_spec.features[{index}].params.min_periods",
                    "kline.primary passthrough feature's rolling_quantile "
                    "only supports min_periods=1: a higher floor can never "
                    "be satisfied by the array's first fetched row and "
                    "always fails closed at runtime",
                    code=ERR_SPEC_UNSUPPORTED,
                    required=1,
                    actual=params["min_periods"],
                )
            continue
        for requirement in matching:
            if "ohlcv_resample.v1" not in requirement["allowed_transforms"]:
                _raise(
                    f"$.artifact_manifest.data_requirements[{requirement['stream_id']}]"
                    ".allowed_transforms",
                    "coarse kline.primary requirement must declare "
                    "ohlcv_resample.v1",
                    required="ohlcv_resample.v1",
                    actual=requirement["allowed_transforms"],
                )
            for field_name in ("provider", "storage_source", "exchange", "market"):
                if requirement[field_name] != primary_requirement[field_name]:
                    _raise(
                        f"$.artifact_manifest.data_requirements[{requirement['stream_id']}]"
                        f".{field_name}",
                        "coarse kline.primary requirement must match the "
                        "primary K-line's own data source identity",
                        required=primary_requirement[field_name],
                        actual=requirement[field_name],
                    )
            primitive = feature["primitive"]
            params = feature["params"]
            if primitive in {"rolling_sum", "rolling_extreme"}:
                window_bars = params["window_bars"]
                if requirement["warmup_bars"] < window_bars:
                    _raise(
                        f"$.artifact_manifest.data_requirements[{requirement['stream_id']}]"
                        ".warmup_bars",
                        "coarse kline.primary requirement's warmup_bars must "
                        "be at least the primitive's window_bars",
                        required=window_bars,
                        actual=requirement["warmup_bars"],
                    )
            elif primitive == "rolling_quantile":
                min_periods = params["min_periods"]
                if requirement["warmup_bars"] < min_periods:
                    _raise(
                        f"$.artifact_manifest.data_requirements[{requirement['stream_id']}]"
                        ".warmup_bars",
                        "coarse kline.primary requirement's warmup_bars must "
                        "be at least the rolling_quantile primitive's "
                        "min_periods",
                        required=min_periods,
                        actual=requirement["warmup_bars"],
                    )


def _derived_requirements(
    spec: dict[str, Any],
    static: dict[str, list[dict[str, Any]]],
    data_requirements: list[dict[str, Any]],
) -> dict[str, Any]:
    data_sources = [
        {
            "provider": item["provider"],
            "storage_source": item["storage_source"],
            "kind": item["kind"],
            "market": item["market"],
            "result_source": item["result_source"],
        }
        for item in data_requirements
    ]
    return {
        "operators": static["operators"],
        "features": static["features"],
        "data_sources": _sort_objects(
            data_sources,
            ("provider", "storage_source", "kind", "market", "result_source"),
        ),
        "cost_models": [spec["execution"]["cost_model"]["schema"]],
        "data_transforms": sorted(
            {
                transform
                for item in data_requirements
                for transform in item["allowed_transforms"]
            },
            key=lambda value: value.encode("utf-16-be"),
        ),
        "result_schemas": [RESULT_SCHEMA],
        "coverage_schemas": ["cutie.strategy_coverage_manifest.v1"],
        "trace_schemas": ["cutie.strategy_execution_trace.v1"],
        "evidence_schemas": ["cutie.strategy_execution_evidence.v1"],
    }


def _validate_manifest(
    spec: dict[str, Any], manifest: Any, static: dict[str, list[dict[str, Any]]]
) -> tuple[str, str, str]:
    obj = _exact(manifest, _MANIFEST_KEYS, "$.artifact_manifest")
    if (
        obj["schema"] != ARTIFACT_MANIFEST_SCHEMA
        or obj["artifact_kind"] != "declarative_strategy"
    ):
        _raise("$.artifact_manifest", "unsupported manifest schema/kind")
    if obj["strategy_spec_schema"] != STRATEGY_SPEC_SCHEMA:
        _raise(
            "$.artifact_manifest.strategy_spec_schema", "must reference StrategySpec v2"
        )
    spec_hash = canonical_json_sha256(normalize_numbers_for_hash(spec))
    if obj["spec_hash"] != spec_hash:
        _raise(
            "$.artifact_manifest.spec_hash",
            "does not match Provider recomputation",
            required=spec_hash,
            actual=obj["spec_hash"],
        )
    if obj["compiler"] != {"id": "cutie.strategy_spec.compiler", "version": "1"}:
        _raise("$.artifact_manifest.compiler", "must use frozen compiler identity")
    if obj["kernel_contract"] != {
        "api_version": "1",
        "required_modes": ["historical_replay", "paper_tick"],
    }:
        _raise("$.artifact_manifest.kernel_contract", "must use frozen kernel contract")
    if obj["provenance_policy"] != "exact_set":
        _raise("$.artifact_manifest.provenance_policy", "must equal exact_set")
    _validate_sources(obj["source_materials"])
    requirements = _validate_data_requirements(spec, obj["data_requirements"])
    _validate_rsi_warmup(spec, requirements)
    _validate_kline_primary_requirements(spec, requirements)
    _exact(
        obj["capability_requirements"],
        _CAPABILITY_REQUIREMENT_KEYS,
        "$.artifact_manifest.capability_requirements",
    )
    expected = _derived_requirements(spec, static, requirements)
    if obj["capability_requirements"] != expected:
        _raise(
            "$.artifact_manifest.capability_requirements",
            "does not match the Provider-derived projection",
            required=expected,
            actual=obj["capability_requirements"],
        )
    manifest_hash = canonical_json_sha256(normalize_numbers_for_hash(obj))
    artifact_hash = canonical_json_sha256(
        {
            "schema": ARTIFACT_DIGEST_SCHEMA,
            "spec_hash": spec_hash,
            "manifest_hash": manifest_hash,
        }
    )
    return spec_hash, manifest_hash, artifact_hash


def capability_payload(provider_revision: str) -> dict[str, Any]:
    """Return the Provider-local 62-2a capability exact-set.

    ``binance_futures`` is the 62-1 result source even when the central adapter
    obtains the files from Binance Vision.
    """
    if _REVISION_RE.fullmatch(provider_revision) is None:
        raise ValueError("provider_revision must be 7..64 lowercase hex")
    operators = [
        {
            "node": "arithmetic",
            "op": "add",
            "version": "1",
            "signatures": [
                {"arg_types": ["decimal", "decimal"], "return_type": "decimal"},
                {"arg_types": ["integer", "integer"], "return_type": "integer"},
            ],
        },
        {
            "node": "compare",
            "op": "gt",
            "version": "1",
            "signatures": [
                {"arg_types": ["decimal", "decimal"], "return_type": "boolean"},
                {"arg_types": ["integer", "integer"], "return_type": "boolean"},
            ],
        },
        {
            "node": "cross",
            "op": "crosses_above",
            "version": "1",
            "signatures": [
                {"arg_types": ["decimal", "decimal"], "return_type": "boolean"},
            ],
        },
    ]
    return {
        "schema": CAPABILITY_SCHEMA,
        "provider_revision": provider_revision,
        "spec_schemas": [STRATEGY_SPEC_SCHEMA],
        "kernel_api_versions": ["1"],
        "execution_modes": ["historical_replay", "paper_tick"],
        "operators": operators,
        "feature_primitives": [
            {
                "primitive": "rolling_extreme",
                "version": "1",
                "source_streams": [
                    "coinglass.futures_cvd",
                    "kline.primary.close",
                    "kline.primary.high",
                    "kline.primary.low",
                    "kline.primary.open",
                ],
                "intervals": ["1d", "1h", "4h"],
                "value_kinds": ["flow", "price"],
                "output_type": "decimal",
            },
            {
                "primitive": "rolling_quantile",
                "version": "1",
                "source_streams": [
                    "coinglass.futures_cvd",
                    "kline.primary.close",
                    "kline.primary.high",
                    "kline.primary.low",
                    "kline.primary.open",
                ],
                "intervals": ["1d", "1h", "4h"],
                "value_kinds": ["flow", "price"],
                "output_type": "decimal",
            },
            {
                "primitive": "rolling_sum",
                "version": "1",
                "source_streams": ["coinglass.futures_cvd"],
                "intervals": ["1d", "1h"],
                "value_kinds": ["flow"],
                "output_type": "decimal",
            },
            {
                "primitive": "rsi_wilder",
                "version": "1",
                "source_streams": [
                    "coinglass.futures_cvd",
                    "kline.primary.close",
                    "kline.primary.high",
                    "kline.primary.low",
                    "kline.primary.open",
                ],
                "intervals": ["1d", "1h", "4h"],
                "value_kinds": ["flow", "price"],
                "output_type": "decimal",
            },
        ],
        "data_sources": [
            {
                "provider": "binance",
                "storage_source": "central_klines",
                "kinds": ["feature", "kline"],
                "markets": ["futures"],
                "result_sources": ["binance_futures"],
            },
            {
                "provider": "coinglass",
                "storage_source": "market_metrics_history",
                "kinds": ["feature"],
                "markets": ["futures"],
                "result_sources": [],
            },
        ],
        "cost_models": ["cutie.execution_cost.v1"],
        "data_transforms": ["ohlcv_resample.v1"],
        "result_schemas": [RESULT_SCHEMA],
        "coverage_schemas": ["cutie.strategy_coverage_manifest.v1"],
        "trace_schemas": ["cutie.strategy_execution_trace.v1"],
        "evidence_schemas": ["cutie.strategy_execution_evidence.v1"],
    }


def capability_hash(capability: dict[str, Any]) -> str:
    return canonical_json_sha256(capability)


def _capability_exact(value: Any, keys: set[str], path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _raise(
            path,
            "must be an object",
            code=ERR_CAPABILITY_MISMATCH,
            required=sorted(keys),
            actual=type(value).__name__,
        )
    actual = set(value)
    if actual != keys:
        _raise(
            path,
            "exact keys required",
            code=ERR_CAPABILITY_MISMATCH,
            required=sorted(keys),
            actual={"missing": sorted(keys - actual), "unknown": sorted(actual - keys)},
        )
    return value


def _capability_string_array(
    value: Any, path: str, *, nonempty: bool = False
) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) > 256
        or any(
            not isinstance(item, str)
            or len(item.encode("utf-8")) > 256
            or _CAPABILITY_VALUE_RE.fullmatch(item) is None
            for item in value
        )
    ):
        _raise(
            path,
            "must be an at-most-256 canonical string array",
            code=ERR_CAPABILITY_MISMATCH,
        )
    expected = sorted(set(value), key=lambda item: item.encode("utf-16-be"))
    if value != expected or (nonempty and not value):
        _raise(
            path,
            "must be UTF-16 sorted and duplicate-free",
            code=ERR_CAPABILITY_MISMATCH,
            required=expected,
            actual=value,
        )
    return value


def _capability_arg_types(value: Any, path: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= 256
        or any(
            not isinstance(item, str) or _CAPABILITY_VALUE_RE.fullmatch(item) is None
            for item in value
        )
    ):
        _raise(
            path,
            "must contain 1..256 canonical type names",
            code=ERR_CAPABILITY_MISMATCH,
        )
    return value


def _validate_capability_signature(
    node: str,
    op: str,
    arg_types: list[str],
    return_type: str,
    path: str,
) -> None:
    allowed_ops = {
        "arithmetic": {"add", "sub", "mul", "div", "min", "max", "abs"},
        "compare": {"gt", "gte", "lt", "lte", "eq", "neq"},
        "cross": {"crosses_above", "crosses_below"},
    }
    if op not in allowed_ops[node]:
        _raise(path, "unknown operator", code=ERR_CAPABILITY_MISMATCH)
    valid_arity = (
        (node == "arithmetic" and op == "abs" and len(arg_types) == 1)
        or (
            node == "arithmetic"
            and op in {"add", "mul", "min", "max"}
            and len(arg_types) >= 2
        )
        or ((node != "arithmetic" or op in {"sub", "div"}) and len(arg_types) == 2)
    )
    if not valid_arity:
        _raise(path, "invalid operator arity", code=ERR_CAPABILITY_MISMATCH)
    if len(set(arg_types)) != 1:
        _raise(
            path,
            "implicit type conversion is forbidden",
            code=ERR_CAPABILITY_MISMATCH,
        )
    arg_type = arg_types[0]
    if arg_type not in {
        "integer",
        "decimal",
        "boolean",
        "string",
    } or return_type not in {
        "integer",
        "decimal",
        "boolean",
        "string",
    }:
        _raise(path, "unknown static type", code=ERR_CAPABILITY_MISMATCH)
    if node == "arithmetic":
        if arg_type not in {"integer", "decimal"} or return_type != arg_type:
            _raise(
                path,
                "arithmetic signature must preserve one numeric type",
                code=ERR_CAPABILITY_MISMATCH,
            )
        if op == "div" and arg_type != "decimal":
            _raise(
                path,
                "div only accepts decimal",
                code=ERR_CAPABILITY_MISMATCH,
            )
    else:
        if return_type != "boolean":
            _raise(
                path,
                "condition operator must return boolean",
                code=ERR_CAPABILITY_MISMATCH,
            )
        if (node == "cross" or op in {"gt", "gte", "lt", "lte"}) and arg_type not in {
            "integer",
            "decimal",
        }:
            _raise(
                path,
                "ordered condition requires one numeric type",
                code=ERR_CAPABILITY_MISMATCH,
            )


def _validate_capability(capability: Any) -> None:
    obj = _capability_exact(capability, _CAPABILITY_KEYS, "$.capability")
    if obj["schema"] != CAPABILITY_SCHEMA:
        _raise(
            "$.capability.schema", "unsupported schema", code=ERR_CAPABILITY_MISMATCH
        )
    if (
        not isinstance(obj["provider_revision"], str)
        or _REVISION_RE.fullmatch(obj["provider_revision"]) is None
    ):
        _raise(
            "$.capability.provider_revision",
            "invalid immutable revision",
            code=ERR_CAPABILITY_MISMATCH,
        )
    if len(canonical_json(obj).encode("utf-8")) > 16 * 1024:
        _raise("$.capability", "exceeds 16 KiB", code=ERR_CAPABILITY_MISMATCH)

    for key in (
        "spec_schemas",
        "kernel_api_versions",
        "execution_modes",
        "cost_models",
        "data_transforms",
        "result_schemas",
        "coverage_schemas",
        "trace_schemas",
        "evidence_schemas",
    ):
        _capability_string_array(
            obj[key],
            f"$.capability.{key}",
            nonempty=key not in {"cost_models", "data_transforms"},
        )

    operators = obj["operators"]
    if not isinstance(operators, list) or len(operators) > 256:
        _raise(
            "$.capability.operators",
            "must contain at most 256 operators",
            code=ERR_CAPABILITY_MISMATCH,
        )
    operator_keys: list[tuple[str, str, str]] = []
    for index, value in enumerate(operators):
        path = f"$.capability.operators[{index}]"
        operator = _capability_exact(
            value, {"node", "op", "version", "signatures"}, path
        )
        if operator["node"] not in {"arithmetic", "compare", "cross"}:
            _raise(
                f"{path}.node",
                "unknown operator node",
                code=ERR_CAPABILITY_MISMATCH,
            )
        for key in ("op", "version"):
            if (
                not isinstance(operator[key], str)
                or _CAPABILITY_VALUE_RE.fullmatch(operator[key]) is None
            ):
                _raise(
                    f"{path}.{key}",
                    "invalid canonical key",
                    code=ERR_CAPABILITY_MISMATCH,
                )
        signatures = operator["signatures"]
        if not isinstance(signatures, list) or not 1 <= len(signatures) <= 256:
            _raise(
                f"{path}.signatures",
                "must contain 1..256 signatures",
                code=ERR_CAPABILITY_MISMATCH,
            )
        signature_keys: list[tuple[tuple[str, ...], str]] = []
        for signature_index, value in enumerate(signatures):
            signature_path = f"{path}.signatures[{signature_index}]"
            signature = _capability_exact(
                value, {"arg_types", "return_type"}, signature_path
            )
            arg_types = _capability_arg_types(
                signature["arg_types"], f"{signature_path}.arg_types"
            )
            if (
                not isinstance(signature["return_type"], str)
                or _CAPABILITY_VALUE_RE.fullmatch(signature["return_type"]) is None
            ):
                _raise(
                    f"{signature_path}.return_type",
                    "invalid return type",
                    code=ERR_CAPABILITY_MISMATCH,
                )
            _validate_capability_signature(
                operator["node"],
                operator["op"],
                arg_types,
                signature["return_type"],
                signature_path,
            )
            signature_keys.append((tuple(arg_types), signature["return_type"]))
        if signature_keys != sorted(set(signature_keys)):
            _raise(
                f"{path}.signatures",
                "must be sorted and duplicate-free",
                code=ERR_CAPABILITY_MISMATCH,
            )
        operator_keys.append((operator["node"], operator["op"], operator["version"]))
    if operator_keys != sorted(set(operator_keys)):
        _raise(
            "$.capability.operators",
            "must be sorted and duplicate-free",
            code=ERR_CAPABILITY_MISMATCH,
        )

    features = obj["feature_primitives"]
    if not isinstance(features, list) or len(features) > 256:
        _raise(
            "$.capability.feature_primitives",
            "must contain at most 256 entries",
            code=ERR_CAPABILITY_MISMATCH,
        )
    feature_keys: list[tuple[str, str]] = []
    for index, value in enumerate(features):
        path = f"$.capability.feature_primitives[{index}]"
        feature = _capability_exact(
            value,
            {
                "primitive",
                "version",
                "source_streams",
                "intervals",
                "value_kinds",
                "output_type",
            },
            path,
        )
        for key in ("primitive", "version", "output_type"):
            if (
                not isinstance(feature[key], str)
                or _CAPABILITY_VALUE_RE.fullmatch(feature[key]) is None
            ):
                _raise(
                    f"{path}.{key}",
                    "invalid canonical key",
                    code=ERR_CAPABILITY_MISMATCH,
                )
        for key in ("source_streams", "intervals", "value_kinds"):
            _capability_string_array(feature[key], f"{path}.{key}", nonempty=True)
        feature_keys.append((feature["primitive"], feature["version"]))
    if feature_keys != sorted(set(feature_keys)):
        _raise(
            "$.capability.feature_primitives",
            "must be sorted and duplicate-free",
            code=ERR_CAPABILITY_MISMATCH,
        )

    data_sources = obj["data_sources"]
    if not isinstance(data_sources, list) or len(data_sources) > 256:
        _raise(
            "$.capability.data_sources",
            "must contain at most 256 entries",
            code=ERR_CAPABILITY_MISMATCH,
        )
    data_source_keys: list[tuple[str, str]] = []
    for index, value in enumerate(data_sources):
        path = f"$.capability.data_sources[{index}]"
        source = _capability_exact(
            value,
            {"provider", "storage_source", "kinds", "markets", "result_sources"},
            path,
        )
        for key in ("provider", "storage_source"):
            if (
                not isinstance(source[key], str)
                or _CAPABILITY_VALUE_RE.fullmatch(source[key]) is None
            ):
                _raise(
                    f"{path}.{key}",
                    "invalid canonical key",
                    code=ERR_CAPABILITY_MISMATCH,
                )
        for key in ("kinds", "markets"):
            _capability_string_array(source[key], f"{path}.{key}", nonempty=True)
        _capability_string_array(source["result_sources"], f"{path}.result_sources")
        data_source_keys.append((source["provider"], source["storage_source"]))
    if data_source_keys != sorted(set(data_source_keys)):
        _raise(
            "$.capability.data_sources",
            "must be sorted and duplicate-free",
            code=ERR_CAPABILITY_MISMATCH,
        )


def _check_capability(
    plan_requirements: dict[str, Any], capability: dict[str, Any]
) -> None:
    available_operators = {
        (
            item["node"],
            item["op"],
            item["version"],
            tuple(signature["arg_types"]),
            signature["return_type"],
        )
        for item in capability["operators"]
        for signature in item["signatures"]
    }
    for index, required in enumerate(plan_requirements["operators"]):
        identity = (
            required["node"],
            required["op"],
            required["version"],
            tuple(required["arg_types"]),
            required["return_type"],
        )
        if identity not in available_operators:
            _raise(
                f"$.artifact_manifest.capability_requirements.operators[{index}]",
                "operator signature is not supported",
                code=ERR_SPEC_UNSUPPORTED,
                required=required,
                actual=capability["operators"],
            )
    for index, required in enumerate(plan_requirements["features"]):
        matches = [
            item
            for item in capability["feature_primitives"]
            if item["primitive"] == required["primitive"]
            and item["version"] == required["version"]
            and required["source_stream"] in item["source_streams"]
            and required["interval"] in item["intervals"]
            and required["value_kind"] in item["value_kinds"]
            and item["output_type"] == required["output_type"]
        ]
        if not matches:
            _raise(
                f"$.artifact_manifest.capability_requirements.features[{index}]",
                "feature primitive is not supported",
                code=ERR_SPEC_UNSUPPORTED,
                required=required,
                actual=capability["feature_primitives"],
            )
    for index, required in enumerate(plan_requirements["data_sources"]):
        matches = [
            item
            for item in capability["data_sources"]
            if item["provider"] == required["provider"]
            and item["storage_source"] == required["storage_source"]
            and required["kind"] in item["kinds"]
            and required["market"] in item["markets"]
            and (
                required["result_source"] is None
                or required["result_source"] in item["result_sources"]
            )
        ]
        if not matches:
            _raise(
                f"$.artifact_manifest.capability_requirements.data_sources[{index}]",
                "data source is not supported",
                code=ERR_SPEC_UNSUPPORTED,
                required=required,
                actual=capability["data_sources"],
            )
    mapping = {
        "cost_models": "cost_models",
        "data_transforms": "data_transforms",
        "result_schemas": "result_schemas",
        "coverage_schemas": "coverage_schemas",
        "trace_schemas": "trace_schemas",
        "evidence_schemas": "evidence_schemas",
    }
    for required_key, capability_key in mapping.items():
        for value in plan_requirements[required_key]:
            if value not in capability[capability_key]:
                _raise(
                    f"$.artifact_manifest.capability_requirements.{required_key}",
                    "requirement is not supported",
                    code=ERR_SPEC_UNSUPPORTED,
                    required=value,
                    actual=capability[capability_key],
                )


def compile_strategy(
    strategy_spec: dict[str, Any],
    artifact_manifest: dict[str, Any],
    capability: dict[str, Any],
) -> CompiledPlan:
    """Validate, type-check, bind hashes, and compare exact Provider capability."""
    try:
        canonical_json(strategy_spec)
        canonical_json(artifact_manifest)
        canonical_json(capability)
    except CanonicalJsonError as exc:
        raise StrategyContractError(ERR_SPEC_INVALID, "$", str(exc)) from exc
    parameters, features, static = _validate_spec(strategy_spec)
    spec_hash, manifest_hash, artifact_hash = _validate_manifest(
        strategy_spec, artifact_manifest, static
    )
    _validate_capability(capability)
    requirements = artifact_manifest["capability_requirements"]
    _check_capability(requirements, capability)
    if strategy_spec["execution"]["cost_model"]["funding"] == "included":
        _raise(
            "$.strategy_spec.execution.cost_model.funding",
            "funding=included requires a future result schema and immutable ledger",
            code=ERR_SPEC_UNSUPPORTED,
            required="excluded",
            actual="included",
        )
    return CompiledPlan(
        strategy_spec=copy.deepcopy(strategy_spec),
        artifact_manifest=copy.deepcopy(artifact_manifest),
        spec_hash=spec_hash,
        manifest_hash=manifest_hash,
        artifact_hash=artifact_hash,
        parameter_types=parameters,
        feature_types=features,
    )


def _decimal(value: Any, path: str) -> Decimal:
    try:
        if isinstance(value, Decimal):
            result = value
        elif isinstance(value, int) and not isinstance(value, bool):
            result = Decimal(value)
        elif isinstance(value, str):
            result = Decimal(value)
        else:
            raise InvalidOperation
        if not result.is_finite():
            raise InvalidOperation
        return result
    except (InvalidOperation, ValueError):
        raise KernelExecutionError(
            ERR_COVERAGE_INCOMPLETE, path, "missing or invalid decimal frame value"
        )


def kline_primary_bucket_required_start(
    start_at: int, warmup_bars: int, target_step: int
) -> int:
    """The bucket-aligned ``required_start`` a coarse ``kline.primary.*``
    requirement must declare (and a Provider fetch must honor), shared by
    the Provider's fetch range and the coverage completeness check so both
    always agree.

    A naive ``start_at - warmup_bars * target_step`` (correct for a
    same-interval requirement, whose own rows are exactly what a decision
    frame reads) is wrong here: the as-of anchor for the *first* decision
    frame is always the bucket that closed at-or-before the bucket
    *containing* ``start_at`` — that bucket has already elapsed even when
    ``start_at`` itself falls partway through it — never the bucket that
    merely starts at-or-after ``start_at``. Flooring to the bucket
    containing ``start_at`` first, then stepping back ``warmup_bars``
    buckets, is what actually has to be fetched/declared; offsetting by
    ``warmup_bars`` directly from an unaligned ``start_at`` can land inside
    a bucket that a decision frame needs in full, silently truncating it.
    """
    bucket_grid_start = (start_at // target_step) * target_step
    return max(0, bucket_grid_start - warmup_bars * target_step)


def kline_primary_bucket_required_end(end_at: int, target_step: int) -> int:
    """The bucket-aligned ``required_end`` a coarse ``kline.primary.*``
    requirement's completeness check must use, symmetric to
    ``kline_primary_bucket_required_start``. ``execution_params.end_at``
    (the raw decision-window boundary, at primary granularity) need not fall
    on a coarse bucket boundary; the last bucket a decision frame closing
    at-or-before ``end_at`` could ever as-of read is the one that closed
    at-or-before ``end_at``, i.e. floored to the bucket grid — comparing the
    completeness count against the unaligned ``end_at`` directly would
    demand a fractional/impossible bucket count whenever ``end_at`` falls
    mid-bucket, even though the coarse stream is genuinely complete for
    every decision frame that actually needs it.
    """
    return (end_at // target_step) * target_step


def ohlcv_resample(
    primary_rows: list[dict[str, Any]], primary_step: int, target_interval: str
) -> list[dict[str, Any]]:
    """SPEC §7.3 ``ohlcv_resample.v1``: deterministic, lossless fine-to-coarse
    OHLC aggregation. UTC-aligned half-open buckets (epoch 0 is a valid 00:00
    UTC boundary, so integer bucket-floor division is correct with no extra
    offset); a bucket is only emitted when every one of its constituent
    ``primary_step``-spaced bars is present in ``primary_rows`` — a bucket
    missing any input bar is silently dropped (never partially aggregated),
    which downstream becomes a feature-missing gap per §5.5, never a fabricated
    value. Output rows carry ``open_time`` = bucket open, matching the primary
    K-line row shape (``open/high/low/close``, no ``volume``: only price
    fields are ever consumed downstream via ``kline.primary.<field>``).
    """
    target_step = _interval_seconds(target_interval)
    if target_step <= 0 or target_step % primary_step != 0:
        _raise(
            "$.transform.ohlcv_resample.target_interval",
            "target interval must be a positive multiple of the primary interval",
            code=ERR_SPEC_UNSUPPORTED,
            required=target_interval,
        )
    bars_per_bucket = target_step // primary_step
    by_open_time = {row["open_time"]: row for row in primary_rows}
    bucket_members: dict[int, list[int]] = {}
    for open_time in by_open_time:
        bucket_open = (open_time // target_step) * target_step
        bucket_members.setdefault(bucket_open, []).append(open_time)
    output: list[dict[str, Any]] = []
    for bucket_open in sorted(bucket_members):
        expected = [bucket_open + index * primary_step for index in range(bars_per_bucket)]
        if any(ts not in by_open_time for ts in expected):
            continue
        bars = [by_open_time[ts] for ts in expected]
        with localcontext(_DECIMAL_CONTEXT) as ctx:
            open_value = _decimal(bars[0]["open"], "$.transform.ohlcv_resample.open")
            high_value = max(
                _decimal(bar["high"], "$.transform.ohlcv_resample.high") for bar in bars
            )
            low_value = min(
                _decimal(bar["low"], "$.transform.ohlcv_resample.low") for bar in bars
            )
            close_value = _decimal(bars[-1]["close"], "$.transform.ohlcv_resample.close")
            open_value, high_value, low_value, close_value = (
                +open_value,
                +high_value,
                +low_value,
                +close_value,
            )
        output.append(
            {
                "open_time": bucket_open,
                "open": canonical_decimal_str(open_value),
                "high": canonical_decimal_str(high_value),
                "low": canonical_decimal_str(low_value),
                "close": canonical_decimal_str(close_value),
            }
        )
    return output


def _quantile_r7(sorted_values: list[Decimal], quantile: Decimal) -> Decimal:
    """R type-7 linear-interpolation quantile, per SPEC §5.5 ``rolling_quantile``.

    ``sorted_values`` must already be ascending; ``quantile`` strictly in
    (0, 1) guarantees ``floor(h)`` and ``floor(h)+1`` are always valid indices
    once ``len(sorted_values) >= 2``. A single-sample window (reachable at
    the stream-start boundary with ``min_periods=1``) has no second order
    statistic to interpolate against, and any quantile of one observation is
    that observation, so it is returned directly without indexing ``x[1]``.
    """
    n = len(sorted_values)
    if n == 1:
        with localcontext(_DECIMAL_CONTEXT):
            return +sorted_values[0]
    with localcontext(_DECIMAL_CONTEXT) as ctx:
        h = ctx.multiply(Decimal(n - 1), quantile)
        floor_h = int(h.to_integral_value(rounding=ROUND_FLOOR))
        fraction = ctx.subtract(h, Decimal(floor_h))
        lower = sorted_values[floor_h]
        upper = sorted_values[floor_h + 1]
        return +ctx.add(lower, ctx.multiply(fraction, ctx.subtract(upper, lower)))


def _stream_lookback(
    ordered_timestamps: list[int],
    rows_by_ts: dict[int, tuple[Any, int, str]],
    anchor_ts: int,
    step: int,
    window_bars: int,
    error_path: str,
) -> Optional[tuple[list[Decimal], int]]:
    """Gather up to ``window_bars`` raw values ending at ``anchor_ts`` (frame
    counted, inclusive of the anchor), stepping back by ``step``.

    Returns ``None`` when a timestamp inside the stream's fetched range
    ``[ordered_timestamps[0], ordered_timestamps[-1]]`` has a missing/invalid
    value — a genuine gap per §5.5, never skipped. Timestamps *before* the
    stream's fetched range are treated as insufficient warmup history (not a
    gap) and simply omitted from the returned list, so callers that require an
    exact ``window_bars`` count can detect the boundary case via
    ``len(values) < window_bars`` while ``rolling_quantile``'s ``min_periods``
    leniency can accept the shorter, most-recent-first list as-is.
    Returns ``(values, availability_ceiling)`` where ``availability_ceiling``
    is the maximum ``available_at`` among the raw points actually used.
    """
    if not ordered_timestamps:
        return [], 0
    stream_start = ordered_timestamps[0]
    stream_end = ordered_timestamps[-1]
    values: list[Decimal] = []
    ceiling = 0
    for offset in range(window_bars - 1, -1, -1):
        ts = anchor_ts - offset * step
        if ts < stream_start or ts > stream_end:
            continue
        point = rows_by_ts.get(ts)
        if point is None:
            return None
        raw_value, available_at, _revision = point
        try:
            values.append(_decimal(raw_value, error_path))
        except KernelExecutionError:
            return None
        ceiling = max(ceiling, available_at)
    return values, ceiling


def _evaluate_windowed_primitive(
    feature: dict[str, Any],
    rows_by_ts: dict[int, tuple[Any, int, str]],
    ordered_timestamps: list[int],
    anchor_ts: int,
    step: int,
    error_path: str,
) -> Optional[tuple[Decimal, int]]:
    """Dispatch ``rolling_sum``/``rolling_extreme``/``rolling_quantile`` per
    their frozen §5.5 semantics. ``rsi_wilder`` is precomputed separately (its
    recursive seed needs the stream's full fetched history, not a fixed
    window) — see ``_rsi_wilder_series``.
    """
    primitive = feature["primitive"]
    params = feature["params"]
    if primitive == "rolling_sum":
        window_bars = params["window_bars"]
        result = _stream_lookback(
            ordered_timestamps, rows_by_ts, anchor_ts, step, window_bars, error_path
        )
        if result is None:
            return None
        values, ceiling = result
        if len(values) != window_bars:
            return None
        with localcontext(_DECIMAL_CONTEXT):
            total = +sum(values, Decimal(0))
        return total, ceiling
    if primitive == "rolling_extreme":
        window_bars = params["window_bars"]
        result = _stream_lookback(
            ordered_timestamps, rows_by_ts, anchor_ts, step, window_bars, error_path
        )
        if result is None:
            return None
        values, ceiling = result
        if len(values) != window_bars:
            return None
        # min()/max() only compare and return one of the existing Decimal
        # objects — no arithmetic happens, so unlike sum() nothing rounds it
        # to decimal128 automatically. A raw source value with more than 34
        # significant digits would otherwise pass through unrounded.
        with localcontext(_DECIMAL_CONTEXT):
            extreme = +(min(values) if params["mode"] == "min" else max(values))
        return extreme, ceiling
    if primitive == "rolling_quantile":
        window_bars = params["window_bars"]
        min_periods = params["min_periods"]
        result = _stream_lookback(
            ordered_timestamps, rows_by_ts, anchor_ts, step, window_bars, error_path
        )
        if result is None:
            return None
        values, ceiling = result
        if len(values) < min_periods:
            return None
        quantile = Decimal(params["quantile"])
        return _quantile_r7(sorted(values), quantile), ceiling
    return None


def _rsi_wilder_series(
    rows_by_ts: dict[int, tuple[Any, int, str]],
    ordered_timestamps: list[int],
    period: int,
    error_path: str,
) -> dict[int, tuple[Optional[Decimal], int]]:
    """SPEC §5.5 ``rsi_wilder``: seed = simple average of the first ``period``
    deltas measured from the stream's own fetched-window start (the recursion
    anchor is the fetch window, never the stream's full unbounded history);
    Wilder recursion afterward; ``avg_loss == 0`` -> RSI 100. A missing/invalid
    raw value breaks the recursive chain irrecoverably: every timestamp from
    that point forward is also missing, since Wilder's average is a running
    function of every prior sample.

    Computed once per feature over the whole fetched range (not once per
    decision frame) since the recursion is inherently sequential; returns a
    ``{ts: (value_or_None, availability_ceiling)}`` map for O(1) frame lookup.
    """
    series: dict[int, tuple[Optional[Decimal], int]] = {}
    n = len(ordered_timestamps)
    if n < period + 1:
        for ts in ordered_timestamps:
            series[ts] = (None, 0)
        return series
    parsed: list[Optional[Decimal]] = []
    ceilings: list[int] = []
    broken_from: Optional[int] = None
    running_ceiling = 0
    for index, ts in enumerate(ordered_timestamps):
        raw_value, available_at, _revision = rows_by_ts[ts]
        running_ceiling = max(running_ceiling, available_at)
        ceilings.append(running_ceiling)
        try:
            parsed.append(_decimal(raw_value, error_path))
        except KernelExecutionError:
            parsed.append(None)
            if broken_from is None:
                broken_from = index
    for ts in ordered_timestamps[:period]:
        series[ts] = (None, 0)
    avg_gain: Optional[Decimal] = None
    avg_loss: Optional[Decimal] = None
    with localcontext(_DECIMAL_CONTEXT) as ctx:
        for index in range(period, n):
            ts = ordered_timestamps[index]
            if broken_from is not None and index >= broken_from:
                series[ts] = (None, 0)
                avg_gain = None
                avg_loss = None
                continue
            if index == period:
                gains: list[Decimal] = []
                losses: list[Decimal] = []
                for k in range(1, period + 1):
                    delta = ctx.subtract(parsed[k], parsed[k - 1])
                    gains.append(delta if delta > 0 else Decimal(0))
                    losses.append(-delta if delta < 0 else Decimal(0))
                avg_gain = ctx.divide(sum(gains, Decimal(0)), Decimal(period))
                avg_loss = ctx.divide(sum(losses, Decimal(0)), Decimal(period))
            else:
                delta = ctx.subtract(parsed[index], parsed[index - 1])
                gain = delta if delta > 0 else Decimal(0)
                loss = -delta if delta < 0 else Decimal(0)
                avg_gain = ctx.divide(
                    ctx.add(ctx.multiply(avg_gain, Decimal(period - 1)), gain),
                    Decimal(period),
                )
                avg_loss = ctx.divide(
                    ctx.add(ctx.multiply(avg_loss, Decimal(period - 1)), loss),
                    Decimal(period),
                )
            if avg_loss == 0:
                rsi = Decimal(100)
            else:
                rs = ctx.divide(avg_gain, avg_loss)
                rsi = ctx.subtract(
                    Decimal(100), ctx.divide(Decimal(100), ctx.add(Decimal(1), rs))
                )
            series[ts] = (+rsi, ceilings[index])
    return series


def build_frames(
    data_streams: dict[str, list[dict[str, Any]]],
    coverage_manifest: dict[str, Any],
    plan: CompiledPlan,
) -> list[FeatureFrame]:
    """Build closed frames from a declared-only exact stream set.

    ``data_streams`` keys are manifest ``stream_id`` values.  K-line rows use
    ``open_time/open/high/low/close/volume``; feature rows use
    ``ts/value/available_at/revision``.  No source substitution is attempted.
    """
    requirements = plan.artifact_manifest["data_requirements"]
    expected_keys = {item["stream_id"] for item in requirements}
    if set(data_streams) != expected_keys:
        _raise(
            "$.data_streams",
            "must equal manifest data requirement exact-set",
            code=ERR_COVERAGE_INCOMPLETE,
            required=sorted(expected_keys),
            actual=sorted(data_streams),
        )
    if coverage_manifest.get("summary", {}).get("strict_eligible") is not True:
        _raise(
            "$.coverage_manifest.summary.strict_eligible",
            "strict coverage required",
            code=ERR_COVERAGE_INCOMPLETE,
        )
    primary = next(
        item
        for item in requirements
        if item["execution_role"] == "primary_execution_kline"
    )
    primary_rows = data_streams[primary["stream_id"]]
    if not primary_rows:
        _raise(
            "$.data_streams",
            "primary K-line stream is empty",
            code=ERR_COVERAGE_INCOMPLETE,
        )
    market_timeframe = plan.strategy_spec["market"]["timeframe"]

    def _is_kline_primary_passthrough(feature: dict[str, Any]) -> bool:
        return (
            feature["source_stream"].startswith(_KLINE_PRIMARY_PREFIX)
            and feature["interval"] == market_timeframe
        )

    feature_streams: dict[str, tuple[dict[int, tuple[Any, int, str]], list[int]]] = {}
    for requirement in requirements:
        if requirement["kind"] != "feature":
            continue
        matched_features = [
            feature
            for feature in plan.strategy_spec["features"]
            if requirement["stream_id"].startswith(feature["source_stream"] + ".")
            and feature["interval"] == requirement["interval"]
        ]
        if not matched_features:
            _raise(
                f"$.artifact_manifest.data_requirements[{requirement['stream_id']}]",
                "feature requirement has no StrategySpec feature",
                code=ERR_COVERAGE_INCOMPLETE,
            )
        rows_by_ts: dict[int, tuple[Any, int, str]] = {}
        ordered_timestamps: list[int] = []
        for row in data_streams[requirement["stream_id"]]:
            if set(row) != {"ts", "value", "available_at", "revision"}:
                _raise(
                    "$.data_streams",
                    "feature row has unknown/missing keys",
                    code=ERR_COVERAGE_INCOMPLETE,
                )
            ts = _safe_int(row["ts"], "$.data_streams.feature.ts")
            available_at = _safe_int(
                row["available_at"], "$.data_streams.feature.available_at"
            )
            if ts in rows_by_ts:
                _raise(
                    "$.data_streams",
                    "duplicate feature timestamp",
                    code=ERR_COVERAGE_INCOMPLETE,
                )
            if available_at < ts:
                _raise(
                    "$.data_streams.feature.available_at",
                    "cannot precede the source timestamp",
                    code=ERR_COVERAGE_INCOMPLETE,
                )
            if not isinstance(row["revision"], str) or not row["revision"]:
                _raise(
                    "$.data_streams.feature.revision",
                    "must be a non-empty string",
                    code=ERR_COVERAGE_INCOMPLETE,
                )
            rows_by_ts[ts] = (row["value"], available_at, str(row["revision"]))
            ordered_timestamps.append(ts)
        ordered_timestamps.sort()
        feature_step = _interval_seconds(requirement["interval"])
        if any(
            current - previous != feature_step
            for previous, current in zip(ordered_timestamps, ordered_timestamps[1:])
        ):
            _raise(
                "$.data_streams.feature",
                "rows must be sorted and contiguous at the declared interval",
                code=ERR_COVERAGE_INCOMPLETE,
            )
        for feature in matched_features:
            if feature["key"] in feature_streams:
                _raise(
                    f"$.artifact_manifest.data_requirements[{requirement['stream_id']}]",
                    "multiple requirements bind the same StrategySpec feature",
                    code=ERR_COVERAGE_INCOMPLETE,
                )
            feature_streams[feature["key"]] = (rows_by_ts, ordered_timestamps)

    # kline.primary passthrough features (interval == primary timeframe) read
    # directly from the primary K-line series and never bind a data
    # requirement of their own (§5.5: "不产生新 stream，不需要 transform").
    missing_feature_keys = {
        feature["key"]
        for feature in plan.strategy_spec["features"]
        if not _is_kline_primary_passthrough(feature)
    } - set(feature_streams)
    if missing_feature_keys:
        _raise(
            "$.artifact_manifest.data_requirements",
            "missing data requirement for StrategySpec feature",
            code=ERR_COVERAGE_INCOMPLETE,
            required=sorted(missing_feature_keys),
        )

    identity = coverage_manifest.get("request_identity")
    if not isinstance(identity, dict) or not isinstance(identity.get("symbol"), str):
        _raise(
            "$.coverage_manifest.request_identity",
            "selected symbol is required",
            code=ERR_COVERAGE_INCOMPLETE,
        )
    selected_symbol = identity["symbol"]
    if selected_symbol not in plan.strategy_spec["market"]["symbols"]:
        _raise(
            "$.coverage_manifest.request_identity.symbol",
            "outside StrategySpec symbol domain",
            code=ERR_COVERAGE_INCOMPLETE,
        )
    # kline.primary passthrough fields are read straight off the primary
    # series; build a synthetic (rows_by_ts, ordered_timestamps) pair per
    # field lazily so it flows through the same primitive dispatch below.
    timeframe_seconds = _interval_seconds(market_timeframe)
    primary_ordered_ts = sorted(row["open_time"] for row in primary_rows)
    _primary_field_cache: dict[str, dict[int, tuple[Any, int, str]]] = {}

    def _primary_field_series(field: str) -> dict[int, tuple[Any, int, str]]:
        if field not in _primary_field_cache:
            _primary_field_cache[field] = {
                row["open_time"]: (
                    row[field],
                    row["open_time"] + timeframe_seconds,
                    "",
                )
                for row in primary_rows
            }
        return _primary_field_cache[field]

    # rsi_wilder's Wilder recursion needs the stream's full fetched history;
    # precompute once per feature (not once per decision frame). A passthrough
    # kline.primary feature has no feature_streams entry of its own (it reads
    # the primary series directly) but still needs its RSI series precomputed
    # from that same synthetic per-field series, or the per-frame lookup below
    # would KeyError on a cache that was never populated for it.
    rsi_series_cache: dict[str, dict[int, tuple[Optional[Decimal], int]]] = {}
    feature_revision: dict[str, str] = {}
    for feature in plan.strategy_spec["features"]:
        if _is_kline_primary_passthrough(feature):
            if feature["primitive"] == "rsi_wilder":
                field = feature["source_stream"][len(_KLINE_PRIMARY_PREFIX) :]
                rsi_series_cache[feature["key"]] = _rsi_wilder_series(
                    _primary_field_series(field),
                    primary_ordered_ts,
                    feature["params"]["period"],
                    f"$.data_streams.{feature['source_stream']}",
                )
            continue
        rows_by_ts, ordered_timestamps = feature_streams[feature["key"]]
        feature_revision[feature["key"]] = next(
            iter(rows_by_ts.values()), (None, None, "")
        )[2]
        if feature["primitive"] == "rsi_wilder":
            rsi_series_cache[feature["key"]] = _rsi_wilder_series(
                rows_by_ts,
                ordered_timestamps,
                feature["params"]["period"],
                f"$.data_streams.{feature['source_stream']}",
            )

    frames: list[FeatureFrame] = []
    for row_index, row in enumerate(primary_rows):
        if set(row) != {"open_time", "open", "high", "low", "close", "volume"}:
            _raise(
                f"$.data_streams.primary[{row_index}]",
                "K-line row has unknown/missing keys",
                code=ERR_COVERAGE_INCOMPLETE,
            )
        open_at = _safe_int(
            row["open_time"], f"$.data_streams.primary[{row_index}].open_time"
        )
        close_at = open_at + timeframe_seconds
        values: dict[str, Any] = {
            "open": canonical_decimal_str(
                _decimal(row["open"], "$.data_streams.primary.open")
            ),
            "high": canonical_decimal_str(
                _decimal(row["high"], "$.data_streams.primary.high")
            ),
            "low": canonical_decimal_str(
                _decimal(row["low"], "$.data_streams.primary.low")
            ),
            "close": canonical_decimal_str(
                _decimal(row["close"], "$.data_streams.primary.close")
            ),
            "volume": canonical_decimal_str(
                _decimal(row["volume"], "$.data_streams.primary.volume")
            ),
        }
        revisions: dict[str, str] = {}
        for feature in plan.strategy_spec["features"]:
            if feature["primitive"] not in _KNOWN_PRIMITIVES:
                _raise(
                    f"$.strategy_spec.features[{feature['key']}].primitive",
                    "primitive is not implemented",
                    code=ERR_SPEC_UNSUPPORTED,
                )
            error_path = f"$.data_streams.{feature['source_stream']}"
            passthrough = _is_kline_primary_passthrough(feature)
            coarse_kline = (
                not passthrough
                and feature["source_stream"].startswith(_KLINE_PRIMARY_PREFIX)
            )
            if passthrough:
                field = feature["source_stream"][len(_KLINE_PRIMARY_PREFIX) :]
                rows_by_ts = _primary_field_series(field)
                ordered_timestamps = primary_ordered_ts
                anchor_ts = open_at
            elif coarse_kline:
                # §5.5 as-of alignment: the most recent complete bucket whose
                # close (== available_at) is <= this decision frame's close.
                rows_by_ts, ordered_timestamps = feature_streams[feature["key"]]
                feature_step = _interval_seconds(feature["interval"])
                bucket_close = (close_at // feature_step) * feature_step
                anchor_ts = bucket_close - feature_step
            else:
                rows_by_ts, ordered_timestamps = feature_streams[feature["key"]]
                anchor_ts = open_at

            if feature["primitive"] == "rsi_wilder":
                point = rsi_series_cache[feature["key"]].get(anchor_ts)
                if point is None or point[0] is None:
                    _raise(
                        error_path,
                        "required feature window is missing or not yet available",
                        code=ERR_COVERAGE_INCOMPLETE,
                        required=anchor_ts,
                    )
                value, ceiling = point
            else:
                step = _interval_seconds(feature["interval"])
                outcome = _evaluate_windowed_primitive(
                    feature, rows_by_ts, ordered_timestamps, anchor_ts, step, error_path
                )
                if outcome is None:
                    _raise(
                        error_path,
                        "required feature window is missing or not yet available",
                        code=ERR_COVERAGE_INCOMPLETE,
                        required=anchor_ts,
                    )
                value, ceiling = outcome
            if ceiling > close_at:
                _raise(
                    error_path,
                    "required feature window is missing or not yet available",
                    code=ERR_COVERAGE_INCOMPLETE,
                    required=anchor_ts,
                )
            # canonical_decimal_str()'s normalize() rounds to whatever context
            # is ambient at the call site (Python's default is prec=28, not
            # decimal128's 34) — every primitive above already rounds `value`
            # to 34 significant digits internally, but that rounding is only
            # observable in the final string if serialization also happens
            # while decimal128 is still the active context.
            with localcontext(_DECIMAL_CONTEXT):
                values[feature["key"]] = canonical_decimal_str(value)
            if not passthrough:
                revisions[feature["source_stream"]] = feature_revision[feature["key"]]
        frames.append(
            FeatureFrame(
                open_at, close_at, close_at, selected_symbol, values, revisions
            )
        )
    if any(
        frames[index + 1].bar_open_at - frames[index].bar_open_at != timeframe_seconds
        for index in range(len(frames) - 1)
    ):
        _raise(
            "$.data_streams.primary",
            "K-lines must be sorted and contiguous at the decision timeframe",
            code=ERR_COVERAGE_INCOMPLETE,
        )
    return frames


def _interval_seconds(interval: str) -> int:
    unit = interval[-1]
    multiplier = {"m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
    return int(interval[:-1]) * multiplier


def initial_state(plan: CompiledPlan, execution_params: dict[str, Any]) -> KernelState:
    expected = {
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
    _exact(execution_params, expected, "$.execution_params")
    spec = plan.strategy_spec
    if execution_params["symbol"] not in spec["market"]["symbols"]:
        _raise("$.execution_params.symbol", "outside StrategySpec symbol domain")
    if (
        execution_params["market"] != spec["market"]["market_type"]
        or execution_params["timeframe"] != spec["market"]["timeframe"]
    ):
        _raise(
            "$.execution_params", "market/timeframe differs from immutable StrategySpec"
        )
    if execution_params["provider_tool_id"] != COMPILER_TOOL_ID:
        _raise(
            "$.execution_params.provider_tool_id",
            "artifact request must use compiler tool",
        )
    start_at = _safe_int(execution_params["start_at"], "$.execution_params.start_at")
    end_at = _safe_int(execution_params["end_at"], "$.execution_params.end_at")
    if end_at <= start_at:
        _raise("$.execution_params", "end_at must be greater than start_at")
    if (
        execution_params["fee_bps"] != spec["execution"]["cost_model"]["fee_bps"]
        or execution_params["slippage_bps"]
        != spec["execution"]["cost_model"]["slippage_bps"]
    ):
        _raise("$.execution_params", "cost values differ from immutable StrategySpec")
    capital = _canonical_decimal(
        execution_params["initial_capital"],
        "$.execution_params.initial_capital",
        positive=True,
    )
    params = execution_params["provider_params"]
    if not isinstance(params, dict) or set(params) != {"instrument_rules"}:
        _raise(
            "$.execution_params.provider_params",
            "must contain only trusted instrument_rules",
        )
    rules = _exact(
        params["instrument_rules"],
        {"symbol", "price_tick", "qty_step", "min_qty", "min_notional"},
        "$.execution_params.provider_params.instrument_rules",
    )
    if rules["symbol"] != execution_params["symbol"]:
        _raise(
            "$.execution_params.provider_params.instrument_rules.symbol",
            "must equal selected symbol",
        )
    for key in ("price_tick", "qty_step", "min_qty", "min_notional"):
        _canonical_decimal(
            rules[key],
            f"$.execution_params.provider_params.instrument_rules.{key}",
            positive=True,
        )
    return KernelState(
        equity=capital,
        initial_capital=capital,
        instrument_rules=copy.deepcopy(rules),
        execution_start_at=start_at,
        execution_end_at=end_at,
    )


def _ctx_op(op: str, args: list[Any]) -> Any:
    if all(isinstance(item, int) and not isinstance(item, bool) for item in args):
        if op == "add":
            value = sum(args)
        elif op == "sub":
            value = args[0] - args[1]
        elif op == "mul":
            value = 1
            for item in args:
                value *= item
        elif op == "min":
            value = min(args)
        elif op == "max":
            value = max(args)
        elif op == "abs":
            value = abs(args[0])
        else:
            raise KernelExecutionError(
                ERR_SPEC_INVALID, "$.expression", "invalid integer operator"
            )
        if abs(value) > _JS_SAFE_INT:
            raise KernelExecutionError(
                ERR_SPEC_INVALID, "$.expression", "integer overflow"
            )
        return value
    decimal_args = [_decimal(item, "$.expression") for item in args]
    try:
        with localcontext(_DECIMAL_CONTEXT) as ctx:
            if op == "add":
                value = Decimal(0)
                for item in decimal_args:
                    value = ctx.add(value, item)
            elif op == "sub":
                value = ctx.subtract(decimal_args[0], decimal_args[1])
            elif op == "mul":
                value = Decimal(1)
                for item in decimal_args:
                    value = ctx.multiply(value, item)
            elif op == "div":
                value = ctx.divide(decimal_args[0], decimal_args[1])
            elif op == "min":
                value = min(decimal_args)
            elif op == "max":
                value = max(decimal_args)
            elif op == "abs":
                value = ctx.abs(decimal_args[0])
            else:
                raise InvalidOperation
            return +value
    except (ArithmeticError, InvalidOperation) as exc:
        raise KernelExecutionError(
            ERR_SPEC_INVALID, "$.expression", "decimal operation failed"
        ) from exc


class StrategyKernel:
    def __init__(self, plan: CompiledPlan) -> None:
        self.plan = plan
        self.parameters = {
            item["key"]: item["value"] for item in plan.strategy_spec["parameters"]
        }

    def _value(self, expr: dict[str, Any], state: KernelState, index: int) -> Any:
        node = expr["node"]
        if node == "literal":
            return (
                Decimal(expr["value"])
                if expr["value_type"] == "decimal"
                else expr["value"]
            )
        if node == "parameter":
            value = self.parameters[expr["key"]]
            return (
                Decimal(value)
                if self.plan.parameter_types[expr["key"]] == "decimal"
                else value
            )
        if node == "feature":
            target = index - expr["lag_bars"]
            if target < 0 or expr["key"] not in state.frames[target].values:
                raise KernelExecutionError(
                    ERR_COVERAGE_INCOMPLETE,
                    "$.frame.values",
                    "required lagged feature is missing",
                )
            value = state.frames[target].values[expr["key"]]
            return (
                Decimal(value)
                if self.plan.feature_types[expr["key"]] == "decimal"
                else value
            )
        if node == "arithmetic":
            return _ctx_op(
                expr["op"], [self._value(item, state, index) for item in expr["args"]]
            )
        raise KernelExecutionError(
            ERR_SPEC_INVALID, "$.expression", "unknown compiled ValueExpr"
        )

    def _condition(self, expr: dict[str, Any], state: KernelState, index: int) -> bool:
        node = expr["node"]
        if node in {"compare", "cross"}:
            left = self._value(expr["left"], state, index)
            right = self._value(expr["right"], state, index)
            if node == "cross":
                if index == 0:
                    return False
                previous_left = self._value(expr["left"], state, index - 1)
                previous_right = self._value(expr["right"], state, index - 1)
                if expr["op"] == "crosses_above":
                    return previous_left <= previous_right and left > right
                return previous_left >= previous_right and left < right
            return {
                "gt": left > right,
                "gte": left >= right,
                "lt": left < right,
                "lte": left <= right,
                "eq": left == right,
                "neq": left != right,
            }[expr["op"]]
        if node == "all":
            return all(self._condition(item, state, index) for item in expr["args"])
        if node == "any":
            return any(self._condition(item, state, index) for item in expr["args"])
        if node == "not":
            return not self._condition(expr["arg"], state, index)
        raise KernelExecutionError(
            ERR_SPEC_INVALID, "$.condition", "unknown compiled ConditionExpr"
        )

    @staticmethod
    def _quantize(value: Decimal, tick: Decimal, direction: str) -> Decimal:
        rounding = ROUND_FLOOR if direction == "down" else ROUND_CEILING
        with localcontext(_DECIMAL_CONTEXT):
            units = (value / tick).to_integral_value(rounding=rounding)
            return +(units * tick)

    def _entry_triggers(
        self, state: KernelState, entry_price: Decimal, pending: PendingEntry
    ) -> tuple[Optional[Decimal], Optional[Decimal]]:
        side = self.plan.strategy_spec["entry"]["side"]
        rules = state.instrument_rules
        tick = Decimal(rules["price_tick"])
        exit_spec = self.plan.strategy_spec["exit"]
        stop_rule = exit_spec["stop_loss"]
        with localcontext(_DECIMAL_CONTEXT):
            if stop_rule["model"] == "fixed_percent":
                ratio = Decimal(stop_rule["value"])
                raw_stop = entry_price * (1 - ratio if side == "long" else 1 + ratio)
            else:
                raw_stop = pending.stop_value
        assert raw_stop is not None
        stop_direction = "down" if side == "long" else "up"
        stop = self._quantize(raw_stop, tick, stop_direction)
        take_rule = exit_spec["take_profit"]
        with localcontext(_DECIMAL_CONTEXT):
            if take_rule["model"] == "fixed_percent":
                ratio = Decimal(take_rule["value"])
                raw_take = entry_price * (1 + ratio if side == "long" else 1 - ratio)
            elif take_rule["model"] == "r_multiple":
                distance = abs(entry_price - stop)
                if distance <= 0:
                    raise KernelExecutionError(
                        ERR_SPEC_INVALID,
                        "$.strategy_spec.exit.take_profit",
                        "quantized 1R is zero",
                    )
                multiple = Decimal(take_rule["value"])
                raw_take = (
                    entry_price + multiple * distance
                    if side == "long"
                    else entry_price - multiple * distance
                )
            else:
                raw_take = pending.take_value
        assert raw_take is not None
        take_direction = "up" if side == "long" else "down"
        take = self._quantize(raw_take, tick, take_direction)
        if (side == "long" and not stop < entry_price < take) or (
            side == "short" and not take < entry_price < stop
        ):
            raise KernelExecutionError(
                ERR_SPEC_INVALID,
                "$.strategy_spec.exit",
                "quantized stop/take is on the wrong entry side",
            )
        return stop, take

    def _open_pending(
        self, state: KernelState, frame: FeatureFrame, index: int
    ) -> None:
        pending = state.pending_entry
        if pending is None:
            return
        state.pending_entry = None
        entry_price = _decimal(frame.values["open"], "$.frame.values.open")
        tick = Decimal(state.instrument_rules["price_tick"])
        with localcontext(_DECIMAL_CONTEXT) as ctx:
            tick_remainder = ctx.remainder(entry_price, tick)
        if tick_remainder != 0:
            raise KernelExecutionError(
                ERR_COVERAGE_INCOMPLETE,
                "$.frame.values.open",
                "entry reference price violates price_tick",
            )
        try:
            stop, take = self._entry_triggers(state, entry_price, pending)
        except KernelExecutionError as exc:
            state.diagnostics.append(
                {"frame_index": index, "kind": "order_rejected", "reason": exc.message}
            )
            return
        sizing = self.plan.strategy_spec["risk"]["position_sizing"]
        leverage = Decimal(self.plan.strategy_spec["risk"]["leverage"])
        value = Decimal(sizing["value"])
        try:
            with localcontext(_DECIMAL_CONTEXT) as ctx:
                if sizing["model"] == "fixed_notional":
                    raw_qty = ctx.divide(value, entry_price)
                elif sizing["model"] == "fixed_margin":
                    raw_qty = ctx.divide(ctx.multiply(value, leverage), entry_price)
                else:
                    risk_budget = ctx.multiply(state.equity, value)
                    risk_distance = ctx.abs(ctx.subtract(entry_price, stop))
                    raw_qty = ctx.divide(risk_budget, risk_distance)
                step = Decimal(state.instrument_rules["qty_step"])
                qty = ctx.multiply(
                    ctx.divide(raw_qty, step).to_integral_value(rounding=ROUND_FLOOR),
                    step,
                )
                order_notional = ctx.multiply(qty, entry_price)
                margin = ctx.divide(order_notional, leverage)
        except ArithmeticError as exc:
            raise KernelExecutionError(
                ERR_SPEC_INVALID,
                "$.strategy_spec.risk.position_sizing",
                "decimal sizing operation failed",
            ) from exc
        min_qty = Decimal(state.instrument_rules["min_qty"])
        min_notional = Decimal(state.instrument_rules["min_notional"])
        if qty < min_qty or order_notional < min_notional or margin > state.equity:
            state.diagnostics.append(
                {
                    "frame_index": index,
                    "kind": "order_rejected",
                    "reason": "instrument_or_margin_limit",
                }
            )
            return
        state.position = Position(
            side=self.plan.strategy_spec["entry"]["side"],
            qty=qty,
            entry_price=entry_price,
            opened_at=frame.bar_open_at,
            stop_loss=stop,
            take_profit=take,
            bars_held=1,
        )
        decision = {
            "frame_index": index,
            "kind": "entry_filled",
            "price": canonical_decimal_str(entry_price),
            "qty": canonical_decimal_str(qty),
        }
        state.decisions.append(decision)
        state.fill_ledger.append(copy.deepcopy(decision))

    def _exit_candidates(
        self, position: Position, frame: FeatureFrame
    ) -> dict[str, Decimal]:
        open_price = Decimal(frame.values["open"])
        high = Decimal(frame.values["high"])
        low = Decimal(frame.values["low"])
        close = Decimal(frame.values["close"])
        candidates: dict[str, Decimal] = {}
        stop = position.stop_loss
        take = position.take_profit
        if stop is not None:
            if (position.side == "long" and open_price <= stop) or (
                position.side == "short" and open_price >= stop
            ):
                candidates["stop_loss"] = open_price
            elif (position.side == "long" and low <= stop) or (
                position.side == "short" and high >= stop
            ):
                candidates["stop_loss"] = stop
        if take is not None:
            if (position.side == "long" and open_price >= take) or (
                position.side == "short" and open_price <= take
            ):
                candidates["take_profit"] = open_price
            elif (position.side == "long" and high >= take) or (
                position.side == "short" and low <= take
            ):
                candidates["take_profit"] = take
        if position.pending_signal_exit:
            candidates["signal_exit"] = open_price
        time_bars = self.plan.strategy_spec["exit"]["time_exit_bars"]
        if time_bars is not None and position.bars_held >= time_bars:
            candidates["time_exit"] = close
        return candidates

    def _close(
        self,
        state: KernelState,
        frame: FeatureFrame,
        index: int,
        exit_kind: str,
        exit_price: Decimal,
    ) -> None:
        position = state.position
        assert position is not None
        fee_bps = Decimal(self.plan.strategy_spec["execution"]["cost_model"]["fee_bps"])
        slippage_bps = Decimal(
            self.plan.strategy_spec["execution"]["cost_model"]["slippage_bps"]
        )
        try:
            with localcontext(_DECIMAL_CONTEXT) as ctx:
                notional_sum = ctx.multiply(
                    ctx.add(position.entry_price, exit_price), position.qty
                )
                fee = ctx.divide(ctx.multiply(notional_sum, fee_bps), Decimal(10000))
                slippage = ctx.divide(
                    ctx.multiply(notional_sum, slippage_bps), Decimal(10000)
                )
                price_delta = (
                    ctx.subtract(exit_price, position.entry_price)
                    if position.side == "long"
                    else ctx.subtract(position.entry_price, exit_price)
                )
                gross = ctx.multiply(price_delta, position.qty)
                pnl = ctx.subtract(ctx.subtract(gross, fee), slippage)
                state.equity = ctx.add(state.equity, pnl)
        except ArithmeticError as exc:
            raise KernelExecutionError(
                ERR_SPEC_INVALID, "$.execution.cost", "decimal cost operation failed"
            ) from exc
        seq = len(state.trades) + 1
        trade = {
            "seq": seq,
            "opened_at": position.opened_at,
            "closed_at": (
                frame.bar_close_at
                if exit_kind in {"time_exit", "end_of_data"}
                else frame.bar_open_at
            ),
            "side": position.side,
            "qty": canonical_decimal_str(position.qty),
            "entry_price": canonical_decimal_str(position.entry_price),
            "exit_price": canonical_decimal_str(exit_price),
            "fee": canonical_decimal_str(fee),
            "slippage": canonical_decimal_str(slippage),
            "pnl": canonical_decimal_str(pnl),
        }
        trace = {
            "seq": seq,
            "symbol": frame.symbol,
            "stop_loss": (
                canonical_decimal_str(position.stop_loss)
                if position.stop_loss is not None
                else None
            ),
            "take_profit": (
                canonical_decimal_str(position.take_profit)
                if position.take_profit is not None
                else None
            ),
            "exit_kind": exit_kind,
        }
        state.trades.append(trade)
        state.trace_trades.append(trace)
        state.cost_ledger.append(
            {
                "seq": seq,
                "fee": trade["fee"],
                "slippage": trade["slippage"],
                "pnl": trade["pnl"],
            }
        )
        state.fill_ledger.append(
            {
                "frame_index": index,
                "kind": "exit_filled",
                "exit_kind": exit_kind,
                "price": trade["exit_price"],
            }
        )
        state.decisions.append(
            {"frame_index": index, "kind": "exit_filled", "exit_kind": exit_kind}
        )
        state.position = None
        state.last_exit_index = index

    def evaluate(self, state: KernelState, frame: FeatureFrame) -> dict[str, Any]:
        """Advance one closed frame; the same method is used by replay and paper.

        Frames with ``bar_open_at`` before ``execution_start_at`` are warmup
        frames: they are validated and appended to ``state.frames`` so lagged
        feature/cross lookups have history by the first evaluation frame, but
        they are returned before any entry/exit decision logic runs and can
        never open, hold, or close a position. See ``KernelState.frames`` for
        the resulting ``frame_index`` offset: it counts warmup frames too, so
        the first evaluation-window frame is ``frame_index == warmup_bars``.
        """
        if frame.bar_close_at > state.execution_end_at:
            raise KernelExecutionError(
                ERR_COVERAGE_INCOMPLETE,
                "$.frame",
                "frame is outside the immutable execution window",
            )
        if frame.available_at > frame.bar_close_at:
            raise KernelExecutionError(
                ERR_COVERAGE_INCOMPLETE,
                "$.frame.available_at",
                "frame is not available at decision time",
            )
        if state.frames and frame.bar_open_at <= state.frames[-1].bar_open_at:
            raise KernelExecutionError(
                ERR_COVERAGE_INCOMPLETE,
                "$.frame.bar_open_at",
                "frames must be strictly increasing",
            )
        if frame.symbol != state.instrument_rules["symbol"]:
            raise KernelExecutionError(
                ERR_BINDING_MISMATCH,
                "$.frame.symbol",
                "frame symbol differs from execution binding",
            )
        price_tick = Decimal(state.instrument_rules["price_tick"])
        for price_key in ("open", "high", "low", "close"):
            price = _decimal(frame.values.get(price_key), f"$.frame.values.{price_key}")
            with localcontext(_DECIMAL_CONTEXT) as ctx:
                tick_remainder = ctx.remainder(price, price_tick)
            if price <= 0 or tick_remainder != 0:
                raise KernelExecutionError(
                    ERR_COVERAGE_INCOMPLETE,
                    f"$.frame.values.{price_key}",
                    "K-line reference price violates the trusted price_tick",
                )
        open_price = Decimal(frame.values["open"])
        high_price = Decimal(frame.values["high"])
        low_price = Decimal(frame.values["low"])
        close_price = Decimal(frame.values["close"])
        if not (
            low_price
            <= min(open_price, close_price)
            <= max(open_price, close_price)
            <= high_price
        ):
            raise KernelExecutionError(
                ERR_COVERAGE_INCOMPLETE,
                "$.frame.values",
                "K-line OHLC ordering is invalid",
            )
        state.frames.append(frame)
        index = len(state.frames) - 1
        before = len(state.decisions)
        if frame.bar_open_at < state.execution_start_at:
            return {
                "next_state": state,
                "decisions": copy.deepcopy(state.decisions[before:]),
                "diagnostics": copy.deepcopy(state.diagnostics),
            }
        held_from_previous_frame = state.position is not None
        self._open_pending(state, frame, index)
        if held_from_previous_frame and state.position is not None:
            state.position.bars_held += 1
        if state.position is not None:
            candidates = self._exit_candidates(state.position, frame)
            for exit_kind in self.plan.strategy_spec["execution"]["intrabar_priority"]:
                if exit_kind in candidates:
                    self._close(state, frame, index, exit_kind, candidates[exit_kind])
                    break
        if state.position is not None:
            signal_exit = self.plan.strategy_spec["exit"]["signal_exit"]
            state.position.pending_signal_exit = bool(
                signal_exit is not None and self._condition(signal_exit, state, index)
            )
        if state.position is None and state.pending_entry is None:
            cooldown = self.plan.strategy_spec["entry"]["cooldown_bars"]
            eligible = (
                state.last_exit_index is None
                or index - state.last_exit_index >= cooldown
            )
            if eligible and self._condition(
                self.plan.strategy_spec["entry"]["condition"], state, index
            ):
                exit_spec = self.plan.strategy_spec["exit"]
                stop_value = (
                    self._value(exit_spec["stop_loss"]["value"], state, index)
                    if exit_spec["stop_loss"]["model"] == "feature_expression"
                    else None
                )
                take_value = (
                    self._value(exit_spec["take_profit"]["value"], state, index)
                    if exit_spec["take_profit"]["model"] == "feature_expression"
                    else None
                )
                state.pending_entry = PendingEntry(index, stop_value, take_value)
                state.decisions.append({"frame_index": index, "kind": "entry_pending"})
        return {
            "next_state": state,
            "decisions": copy.deepcopy(state.decisions[before:]),
            "diagnostics": copy.deepcopy(state.diagnostics),
        }

    def finalize(self, state: KernelState) -> None:
        if state.pending_entry is not None:
            state.diagnostics.append(
                {"frame_index": len(state.frames) - 1, "kind": "no_next_bar"}
            )
            state.pending_entry = None
        if state.position is not None:
            if not state.frames:
                raise KernelExecutionError(
                    ERR_COVERAGE_INCOMPLETE, "$.frames", "cannot close without a frame"
                )
            frame = state.frames[-1]
            self._close(
                state,
                frame,
                len(state.frames) - 1,
                "end_of_data",
                Decimal(frame.values["close"]),
            )


def _max_drawdown(curve: list[dict[str, Any]]) -> Decimal:
    peak: Optional[Decimal] = None
    maximum = Decimal(0)
    try:
        with localcontext(_DECIMAL_CONTEXT) as ctx:
            for point in curve:
                equity = Decimal(point["equity"])
                peak = equity if peak is None or equity > peak else peak
                if peak:
                    drawdown = ctx.divide(ctx.subtract(peak, equity), peak)
                    maximum = max(maximum, drawdown)
    except ArithmeticError as exc:
        raise KernelExecutionError(
            ERR_SPEC_INVALID,
            "$.metrics.max_drawdown",
            "decimal metric operation failed",
        ) from exc
    return maximum


def simulate(
    plan: CompiledPlan, frames: list[FeatureFrame], state: KernelState
) -> dict[str, Any]:
    kernel = StrategyKernel(plan)
    for frame in frames:
        kernel.evaluate(state, frame)
    kernel.finalize(state)
    curve = [
        {
            "ts": state.execution_start_at,
            "equity": canonical_decimal_str(state.initial_capital),
        }
    ]
    running = state.initial_capital
    try:
        with localcontext(_DECIMAL_CONTEXT) as ctx:
            for trade in state.trades:
                running = ctx.add(running, Decimal(trade["pnl"]))
                curve.append(
                    {
                        "ts": trade["closed_at"],
                        "equity": canonical_decimal_str(running),
                    }
                )
            total_return = ctx.divide(
                ctx.subtract(running, state.initial_capital), state.initial_capital
            )
    except ArithmeticError as exc:
        raise KernelExecutionError(
            ERR_SPEC_INVALID, "$.metrics", "decimal metric operation failed"
        ) from exc
    return {
        "trades": copy.deepcopy(state.trades),
        "equity_curve": curve,
        "metrics": {
            "total_return": canonical_decimal_str(total_return),
            "max_drawdown": canonical_decimal_str(_max_drawdown(curve)),
            "trade_count": len(state.trades),
        },
        "trace_trades": copy.deepcopy(state.trace_trades),
        "decisions": copy.deepcopy(state.decisions),
        "diagnostics": copy.deepcopy(state.diagnostics),
        "fill_ledger": copy.deepcopy(state.fill_ledger),
        "cost_ledger": copy.deepcopy(state.cost_ledger),
    }


def paper_tick(
    kernel: StrategyKernel, state: KernelState, frame: FeatureFrame
) -> dict[str, Any]:
    """Thin future paper adapter, intentionally delegating to the one evaluate."""
    return kernel.evaluate(state, frame)


__all__ = [
    "ARTIFACT_DIGEST_SCHEMA",
    "ARTIFACT_MANIFEST_SCHEMA",
    "CAPABILITY_SCHEMA",
    "COMPILER_TOOL_ID",
    "ERR_BINDING_MISMATCH",
    "ERR_CAPABILITY_MISMATCH",
    "ERR_COVERAGE_INCOMPLETE",
    "ERR_SPEC_INVALID",
    "ERR_SPEC_UNSUPPORTED",
    "FeatureFrame",
    "KernelExecutionError",
    "KernelState",
    "StrategyContractError",
    "StrategyKernel",
    "build_frames",
    "capability_hash",
    "capability_payload",
    "compile_strategy",
    "initial_state",
    "kline_primary_bucket_required_end",
    "kline_primary_bucket_required_start",
    "ohlcv_resample",
    "paper_tick",
    "simulate",
]
