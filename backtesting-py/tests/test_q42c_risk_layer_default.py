"""Q42-C: a request that carries stop_loss_pct / take_profit_pct without risk_layer_enabled now runs the
unified risk layer (intrabar High/Low touch, exit at the next open); an explicit false keeps the close-only
legacy layer; a request with neither key is byte-identical to before.

Server-side nothing changes: this batch only touches the provider. The S4-S None cases QA asked for are
out of scope for this batch.
"""
from __future__ import annotations

import hashlib
import json

import pandas as pd
import pytest
from backtesting import Backtest

import test_9t5_fibonacci as fib
import test_f2_calendar as cal
import test_f3_us_open as us
import test_f4_cme_gap as cme
import test_f5_vwap_reversion as vwap
import test_f6_streak_rsi as streak
from test_ema_pullback_template import PARAMS as EMA_PARAMS, _run as ema_run
from test_risk_layer import p

LEGACY = {"risk_layer_enabled": False}
N = 40


def _bars(lows=None, highs=None):
    closes = [96.0] * 29 + [192.0] * (N - 29)
    base_lows = [96.0] * 30 + [191.0] * (N - 30)
    base_highs = [97.0] * 29 + [193.0] * (N - 29)
    for i, v in (lows or {}).items():
        base_lows[i] = v
    for i, v in (highs or {}).items():
        base_highs[i] = v
    return closes, base_lows, base_highs


def _ema(params, **wicks):
    closes, lows, highs = _bars(**wicks)
    result = ema_run(closes, {**EMA_PARAMS, **params}, lows=lows, highs=highs)
    return result["_trades"][["EntryBar", "ExitBar"]].values.tolist()


def _flag(tool, params):
    return bool(p.TOOL_SPECS["local.backtesting_py." + tool]["build"](params)["strategy"]._risk.get("risk_layer_enabled"))


def test_default_stop_judged_on_low_not_close():
    # Bar 31 wicks to 150 (-22%) and closes back at 192, above the 2% stop (188.16).
    assert _ema({"stop_loss_pct": 2}, lows={31: 150.0}) == [[30, 32]]


def test_explicit_false_keeps_close_only_stop():
    # The wick is ignored and the close never breaks the stop: the position is still open (finalize_trades=False).
    assert _ema({"stop_loss_pct": 2, **LEGACY}, lows={31: 150.0}) == []


def test_take_profit_only_judged_on_high():
    assert _ema({"take_profit_pct": 5}, highs={31: 260.0}) == [[30, 32]]
    assert _ema({"take_profit_pct": 5, **LEGACY}, highs={31: 260.0}) == []


def test_neither_key_is_byte_identical_to_legacy():
    closes, lows, highs = _bars(lows={31: 150.0})
    absent = ema_run(closes, dict(EMA_PARAMS), lows=lows, highs=highs)
    explicit = ema_run(closes, {**EMA_PARAMS, **LEGACY}, lows=lows, highs=highs)

    def digest(r):
        blob = r["_trades"].to_json() + "\n" + r["_equity_curve"].to_json()
        return hashlib.sha256(blob.encode()).hexdigest()
    assert digest(absent) == digest(explicit)
    assert not p._parse_fixed_risk_params({}).get("risk_layer_enabled")
    assert p._parse_fixed_risk_params({}) == {}


@pytest.mark.parametrize("params,expected", [
    ({}, False),
    ({"stop_loss_pct": 2}, True),
    ({"take_profit_pct": 5}, True),
    ({"stop_loss_pct": 2, "risk_layer_enabled": False}, False),
    ({"stop_loss_pct": 2, "risk_layer_enabled": True}, True),
    ({"risk_layer_enabled": True}, True),
])
def test_effective_flag_matrix(params, expected):
    assert bool(p._parse_fixed_risk_params(params).get("risk_layer_enabled")) is expected


