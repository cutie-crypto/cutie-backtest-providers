"""Report artifact helpers (IMPL §7).

A BYO provider may produce a local report file (HTML / JSON). The protocol rule
is strict:

- ``report_url`` returned to the connector MUST be a relative path / opaque ref
  (e.g. ``/reports/<run>.html``) — never a scheme/host/port or local absolute
  filesystem path. The connector treats it as ``local_machine_only``.
- The provider must enforce a retention policy (e.g. last 100 runs) so report
  files do not grow unbounded.

This module owns the report directory, retention pruning, and relative-path
``report_url`` construction so the adapter only has to write a file.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from . import settings

logger = logging.getLogger("cutie_byo_provider.reports")

# URL prefix under which reports are served (see app.py /reports/{filename}).
REPORT_URL_PREFIX = "/reports"

# Only allow safe filename characters; everything else is stripped.
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9_.\-]")


def ensure_reports_dir() -> Path:
    """Create and return the reports directory."""
    settings.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    return settings.REPORTS_DIR


def safe_filename(name: str) -> str:
    """Sanitize a filename: strip path separators and unsafe characters."""
    base = Path(name).name  # drop any directory component
    cleaned = _SAFE_FILENAME.sub("_", base)
    return cleaned or "report"


def report_path(filename: str) -> Path:
    """Resolve a filename inside the reports directory (no traversal)."""
    return ensure_reports_dir() / safe_filename(filename)


def report_url(filename: str) -> str:
    """Build the relative report_url for a report file (IMPL §7).

    Always a relative path — no scheme / host / port / absolute path.
    """
    return f"{REPORT_URL_PREFIX}/{safe_filename(filename)}"


def enforce_retention(suffixes: Optional[tuple] = None) -> None:
    """Keep at most ``settings.MAX_REPORTS`` files, deleting oldest by mtime.

    ``suffixes`` optionally restricts pruning to specific extensions
    (e.g. ``(".html", ".json")``). When ``None`` all files are considered.
    """
    reports_dir = settings.REPORTS_DIR
    if not reports_dir.exists():
        return
    files = [
        f
        for f in reports_dir.iterdir()
        if f.is_file()
        and not f.name.startswith(".")
        and (suffixes is None or f.suffix in suffixes)
    ]
    files.sort(key=lambda f: f.stat().st_mtime)
    while len(files) > settings.MAX_REPORTS:
        oldest = files.pop(0)
        try:
            oldest.unlink()
        except OSError as exc:  # pragma: no cover - filesystem race
            logger.warning("Failed to prune old report %s: %s", oldest, exc)


def write_report(filename: str, content: str) -> str:
    """Write a text report and return its relative ``report_url``.

    Convenience for adapters that build an HTML / JSON report string. Enforces
    retention after writing.
    """
    path = report_path(filename)
    path.write_text(content, encoding="utf-8")
    enforce_retention()
    return report_url(path.name)
