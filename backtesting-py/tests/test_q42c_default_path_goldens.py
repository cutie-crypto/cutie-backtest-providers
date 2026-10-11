"""Q42-C2: bookkeeping for the default-path cells that are no longer pinned to the legacy risk layer.

The cells (red_streak_rsi / vwap_reversion / calendar_schedule requests with no pct of their own) run where they always did (the 15 frozen-baseline test modules); the rules that decide
and check them live in ``_q42c_default_path.py``. This file guards the registry:
  * the cell list and the recorded goldens describe exactly the same cells;
  * every listed cell is a real, currently collected test that carries ``legacy_risk_layer_default``;
  * every recorded response succeeded and has a stable summary (so a re-record cannot hide a failure).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import _q42c_default_path as reg

ROOT = Path(__file__).resolve().parents[1]


def test_cells_and_goldens_are_the_same_set():
    listed = reg.cells()
    assert len(listed) == 73
    assert set(reg.goldens()) == listed


def test_every_recorded_response_succeeded_with_a_summary():
    for node_id, responses in reg.goldens().items():
        assert responses, node_id
        for summary in responses:
            assert summary["status"] == "success", node_id
            assert summary["n_trades"] == len(summary["exits"]), node_id
            assert len(summary["sha256"]) == 64 and len(summary["disclosure_sha256"]) == 64, node_id


def test_every_listed_cell_is_collected_and_pinned_by_the_fixture():
    files = sorted({node.split("::")[0] for node in reg.cells()})
    out = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *files],
                         cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stdout[-500:] + out.stderr[-500:]
    collected = {line.strip() for line in out.stdout.splitlines() if "::" in line}
    missing = sorted(reg.cells() - collected)
    assert missing == []
    for path in files:
        module = (ROOT / path).read_text()
        assert "legacy_risk_layer_default" in module, path
