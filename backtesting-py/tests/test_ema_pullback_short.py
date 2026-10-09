"""SHORT-T1: independent recurrence, frozen fills, arming and strict boundaries."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import pandas as pd
import pytest
from backtesting import Backtest

from test_ema_pullback_template import _ema, _frame, _fixture as long_fixture
from cutie_backtesting_provider import (
    TOOL_SPECS, _build_ema_pullback, _validate_params_against_schema,
)

PARAMS = dict(ema_fast=5, ema_slow=30, pullback_tolerance_pct=1, direction="short")
FIXTURE = Path(__file__).parent / "fixtures" / "ema_pullback_short_golden.json"


def fixture():
    return json.loads(FIXTURE.read_text())


def run(closes, highs=None, lows=None, params=None):
    return Backtest(_frame(closes, lows, highs),
                    _build_ema_pullback(PARAMS if params is None else params)["strategy"],
                    cash=100000, finalize_trades=False).run()


def reference(f):
    fast, slow = _ema(f["closes"], f["ema_fast"]), _ema(f["closes"], f["ema_slow"])
    zones = [v * (1 - f["pullback_tolerance_pct"] / 100) for v in fast]
    armed, held = True, False
    entries, exits, blocked, states = [], [], [], []
    for i, close in enumerate(f["closes"]):
        if held:
            if close > slow[i]:
                exits.append(i)
                held = False
        elif i + 1 >= f["min_bars"]:
            candidate = fast[i] < slow[i] and f["highs"][i] >= zones[i] and close < fast[i]
            if armed and candidate:
                entries.append(i)
                held, armed = True, False
            else:
                if candidate and not armed:
                    blocked.append(i)
                if close < zones[i]:
                    armed = True
        states.append(armed)
    return fast, slow, zones, entries, exits, blocked, states


def test_short_golden_indicators_and_hand_calculated_fills():
    f = fixture()
    assert len(f["closes"]) == 65
    fast, slow, zones, entries, exits, blocked, states = reference(f)
    for computed, name in ((fast, "ema_fast"), (slow, "ema_slow"), (zones, "zone")):
        assert computed == pytest.approx(f[f"expected_{name}"], rel=0, abs=1e-10)
    assert entries == f["expected_entries"] == [29, 36]
    assert exits == f["expected_exits"] == [32, 38]
    assert blocked == f["expected_blocked_entries"] == [33, 34, 35]
    assert states == f["expected_armed"]
    result = run(f["closes"], f["highs"], f["lows"])
    trades = result["_trades"]
    assert len(trades) == 2
    for (_, trade), expected in zip(trades.iterrows(), f["expected_trades"]):
        assert [trade.EntryBar, trade.ExitBar, trade.EntryPrice, trade.ExitPrice,
                trade.Size, trade.PnL] == pytest.approx(list(expected.values()), rel=0, abs=1e-10)
    assert result["Equity Final [$]"] == pytest.approx(45190.5)
    assert not result["_strategy"].position


def test_short_rearms_only_on_later_flat_close_below_zone():
    f = fixture()
    result = run(f["closes"], f["highs"], f["lows"])
    _, _, zones, _, _, _, armed = reference(f)
    assert f["closes"][30] < zones[30] and not armed[30]  # held: cannot rearm
    assert all(f["closes"][i] >= zones[i] and not armed[i] for i in (33, 34))
    assert f["closes"][35] < zones[35] and armed[35]  # no same-bar entry
    assert result["_trades"][["EntryBar", "ExitBar"]].values.tolist() == [[30, 33], [37, 39]]


def test_short_same_segment_cannot_enter_again_without_rearm():
    f = fixture()
    f["closes"][35:] = [86.4] * (len(f["closes"]) - 35)
    f["highs"][35:] = [100.0] * (len(f["closes"]) - 35)
    fast, slow, zones, entries, _, blocked, armed = reference(f)
    assert all(fast[i] < slow[i] and f["closes"][i] < fast[i] for i in (35, 36, 37))
    assert all(f["closes"][i] >= zones[i] and not armed[i] for i in (35, 36, 37))
    assert entries == [29] and {35, 36, 37}.issubset(blocked)
    # Rebuild valid OHLC around the changed closes; highs still touch the zone.
    result = run(f["closes"], [max(c, h) for c, h in zip(f["closes"], f["highs"])],
                 [min(c, lo) for c, lo in zip(f["closes"], f["lows"])])
    assert list(result["_trades"].EntryBar) == [30]
    assert not result["_strategy"].position


def test_long_golden_price_reflection_has_identical_entry_exit_bars():
    f = long_fixture()
    axis = 2 * f["closes"][0]
    long = run(f["closes"], f["highs"], f["lows"], {**PARAMS, "direction": "long"})
    short = run([axis - c for c in f["closes"]], [axis - lo for lo in f["lows"]],
                [axis - hi for hi in f["highs"]])
    # Percentage zones are not affine-invariant; this fixture remains away from
    # the changed zone boundaries. EMA reflection uses absolute tolerance 1e-10.
    assert short["_trades"][["EntryBar", "ExitBar"]].equals(long["_trades"][["EntryBar", "ExitBar"]])
    assert len(short["_trades"]) == 2 and all(short["_trades"].Size < 0)
    assert short["_trades"].EntryPrice.tolist() == pytest.approx([axis - p for p in long["_trades"].EntryPrice], rel=0, abs=1e-10)
    assert short["_trades"].ExitPrice.tolist() == pytest.approx([axis - p for p in long["_trades"].ExitPrice], rel=0, abs=1e-10)
    assert list(short["_strategy"].ema_fast) == pytest.approx([axis - v for v in long["_strategy"].ema_fast], rel=0, abs=1e-10)


def test_short_close_equal_fast_does_not_enter():
    closes = [192.0] * 29 + [96.0, 160.0, 160.0, 160.0]
    highs = list(closes)
    highs[30:] = [192.0] * 3
    result = run(closes, highs)
    assert result["_strategy"].ema_fast[30] == closes[30] == 160.0
    assert result["_strategy"].ema_fast[30] < result["_strategy"].ema_slow[30]
    assert result["_trades"].empty and not result["_strategy"].position


@pytest.mark.parametrize("touches", [True, False])
def test_short_high_equal_zone_enters_but_just_below_does_not(touches):
    closes = [192.0] * 29 + [96.0, 96.0, 192.0, 192.0]
    zone = _ema(closes, 5)[29] * 0.99
    assert zone == 158.4
    highs = list(closes)
    highs[29] = zone if touches else math.nextafter(zone, -math.inf)
    result = run(closes, highs)
    assert list(result["_trades"].EntryBar) == ([30] if touches else [])


def test_short_close_equal_slow_does_not_exit():
    closes = [384.0] * 30 + [128.0, 368.0, 368.0, 384.0, 384.0]
    result = run(closes, [384.0] * len(closes), params={**PARAMS, "ema_slow": 31})
    assert result["_strategy"].ema_slow[31] == closes[31] == 368.0
    assert result["_trades"][["EntryBar", "ExitBar"]].values.tolist() == [[31, 34]]


@pytest.mark.parametrize("risk_params,signal_close", [
    ({"stop_loss_pct": 5}, 116.0), ({"take_profit_pct": 5}, 90.0),
])
def test_short_fixed_risk_exits_before_trend_exit(risk_params, signal_close):
    closes = [192.0] * 29 + [96.0, 96.0, signal_close, signal_close]
    result = run(closes, [192.0] * len(closes), params={**PARAMS, **risk_params})
    assert signal_close < result["_strategy"].ema_slow[31]
    assert result["_trades"][["EntryBar", "ExitBar"]].values.tolist() == [[30, 32]]
    assert all(result["_trades"].Size < 0)


@pytest.mark.parametrize("direction", ["both", "SHORT", "", None, 1, True, [], {}])
def test_invalid_direction_is_invalid_params(direction):
    schema = TOOL_SPECS["local.backtesting_py.ema_pullback"]["param_schema_properties"]
    assert _validate_params_against_schema({"direction": direction}, schema)
    with pytest.raises(ValueError, match="INVALID_PARAMS:direction"):
        _build_ema_pullback({**PARAMS, "direction": direction})


def test_direction_schema_and_default_long_output_are_explicit():
    spec = TOOL_SPECS["local.backtesting_py.ema_pullback"]
    assert spec["param_schema_properties"]["direction"] == dict(type="string", enum=["long", "short"], default="long")
    f = long_fixture()
    params = {k: v for k, v in PARAMS.items() if k != "direction"}
    default = run(f["closes"], f["highs"], f["lows"], params)
    explicit = run(f["closes"], f["highs"], f["lows"], {**params, "direction": "long"})
    serialized = default["_trades"].to_json() + "\n" + default["_equity_curve"].to_json()
    # Pinned from unmodified 9e36a2e before adding the direction branch.
    assert hashlib.sha256(serialized.encode()).hexdigest() == "10fa8ac990f6d360f693ab1c16e1a98a54080f1c62bfebe7a36c0fd5478f03d0"
    assert default["_trades"].to_json() == explicit["_trades"].to_json()
    assert default["_equity_curve"].to_json() == explicit["_equity_curve"].to_json()
    assert _build_ema_pullback(params)["executed_name"] == "EMA Pullback (5/30, tolerance=1%)"
    assert _build_ema_pullback(PARAMS)["executed_name"] == "EMA Pullback (5/30, tolerance=1%) Short"


@pytest.mark.parametrize("closes", [[100.0] * 40, [192.0] * 20 + [96.0] * 9])
def test_short_equal_emas_and_early_pullback_do_not_enter(closes):
    result = run(closes, [192.0] * len(closes))
    assert result["_trades"].empty and not result["_strategy"].position
