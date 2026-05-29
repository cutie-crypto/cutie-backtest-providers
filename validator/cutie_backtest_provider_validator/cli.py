"""cutie-backtest-provider-validator CLI (IMPL W3.9 §8).

Validates that a Cutie backtest provider's HTTP response layer conforms to the
W3.9 v1 contract: /health, /catalog auth + schema, catalog tool fields, secret
scrub, smoke request/response shape, money-field precision, report_url policy,
and wrapper_type whitelist.

Two transport modes:
  - live HTTP:  --base-url http://127.0.0.1:8765 [--token ...]
  - in-process: --app /path/to/provider.py[:app]  (drives the FastAPI app via
                httpx ASGITransport; no running server needed)

Exit code: 0 when all checks pass, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import List, Optional

from .transport import ValidatorTransport
from .validator import ProviderValidator, SmokeParams, ValidationReport


def _parse_provider_params(raw: Optional[str]) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"--smoke-params is not valid JSON: {exc}")
    if not isinstance(parsed, dict):
        raise SystemExit("--smoke-params must be a JSON object")
    return parsed


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cutie-backtest-provider-validator",
        description="Validate a Cutie backtest provider against the W3.9 v1 contract.",
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--base-url",
        help="Provider base URL, e.g. http://127.0.0.1:8765 (live HTTP mode).",
    )
    src.add_argument(
        "--app",
        help="Path to a provider module exposing a FastAPI app, e.g. "
        "/path/to/cutie_backtesting_provider.py or module:attr (in-process mode).",
    )
    parser.add_argument("--token", default=None, help="Bearer token for /catalog and /cutie/backtest.")
    parser.add_argument("--tool-id", default=None, help="tool_id to smoke-test (default: catalog default/first).")
    parser.add_argument("--smoke-symbol", default="BTCUSDT", help="Smoke symbol (default BTCUSDT).")
    parser.add_argument("--smoke-timeframe", default="1h", help="Smoke timeframe (default 1h).")
    parser.add_argument("--smoke-market", default="spot", help="Smoke market (default spot).")
    parser.add_argument(
        "--start-at",
        type=int,
        default=None,
        help="Smoke range start (unix seconds). Default: 7 days before --end-at.",
    )
    parser.add_argument(
        "--end-at",
        type=int,
        default=None,
        help="Smoke range end (unix seconds). Default: now.",
    )
    parser.add_argument(
        "--smoke-params",
        default=None,
        help="JSON object of provider_params for the smoke run.",
    )
    parser.add_argument(
        "--instruction",
        default="",
        help="Optional human-readable instruction passed in the smoke envelope.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help="HTTP timeout in seconds (default 180).",
    )
    parser.add_argument("--json", action="store_true", help="Print only the machine-readable JSON report.")
    return parser


def _make_transport(args: argparse.Namespace) -> ValidatorTransport:
    if args.base_url:
        return ValidatorTransport.for_live(args.base_url, args.timeout)
    # --app path, optionally with :attr
    spec = args.app
    app_attr = "app"
    if ":" in spec and not spec[1:3] == ":\\":  # avoid splitting Windows drive
        module_part, app_attr = spec.rsplit(":", 1)
    else:
        module_part = spec
    return ValidatorTransport.for_asgi(module_part, app_attr, args.timeout)


def _format_summary(report: ValidationReport) -> str:
    lines: List[str] = []
    status = "PASS" if report.ok else "FAIL"
    lines.append("=" * 72)
    lines.append(f"cutie-backtest-provider-validator: {status}")
    lines.append(f"  base_url        : {report.base_url}")
    lines.append(f"  provider_schema : {report.provider_schema}")
    lines.append(f"  tools_checked   : {report.tools_checked}")
    lines.append("-" * 72)
    for c in report.checks:
        mark = "PASS" if c.passed else "FAIL"
        lines.append(f"  [{mark}] check {c.check_id:>2}: {c.name}")
        for info in c.info:
            lines.append(f"            . {info}")
        for w in c.warnings:
            lines.append(f"            ! WARN {w.get('code')}: {w.get('message')}")
        for err in c.errors:
            lines.append(f"            x {err}")
    lines.append("-" * 72)
    if report.warnings:
        lines.append(f"  {len(report.warnings)} warning(s):")
        for w in report.warnings:
            lines.append(f"    ! {w.get('code')}: {w.get('message')}")
    if report.errors:
        lines.append(f"  {len(report.errors)} error(s):")
        for err in report.errors:
            lines.append(f"    x {err}")
    if report.ok:
        lines.append("  Result: all checks passed.")
    else:
        lines.append("  Result: provider does NOT conform to the W3.9 v1 contract.")
    lines.append("=" * 72)
    return "\n".join(lines)


def run(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    end_at = args.end_at if args.end_at is not None else int(time.time())
    start_at = args.start_at if args.start_at is not None else end_at - 7 * 24 * 3600
    smoke = SmokeParams(
        tool_id=args.tool_id,
        symbol=args.smoke_symbol,
        timeframe=args.smoke_timeframe,
        market=args.smoke_market,
        start_at=start_at,
        end_at=end_at,
        provider_params=_parse_provider_params(args.smoke_params),
        instruction=args.instruction,
    )

    transport = _make_transport(args)
    try:
        validator = ProviderValidator(
            transport=transport,
            base_url=args.base_url or args.app,
            token=args.token,
            smoke=smoke,
        )
        report = validator.run()
    finally:
        transport.close()

    machine = report.to_machine_json()
    if args.json:
        print(json.dumps(machine, indent=2))
    else:
        print(_format_summary(report))
        print()
        print(json.dumps(machine, indent=2))

    return 0 if report.ok else 1


def main() -> None:  # console_scripts entry point
    sys.exit(run())


if __name__ == "__main__":
    main()
