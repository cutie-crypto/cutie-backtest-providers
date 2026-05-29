"""Adapter — the ONLY file you normally need to edit.

Wrap your existing backtest tool (a local Python library, a local CLI, or an
internal HTTP service running on the OpenClaw / Hermes machine) by filling in
three functions:

    list_tools()          -> list[CatalogTool]
    run_backtest(request) -> BacktestResult | dict (business failure)
    build_report(result)  -> Optional[str]  (relative report_url)

Everything else (FastAPI app, Bearer auth, schema serialization, secret scrub,
report retention, Decimal/JSON helpers) is provided by the template and you do
not need to touch it.

Hard rules (enforced by the protocol — see IMPL §4.2, §6, §7, §12):
- Money / quantity fields (capital, equity, price, qty, cost, fee, pnl, bps)
  are decimal strings. Use ``contract.decimal_str``.
- Ratio / percentage metrics (``*_pct``) are JSON numbers, never NaN/Infinity.
- ``report_url`` is a RELATIVE path/ref only — never host/port or local path.
- Never put API keys, exchange secrets, bearer tokens, or local filesystem
  paths into the catalog or response. Use ``security.scrub`` if unsure.
- ``security.live_trading`` must stay ``False``.
- A business failure (data missing, bad params, rate limited) returns a
  ``business_failure(...)`` dict with an UPPERCASE error_type — it is NOT an
  exception. Raising an exception is treated as ``ENGINE_ERROR``.

The default implementation below is a runnable "echo" backtest so you can start
the server and run a smoke request immediately. Replace the marked TODO bodies
with calls into your real engine.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Union

from .contract import (
    Adapter,
    BacktestRequest,
    BacktestResult,
    CatalogTool,
    DataSource,
    Execution,
    OutputSchema,
    ReportCapabilities,
    Security,
    business_failure,
    decimal_str,
    parse_decimal,
)
from . import reports

# ---------------------------------------------------------------------------
# Provider identity — edit these to describe your tool.
# ---------------------------------------------------------------------------

PROVIDER_ID = "my-local-backtester"
PROVIDER_NAME = "My Local Backtester"
PROVIDER_VERSION = "1.0.0"
PROVIDER_HOMEPAGE_URL = "https://example.com/docs"
PROVIDER_MAINTAINER = "kol_or_vendor_name"

ENGINE_NAME = "Example Engine"
ENGINE_VERSION = "2026.05"
DATA_SOURCE = "example_historical_data"

TOOL_ID = "my.backtester.default"

# Timeframes your engine can run. Used by /catalog and as a guard in run_backtest.
SUPPORTED_TIMEFRAMES = ["1h", "4h", "1d"]
SUPPORTED_MARKETS = ["spot"]


def engine_version() -> str:
    """Return the engine version string for /health, /catalog and responses.

    TODO: if your engine exposes a version (e.g. ``mylib.__version__``), read it
    here so the reported version always matches the installed engine.
    """
    return ENGINE_VERSION


# ---------------------------------------------------------------------------
# 1) list_tools  (IMPL §5.1)
# ---------------------------------------------------------------------------


def list_tools() -> List[CatalogTool]:
    """Return the tool catalog for this provider.

    TODO: describe the tool(s) your engine offers. Keep ``param_schema`` a small
    JSON Schema subset (object/string/number/integer/boolean/enum + default/
    min/max/required) so Cutie Web/RN can render a structured parameter form.

    Notes:
    - ``kind`` must be ``"external_http"``.
    - ``wrapper_type`` is one of ``python_inprocess`` / ``local_cli`` /
      ``local_http`` (pick the one that matches how you wrap the tool).
    - ``is_default`` (NOT ``default``); at most one tool may be the default.
    - ``security.live_trading`` must be ``False``.
    - Do NOT include a ``health`` field — the connector derives health.
    """
    return [
        CatalogTool(
            tool_id=TOOL_ID,
            kind="external_http",
            name="My Backtester Default",
            description="Runs my existing backtest engine through a local wrapper.",
            wrapper_type="local_cli",
            provider_name=PROVIDER_NAME,
            engine_name=ENGINE_NAME,
            engine_version=engine_version(),
            data_source=DataSource(
                type="provider_reported",
                name=DATA_SOURCE,
                description=(
                    "Historical OHLCV from the local engine; Cutie does not "
                    "verify coverage."
                ),
                coverage_hint="BTCUSDT 1h from 2023-01-01",
                external_unverified=True,
            ),
            supported_symbols=["BTCUSDT", "ETHUSDT"],
            markets=SUPPORTED_MARKETS,
            timeframes=SUPPORTED_TIMEFRAMES,
            is_default=True,
            execution=Execution(
                mode="sync",
                timeout_ms=120000,
                max_range_days=365,
                max_parallel_runs=1,
                async_supported=False,
            ),
            adapter=Adapter(
                requires_manual_export=False,
                working_dir_policy="ephemeral_or_provider_managed",
                result_file_patterns=["backtest-result-*.json"],
                upstream_auth_local_only=True,
            ),
            param_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "strategy_name": {"type": "string", "default": "default"},
                    "exchange": {"type": "string", "default": "binance"},
                },
            },
            output_schema=OutputSchema(
                metrics=["total_return_pct", "win_rate_pct", "max_drawdown_pct", "trade_count"],
                artifacts=["report_url"],
                series=["equity_curve"],
                tables=["trades"],
            ),
            report_capabilities=ReportCapabilities(
                report_url=True,
                scope="local_machine_only",
                formats=["html", "json"],
                retention_hint="last_100_runs",
            ),
            failure_codes=["INVALID_PARAMS", "NO_DATA", "RATE_LIMITED", "ENGINE_ERROR"],
            security=Security(
                network_scope="openclaw_hermes_local_or_private",
                requires_user_secret=True,
                secrets_stay_local=True,
                live_trading=False,
                filesystem_paths_exposed=False,
            ),
        )
    ]


# ---------------------------------------------------------------------------
# 2) run_backtest  (IMPL §6.1 -> §6.2 / §6.3)
# ---------------------------------------------------------------------------


def run_backtest(request: BacktestRequest) -> Union[BacktestResult, Dict[str, Any]]:
    """Execute a backtest and return a :class:`BacktestResult` on success.

    On a business failure (no data, bad params, rate limited, ...) return a
    ``business_failure(...)`` dict instead of raising — the template serializes
    it as ``result_status=failed`` with an UPPERCASE ``error_type``.

    TODO: replace the body below with a call into your real engine:
        1. Read generic params from ``request.backtest`` (symbol, market,
           timeframe, start_at/end_at, initial_capital, fee_bps, slippage_bps)
           and tool params from ``request.backtest.provider_params``.
        2. Map them to your engine's inputs and run it.
        3. Map the engine output to metrics / equity_curve / trades.

    The default implementation produces a deterministic "echo" result from the
    request so the smoke test passes out-of-the-box.
    """
    bt = request.backtest
    symbol = bt.symbol
    timeframe = bt.timeframe

    # --- Guard rails (map invalid input to standard UPPERCASE failure codes) ---
    if not symbol:
        return business_failure(
            error_type="INVALID_PARAMS",
            error_message="symbol is required",
            provider_name=PROVIDER_NAME,
        )
    if timeframe not in SUPPORTED_TIMEFRAMES:
        return business_failure(
            error_type="TIMEFRAME_UNSUPPORTED",
            error_message=(
                f"Unsupported timeframe: {timeframe}. Supported: {SUPPORTED_TIMEFRAMES}"
            ),
            provider_name=PROVIDER_NAME,
        )
    if bt.start_at is None or bt.end_at is None or bt.start_at >= bt.end_at:
        return business_failure(
            error_type="INVALID_PARAMS",
            error_message="start_at must be before end_at (unix seconds)",
            provider_name=PROVIDER_NAME,
        )

    try:
        initial_capital = parse_decimal(bt.initial_capital, default=Decimal("10000"))
        fee_bps = parse_decimal(bt.fee_bps, default=Decimal("0"))
        slippage_bps = parse_decimal(bt.slippage_bps, default=Decimal("0"))
    except ValueError as exc:
        return business_failure(
            error_type="INVALID_PARAMS",
            error_message=f"Cannot parse decimal fields: {exc}",
            provider_name=PROVIDER_NAME,
        )
    if initial_capital is None or initial_capital <= 0:
        return business_failure(
            error_type="INVALID_PARAMS",
            error_message="initial_capital must be positive",
            provider_name=PROVIDER_NAME,
        )

    # --- TODO: run your real engine here. Example "echo" result below. ---
    run_id = bt.run_id or "unknown"
    final_equity = initial_capital  # flat curve for the echo example

    result = BacktestResult(
        provider_run_id=f"example_{run_id}",
        initial_capital=decimal_str(initial_capital, places=2),
        metrics={
            # Ratio/percentage metrics are JSON numbers (IMPL §6.2).
            "total_return_pct": 0.0,
            "win_rate_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "trade_count": 0,
        },
        equity_curve=[
            {"t": bt.start_at, "equity": decimal_str(initial_capital, places=2)},
            {"t": bt.end_at, "equity": decimal_str(final_equity, places=2)},
        ],
        trades=[],
        assumptions={
            # Money/bps fields are decimal strings (IMPL §6.2).
            "fee_bps": decimal_str(fee_bps, places=4),
            "slippage_bps": decimal_str(slippage_bps, places=4),
            "data_source": "provider_reported",
            "real_market_data": True,
            "no_live_trading": True,
        },
        limitations={
            "verification": "external_unverified",
            "verified_by_cutie": False,
            "data_quality": "provider_reported",
            # IMPL §9.4: state whether the engine implemented the Cutie draft.
            "strategy_match": "provider_strategy_class_not_verified",
        },
        raw_report={
            "provider_summary": (
                f"Example echo run for {symbol} {timeframe}; replace adapter."
            ),
        },
    )

    report_url = build_report(result)
    if report_url is not None:
        result.report_url = report_url
        result.report_url_scope = "local_machine_only"
    return result


# ---------------------------------------------------------------------------
# 3) build_report  (IMPL §7)
# ---------------------------------------------------------------------------


def build_report(result: BacktestResult) -> Any:
    """Write a local report file and return its RELATIVE ``report_url``.

    Return ``None`` if you do not produce a report. The returned URL must be a
    relative path/ref (e.g. ``/reports/<run>.json``) — never a host/port or a
    local absolute filesystem path (IMPL §7). The template enforces retention.

    TODO: render your engine's native report (HTML/JSON) here. The default
    writes a small JSON summary so report retention can be exercised.
    """
    import json

    run_ref = result.provider_run_id or "report"
    filename = f"{run_ref}.json"
    body = json.dumps(
        {
            "metrics": result.metrics,
            "assumptions": result.assumptions,
            "limitations": result.limitations,
        },
        sort_keys=True,
    )
    return reports.write_report(filename, body)
