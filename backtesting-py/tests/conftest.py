"""Shared fixtures.

Q42-C changed the default of ``risk_layer_enabled``: a request carrying ``stop_loss_pct`` or
``take_profit_pct`` without the key now runs the unified risk layer (intrabar High/Low touch).
The immutable byte baselines were captured on the close-only legacy layer, which is still reachable
through an explicit ``risk_layer_enabled=false``. ``legacy_risk_layer_default`` makes the parser treat
an absent key as that explicit false, so those baselines keep pinning the legacy layer bit for bit.
It never loosens an assertion; the new default is covered by test_q42c_risk_layer_default.py.

Q42-C2: cells whose template fills in its own default pct (red_streak_rsi / vwap_reversion / calendar_schedule
with no pct and no risk_layer_enabled in the request) are listed in fixtures/q42c_default_path_cells.json. The
fixture leaves those unpinned and _q42c_default_path.py asserts the new behaviour instead (see its docstring).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _q42c_default_path as q42c_default_path  # noqa: E402
from _q42c_default_path import pytest_runtest_call, pytest_sessionfinish  # noqa: E402,F401  (Q42-C2 plugin hooks)


@pytest.fixture
def legacy_risk_layer_default(request, monkeypatch):
    if request.node.nodeid in q42c_default_path.cells():
        yield
        return
    import cutie_backtesting_provider as provider
    original = provider._parse_fixed_risk_params

    def parse_with_explicit_false(params, **kwargs):
        return original({"risk_layer_enabled": False, **params}, **kwargs)

    monkeypatch.setattr(provider, "_parse_fixed_risk_params", parse_with_explicit_false)
    yield
