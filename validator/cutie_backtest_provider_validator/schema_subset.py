"""param_schema subset validation + numeric helpers (IMPL W3.9 §5.1, §6.2).

§5.1: param_schema is a JSON Schema *subset* — P0 supports
object / string / number / integer / boolean / enum / default / min / max / required.
Anything outside this subset (e.g. ``$ref``, ``oneOf``, ``allOf``, ``patternProperties``)
is rejected so the connector/UI can render a structured form deterministically.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any, List

ALLOWED_TYPES = frozenset(
    {"object", "string", "number", "integer", "boolean", "array"}
)

# Keywords the P0 subset understands. Property-level keywords are validated
# recursively; top-level object keywords overlap with these.
ALLOWED_SCHEMA_KEYS = frozenset(
    {
        "type",
        "properties",
        "additionalProperties",
        "required",
        "enum",
        "default",
        "description",
        "title",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "items",
        "minItems",
        "maxItems",
    }
)

# Composition / reference keywords explicitly outside the P0 subset.
DISALLOWED_SCHEMA_KEYS = frozenset(
    {
        "$ref",
        "$defs",
        "definitions",
        "oneOf",
        "anyOf",
        "allOf",
        "not",
        "if",
        "then",
        "else",
        "patternProperties",
        "unevaluatedProperties",
        "dependentSchemas",
        "propertyNames",
    }
)

_DECIMAL_STRING_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)$")


def validate_param_schema(schema: Any, path: str = "param_schema") -> List[str]:
    """Validate a param_schema against the P0 subset. Returns list of errors."""
    errors: List[str] = []
    if not isinstance(schema, dict):
        return [f"{path} must be a JSON object (JSON Schema subset)"]

    for key in schema.keys():
        if key in DISALLOWED_SCHEMA_KEYS:
            errors.append(
                f"{path}.{key} is not allowed in the P0 param_schema subset"
            )

    schema_type = schema.get("type")
    if schema_type is not None:
        if isinstance(schema_type, list):
            for t in schema_type:
                if t not in ALLOWED_TYPES:
                    errors.append(f"{path}.type contains unsupported type {t!r}")
        elif schema_type not in ALLOWED_TYPES:
            errors.append(f"{path}.type {schema_type!r} is not supported")

    enum = schema.get("enum")
    if enum is not None and not isinstance(enum, list):
        errors.append(f"{path}.enum must be an array")

    required = schema.get("required")
    if required is not None:
        if not isinstance(required, list) or not all(
            isinstance(r, str) for r in required
        ):
            errors.append(f"{path}.required must be an array of strings")

    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, dict):
            errors.append(f"{path}.properties must be an object")
        else:
            for prop_name, prop_schema in properties.items():
                errors.extend(
                    validate_param_schema(prop_schema, f"{path}.properties.{prop_name}")
                )

    items = schema.get("items")
    if items is not None and isinstance(items, dict):
        errors.extend(validate_param_schema(items, f"{path}.items"))

    return errors


def is_decimal_string(value: Any) -> bool:
    """True if value is a plain decimal string (IMPL §6.2 money fields)."""
    if not isinstance(value, str):
        return False
    if not _DECIMAL_STRING_RE.match(value.strip()):
        return False
    try:
        Decimal(value.strip())
    except (InvalidOperation, ValueError):
        return False
    return True


def is_finite_number(value: Any) -> bool:
    """True if value is a JSON number that is finite (not NaN/Infinity)."""
    if isinstance(value, bool):
        # bool is a subclass of int; treat as non-numeric for ratio fields.
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return False
