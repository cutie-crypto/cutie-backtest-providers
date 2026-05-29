"""Runtime settings for the Cutie BYO backtest provider template.

All configuration is read from environment variables so the same wrapper can be
started on an OpenClaw / Hermes machine without editing code.

Environment variables
----------------------
CUTIE_BACKTEST_PROVIDER_TOKEN
    Bearer token required for ``/catalog`` and ``/cutie/backtest``. When unset
    the provider runs in dev mode and accepts any request (a startup warning is
    logged). Always set this in production.
CUTIE_BACKTEST_PORT
    Port used by the ``__main__`` uvicorn entrypoint. Default ``8767``.
CUTIE_BACKTEST_HOST
    Bind host for the ``__main__`` uvicorn entrypoint. Default ``127.0.0.1``.
    Keep this on loopback or a private-network address; the Cutie Connector only
    talks to localhost / RFC1918 providers (IMPL §12).
CUTIE_BACKTEST_REPORTS_DIR
    Directory where generated report files are stored. Default ``./reports``
    next to the package. Reports are pruned to ``CUTIE_BACKTEST_MAX_REPORTS``.
CUTIE_BACKTEST_MAX_REPORTS
    Maximum number of report files to retain. Default ``100``.
"""

from __future__ import annotations

import os
from pathlib import Path

# Bearer token. Empty string => dev mode (no auth).
PROVIDER_TOKEN: str = os.environ.get("CUTIE_BACKTEST_PROVIDER_TOKEN", "")

# Bind address for the __main__ entrypoint. Loopback by default (IMPL §12).
HOST: str = os.environ.get("CUTIE_BACKTEST_HOST", "127.0.0.1")
PORT: int = int(os.environ.get("CUTIE_BACKTEST_PORT", "8767"))

# Report retention.
_DEFAULT_REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"
REPORTS_DIR: Path = Path(
    os.environ.get("CUTIE_BACKTEST_REPORTS_DIR", str(_DEFAULT_REPORTS_DIR))
)
MAX_REPORTS: int = int(os.environ.get("CUTIE_BACKTEST_MAX_REPORTS", "100"))
