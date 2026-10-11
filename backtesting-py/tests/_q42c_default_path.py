"""Q42-C2: cells that run a template's own default stop / take-profit pct, judged on the new default layer.

red_streak_rsi, vwap_reversion and calendar_schedule fill their default pct in before the request is parsed.
After Q42-C that default is a stop_loss_pct / take_profit_pct the caller did not send, so the cell runs the
unified risk layer (intrabar High/Low touch). The frozen byte baselines were captured on the close-only
layer, so a cell that sends neither ``risk_layer_enabled`` nor a pct of its own must NOT be pinned back to the
legacy layer by ``legacy_risk_layer_default``.

``fixtures/q42c_default_path_cells.json`` lists those cells (pytest node ids of the pre-existing tests).
For a listed cell:
  * the fixture does not pin the legacy layer;
  * the frozen-baseline assertion in the original test body MUST fail (the behaviour really changed), and
  * every /cutie/backtest response the cell produced must match ``fixtures/q42c_default_path_goldens.json``,
    which was recorded at the Q42-C head (``Q42C_RECORD=1`` rewrites it).
Every other cell keeps the fixture and the frozen baseline untouched.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CELLS_FILE = FIXTURES / "q42c_default_path_cells.json"
GOLDEN_FILE = FIXTURES / "q42c_default_path_goldens.json"
V2_KEYS = ("result_status", "schema_version", "trades", "equity_curve", "metrics", "data_manifest")
DISCLOSURE_KEYS = ("assumptions", "raw_report")
RECORD = os.environ.get("Q42C_RECORD") == "1"
RECORDED: dict[str, list] = {}


def cells() -> set[str]:
    return set(json.loads(CELLS_FILE.read_text()))


def goldens() -> dict[str, list]:
    return json.loads(GOLDEN_FILE.read_text()) if GOLDEN_FILE.exists() else {}


def strip_trade_exit_kinds(value):
    """Hook for branches whose responses carry extra, additive exit-kind keys (identity on the Q42-C head)."""
    return value


def summarize(body: dict) -> dict:
    stable = {key: strip_trade_exit_kinds(body[key]) for key in V2_KEYS if key in body}
    canonical = json.dumps(stable, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    meta = json.dumps({key: strip_trade_exit_kinds(body.get(key)) for key in DISCLOSURE_KEYS},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    trades = body.get("trades") or []
    return {
        "status": body.get("result_status"),
        "n_trades": len(trades),
        "exits": [[t.get("side"), t.get("entry_price"), t.get("exit_price"), t.get("closed_at")] for t in trades],
        "sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "disclosure_sha256": hashlib.sha256(meta.encode()).hexdigest(),
    }


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    if item.nodeid not in cells():
        return (yield)
    from starlette.testclient import TestClient
    seen: list[dict] = []
    original = TestClient.request

    def spy(self, method, url, *args, **kwargs):
        response = original(self, method, url, *args, **kwargs)
        if str(url).endswith("/cutie/backtest"):
            seen.append(summarize(response.json()))
        return response

    TestClient.request = spy
    differs = False
    try:
        try:
            yield
        except AssertionError:
            differs = True
    finally:
        TestClient.request = original
    if not differs:
        pytest.fail(f"{item.nodeid}: default-path cell still equals the frozen legacy-layer bytes; "
                    "it should not be listed in q42c_default_path_cells.json")
    if not seen:
        pytest.fail(f"{item.nodeid}: no /cutie/backtest response was captured")
    bad = [s for s in seen if s["status"] != "success"]
    if bad:
        pytest.fail(f"{item.nodeid}: non-success response {bad[0]}")
    if RECORD:
        RECORDED[item.nodeid] = seen
        return None
    assert goldens().get(item.nodeid) == seen, f"{item.nodeid}: default-path response differs from the q42c golden"
    return None


def pytest_sessionfinish(session):
    if RECORD and RECORDED:
        merged = {**goldens(), **RECORDED}
        GOLDEN_FILE.write_text(json.dumps(merged, indent=1, sort_keys=True) + "\n")
