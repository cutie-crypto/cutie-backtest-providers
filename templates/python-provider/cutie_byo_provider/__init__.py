"""Cutie BYO (Bring-Your-Own) backtest provider template.

A minimal FastAPI HTTP provider that conforms to the Cutie external backtest
provider contract (IMPL W3.9 §4-§9). Wrap your own backtest tool by editing
``adapter.py`` only.
"""

from .contract import (
    CATALOG_SCHEMA,
    REQUEST_SCHEMA,
    RESPONSE_SCHEMA,
    BacktestRequest,
    BacktestResult,
    CatalogResponse,
    CatalogTool,
    business_failure,
    decimal_str,
    json_safe,
    parse_decimal,
)

__all__ = [
    "CATALOG_SCHEMA",
    "REQUEST_SCHEMA",
    "RESPONSE_SCHEMA",
    "BacktestRequest",
    "BacktestResult",
    "CatalogResponse",
    "CatalogTool",
    "business_failure",
    "decimal_str",
    "json_safe",
    "parse_decimal",
]

__version__ = "1.0.0"
