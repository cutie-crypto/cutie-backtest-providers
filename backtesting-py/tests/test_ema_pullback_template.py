"""R1-T1: independent EMA recurrence, signal-state golden and real engine checks.

Fixture bar indices are zero-based close-confirmed signals. Orders fill on the
next open; the reference loop never imports the provider's indicator or strategy.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import pytest
from backtesting import Backtest

from cutie_backtesting_provider import (
    DEFAULT_EXCHANGE,
    TOOL_SPECS,
    _build_ema_pullback,
    _validate_params_against_schema,
    _catalog_tool,
)

TOOL = "local.backtesting_py.ema_pullback"
FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "ema_pullback_golden.json"
PARAMS = dict(ema_fast=5, ema_slow=30, pullback_tolerance_pct=1)


def _fixture():
    return json.loads(FIXTURE_PATH.read_text())


def _ema(closes, period):
    """EMA[0] = close[0]; EMA[t] = alpha*close[t] + (1-alpha)*EMA[t-1]."""
    alpha = 2 / (period + 1)
    values = [float(closes[0])]
    for close in closes[1:]:
        values.append(alpha * close + (1 - alpha) * values[-1])
    return values


def _reference(f):
    fast = _ema(f["closes"], f["ema_fast"])
    slow = _ema(f["closes"], f["ema_slow"])
    zones = [v * (1 + f["pullback_tolerance_pct"] / 100) for v in fast]
    armed, held = True, False
    entries, exits, blocked, armed_values = [], [], [], []
    for i, close in enumerate(f["closes"]):
        if held:
            if close < slow[i]:
                exits.append(i)
                held = False
        elif i + 1 >= f["min_bars"]:
            candidate = fast[i] > slow[i] and f["lows"][i] <= zones[i] and close > fast[i]
            if armed and candidate:
                entries.append(i)
                held, armed = True, False
            else:
                if candidate and not armed:
                    blocked.append(i)
                if close > zones[i]:
                    armed = True
        armed_values.append(armed)
    return fast, slow, zones, entries, exits, blocked, armed_values


def _frame(closes, lows=None, highs=None):
    return pd.DataFrame(
        dict(Open=closes, High=highs or [v + 1 for v in closes],
             Low=lows if lows is not None else [v - 1 for v in closes],
             Close=closes, Volume=[1] * len(closes)),
        index=pd.date_range("2026-01-01", periods=len(closes), freq="h"),
    )


def _run(closes, params=None, lows=None, highs=None):
    built = _build_ema_pullback(PARAMS if params is None else params)
    return Backtest(_frame(closes, lows, highs), built["strategy"], cash=100000,
                    finalize_trades=False).run()


def test_golden_indicators_and_state_match_independent_hand_formula():
    f = _fixture()
    fast, slow, zones, entries, exits, blocked, armed = _reference(f)
    for computed, name in ((fast, "ema_fast"), (slow, "ema_slow"), (zones, "zone")):
        assert computed == pytest.approx(f[f"expected_{name}"], abs=1e-10)
    assert entries == f["expected_entries"] == [29, 36]
    assert exits == f["expected_exits"] == [32, 38]
    assert blocked == f["expected_blocked_entries"] == [33, 34, 35]
    assert armed == f["expected_armed"]
    # Held bars above the zone cannot rearm; flat closes inside it cannot either.
    assert f["closes"][30] > zones[30] and armed[30] is False
    assert f["closes"][33] <= zones[33] and armed[33] is False
    assert f["closes"][34] <= zones[34] and armed[34] is False
    # Bar 35 satisfies entry conditions but only rearms for the following bar.
    assert f["closes"][35] > zones[35] and armed[35] is True


def test_golden_matches_real_strategy_engine_execution_offset_by_one():
    f = _fixture()
    result = _run(f["closes"], lows=f["lows"], highs=f["highs"])
    trades = result["_trades"]
    assert list(trades.EntryBar) == [i + 1 for i in f["expected_entries"]]
    assert list(trades.ExitBar) == [i + 1 for i in f["expected_exits"]]
    assert all(trades.Size > 0)
    assert not result["_strategy"].position
    for name in ("ema_fast", "ema_slow"):
        assert list(getattr(result["_strategy"], name)) == pytest.approx(f[f"expected_{name}"])


def test_two_parameter_changes_produce_different_real_engine_trade_counts():
    closes = [100 + 0.01 * i + 10 * math.sin(i / 12) + 2 * math.sin(i / 3)
              for i in range(600)]
    counts = [len(_run(closes, params=params)["_trades"]) for params in (
        {}, {"pullback_tolerance_pct": 0}, {"ema_fast": 50, "ema_slow": 200},
    )]
    assert counts[0] > 0
    assert len(set(counts)) == 3, counts


@pytest.mark.parametrize("risk_params,signal_close", [
    ({"stop_loss_pct": 5}, 180.0), ({"take_profit_pct": 5}, 204.0),
])
def test_fixed_risk_exits_before_trend_exit(risk_params, signal_close):
    closes = [96.0] * 29 + [192.0, 192.0, signal_close, signal_close]
    result = _run(closes, dict(PARAMS, **risk_params), lows=[96.0] * len(closes))
    assert signal_close > result["_strategy"].ema_slow[31]
    assert list(result["_trades"].EntryBar) == [30]
    assert list(result["_trades"].ExitBar) == [32]


@pytest.mark.parametrize("touches", [True, False])
def test_low_equal_zone_enters_but_just_above_does_not(touches):
    closes = [96.0] * 29 + [192.0, 192.0, 96.0, 96.0]
    fast = _ema(closes, 5)
    assert fast[29] == 128.0
    zone = fast[29] * 1.01
    lows = list(closes)
    lows[29] = zone if touches else math.nextafter(zone, math.inf)
    result = _run(closes, lows=lows)
    assert list(result["_trades"].EntryBar) == ([30] if touches else [])


def test_close_equal_fast_does_not_enter():
    closes = [96.0] * 29 + [192.0, 128.0, 128.0, 128.0]
    lows = list(closes)
    lows[30:] = [96.0] * 3
    result = _run(closes, lows=lows)
    strategy = result["_strategy"]
    assert strategy.ema_fast[30] == closes[30] == 128.0
    assert strategy.ema_fast[30] > strategy.ema_slow[30]
    assert result["_trades"].empty
    assert not strategy.position


def test_close_equal_slow_does_not_exit():
    closes = [96.0] * 30 + [352.0, 112.0, 112.0, 96.0, 96.0]
    lows = [96.0] * len(closes)
    result = _run(closes, dict(PARAMS, ema_slow=31), lows=lows)
    assert result["_strategy"].ema_slow[31] == closes[31] == 112.0
    assert list(result["_trades"].EntryBar) == [31]
    assert list(result["_trades"].ExitBar) == [34]


def test_equal_emas_do_not_enter():
    result = _run([100.0] * 40, lows=[99.0] * 40)
    assert result["_trades"].empty
    assert not result["_strategy"].position


def test_warmup_guard_blocks_early_pullback():
    result = _run([100.0] * 20 + [120.0] * 9, lows=[90.0] * 29)
    assert result["_trades"].empty
    assert not result["_strategy"].position
    assert _build_ema_pullback(PARAMS)["min_bars"] == 30
    assert _build_ema_pullback({})["min_bars"] == 60


@pytest.mark.parametrize("params", [
    {"ema_fast": 30, "ema_slow": 30}, {"ema_fast": 50, "ema_slow": 30},
    {"ema_fast": 4}, {"ema_fast": 51}, {"ema_slow": 29}, {"ema_slow": 201},
    {"pullback_tolerance_pct": -0.1}, {"pullback_tolerance_pct": 1.1},
    {"pullback_tolerance_pct": float("nan")}, {"ema_fast": "bad"},
    {"ema_slow": None}, {"pullback_tolerance_pct": "bad"},
])
def test_invalid_params_are_rejected(params):
    with pytest.raises(ValueError, match="INVALID_PARAMS"):
        _build_ema_pullback(params)


def test_tool_schema_and_catalog():
    spec = TOOL_SPECS[TOOL]
    assert spec["build"] is _build_ema_pullback
    assert spec["strategy_family"] == "trend"
    assert spec["is_default"] is False
    props = spec["param_schema_properties"]
    for name, kind, default, minimum, maximum in (
        ("ema_fast", "integer", 20, 5, 50),
        ("ema_slow", "integer", 60, 30, 200),
        ("pullback_tolerance_pct", "number", 0.2, 0, 1),
    ):
        assert props[name] == dict(type=kind, default=default, minimum=minimum, maximum=maximum)
        assert _validate_params_against_schema({name: minimum - 1}, props) is not None
        assert _validate_params_against_schema({name: maximum + 1}, props) is not None
        assert _validate_params_against_schema({name: minimum}, props) is None
        assert _validate_params_against_schema({name: maximum}, props) is None
    assert props["exchange"] == dict(type="string", default=DEFAULT_EXCHANGE)
    assert _validate_params_against_schema({"ema_fast": 5.5}, props) is not None
    for key in ("stop_loss_pct", "take_profit_pct", "position_size_pct", "position_size_notional"):
        assert key in props
    catalog_entry = _catalog_tool(TOOL, spec, ["BTC/USDT"])
    assert catalog_entry["tool_id"] == TOOL
    assert catalog_entry["param_schema"]["properties"] == props