@pytest.mark.parametrize("base,extra", [
    ({"stop_loss_pct": 2}, {"trailing_stop_pct": 5}),
    ({"stop_loss_pct": 2}, {"breakeven_stop": True}),
    ({"stop_loss_pct": 2}, {"take_profit_r": 2}),
    ({"take_profit_pct": 5}, {"atr_stop_multiplier": 2, "risk_atr_period": 14}),
    ({"stop_loss_pct": 2}, {"max_holding_bars": 3}),
])
def test_dynamic_keys_still_require_explicit_enable(base, extra):
    # The default only covers the two plain keys; it never silently enables ATR / trailing / holding limits.
    with pytest.raises(ValueError, match="INVALID_PARAMS"):
        p._parse_fixed_risk_params({**base, **extra})
    assert p._parse_fixed_risk_params({**base, **extra, "risk_layer_enabled": True})["risk_layer_enabled"] is True


def test_turtle_keeps_explicit_enable_requirement():
    with pytest.raises(ValueError, match="require risk_layer_enabled=true"):
        p._parse_turtle_risk_params({"take_profit_pct": 5})
    with pytest.raises(ValueError, match="Turtle does not support"):
        p._parse_turtle_risk_params({"stop_loss_pct": 2})
    assert p._parse_turtle_risk_params({}) == {}


# ---- the six templates -------------------------------------------------------------------------------

def _trades(stats):
    return stats["_trades"][["EntryBar", "ExitBar"]].values.tolist()


def test_calendar_schedule_template_default_stop_flag_on_behavior_unchanged():
    # The template always judged its frozen stop intrabar; only the reported flag changes.
    data = cal.frame()
    data.iloc[3, data.columns.get_loc("Low")] = 50.0
    params = dict(cal.SCHEDULE, time_max_holding_minutes=360)
    assert _flag("calendar_schedule", {}) is True
    assert _flag("calendar_schedule", {"calendar_stop_enabled": False}) is False
    assert _trades(cal.direct(data, params)) == _trades(cal.direct(data, {**params, **LEGACY})) == [[2, 4]]


def test_us_open_momentum_behavior_unchanged():
    data = us.frame()
    assert _trades(us.run(data)) == _trades(us.run(data, LEGACY))


def test_cme_weekend_gap_intrinsic_stop_unchanged():
    # The intrinsic 2% stop is added after parsing and already forces the layer on: no flag flip to observe.
    assert _flag("cme_weekend_gap", {}) is False
    assert _flag("cme_weekend_gap", {"stop_loss_pct": 3}) is True


def test_red_streak_rsi_default_group_runs_unified_layer():
    data = streak.frame()
    data.iloc[46, data.columns.get_loc("Low")] = 80.0  # wick through the 3% stop; close stays at 92
    assert streak.run({}, data)["_strategy"]._risk["risk_layer_enabled"] is True
    assert list(streak.run({}, data)["_trades"].ExitBar) == [47]
    assert list(streak.run(LEGACY, data)["_trades"].ExitBar) == [56]


def test_vwap_reversion_default_stop_preempts_day_end_flatten():
    new, old = vwap.run(), vwap.run(LEGACY)
    assert _trades(new) == _trades(old) == [[3, 4], [6, 7]]
    # Bar 3's low (88) is through the 2% stop (92.12): the stop decides, so no utc_day_end expiry is recorded.
    assert new["_strategy"]._f5_expiries == []
    assert old["_strategy"]._f5_expiries == [dict(decision_at=1767312000, reason="utc_day_end")]


def test_fibonacci_retracement_user_stop_runs_unified_layer():
    data = fib.frame()
    new = fib.run(data, {"stop_loss_pct": 2})
    old = fib.run(data, {"stop_loss_pct": 2, **LEGACY})
    assert _trades(new) == [[9, 11]]
    assert _trades(old) == [[9, 13]]


def test_fibonacci_intrinsic_stop_unchanged():
    # No user pricing key => the wave's own frozen stop, always judged intrabar: nothing to flip.
    data = fib.frame()
    assert _trades(fib.run(data)) == _trades(fib.run(data, LEGACY))
