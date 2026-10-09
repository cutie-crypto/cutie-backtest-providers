"""P1a T2: EMA event semantics, independent golden and real execution."""
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
    DEFAULT_EXCHANGE, TOOL_SPECS, _build_ema_triple_alignment, _validate_params_against_schema,
)

TOOL = "local.backtesting_py.ema_triple_alignment"
FIXTURE = Path(__file__).parent / "fixtures/ema_triple_alignment_golden.json"
PARAMS = dict(ema_short=5, ema_mid=20, ema_long=60)


def _ema(closes, period):
    values = [float(closes[0])]
    alpha = 2 / (period + 1)
    for close in closes[1:]:
        values.append(alpha * close + (1 - alpha) * values[-1])
    return values


def _run(closes, params=None, opens=None):
    opens = closes if opens is None else opens
    df = pd.DataFrame(dict(Open=opens, Close=closes,
                           High=[max(o, c) + 1 for o, c in zip(opens, closes)],
                           Low=[min(o, c) - 1 for o, c in zip(opens, closes)],
                           Volume=[1] * len(closes)),
                      index=pd.date_range("2026-01-01", periods=len(closes), freq="h"))
    built = _build_ema_triple_alignment(PARAMS if params is None else params)
    return Backtest(df, built["strategy"], cash=100000, finalize_trades=False).run()


def test_golden_indicators_and_signals_match_independent_hand_formula():
    f = json.loads(FIXTURE.read_text())
    assert len(f["closes"]) >= 60
    values = [_ema(f["closes"], f[key]) for key in ("ema_short", "ema_mid", "ema_long")]
    for key, computed in zip(("ema_short", "ema_mid", "ema_long"), values):
        assert computed == pytest.approx(f[f"expected_{key}"], abs=1e-10)
    short, mid, long = values
    aligned = [s > m > l for s, m, l in zip(*values)]
    assert aligned == f["expected_aligned"]
    entries, exits, held = [], [], False
    for i in range(1, len(short)):
        if held and short[i] < mid[i]:
            exits.append(i)
            held = False
        elif not held and i + 1 >= f["min_bars"] and aligned[i] and not aligned[i - 1]:
            entries.append(i)
            held = True
    assert entries == f["expected_entries"] == [65, 122]
    assert exits == f["expected_exits"] == [80, 140]
    assert not held


def test_golden_matches_real_strategy_engine_execution_offset_by_one():
    f = json.loads(FIXTURE.read_text())
    result = _run(f["closes"], opens=f["opens"])
    trades = result["_trades"]
    assert list(trades.EntryBar) == [i + 1 for i in f["expected_entries"]]
    assert list(trades.ExitBar) == [i + 1 for i in f["expected_exits"]]
    for trade, entry, exit_ in zip(trades.itertuples(), f["expected_entries"], f["expected_exits"]):
        assert trade.EntryPrice == f["opens"][entry + 1]
        assert trade.ExitPrice == f["opens"][exit_ + 1]
        assert trade.EntryTime == pd.Timestamp("2026-01-01") + pd.Timedelta(hours=entry + 1)
        assert trade.ExitTime == pd.Timestamp("2026-01-01") + pd.Timedelta(hours=exit_ + 1)
        assert trade.Size > 0
    for key in ("ema_short", "ema_mid", "ema_long"):
        assert list(getattr(result["_strategy"], key)) == pytest.approx(f[f"expected_{key}"])
    assert not result["_strategy"].position


def test_sustained_alignment_does_not_reenter_after_risk_exit_until_reformed():
    closes = [100.] * 65 + [110., 110., 120.] + [120.] * 15 + [80.] * 50 + [130.] * 30 + [70.] * 10
    result = _run(closes, dict(PARAMS, take_profit_pct=1))
    trades = result["_trades"]
    assert len(trades) == 2
    assert trades.iloc[0].EntryBar == 66
    assert trades.iloc[0].ExitBar == 68
    strategy = result["_strategy"]
    aligned = [s > m > l for s, m, l in zip(strategy.ema_short, strategy.ema_mid, strategy.ema_long)]
    assert all(aligned[65:83])
    assert not any(aligned[100:133])
    assert trades.iloc[1].EntryBar > 133
    for bar in trades.EntryBar:
        assert aligned[bar - 1] and not aligned[bar - 2]


def test_alignment_formed_before_warmup_does_not_enter_late():
    result = _run([100. + i for i in range(90)])
    strategy = result["_strategy"]
    assert strategy.ema_short[-1] > strategy.ema_mid[-1] > strategy.ema_long[-1]
    assert result["_trades"].empty and not strategy.position


def test_equal_emas_do_not_enter():
    result = _run([100.] * 70)
    assert result["_trades"].empty and not result["_strategy"].position


def test_equal_short_mid_does_not_exit():
    # EMA7 alpha=1/4, EMA31 alpha=1/16: 88 -> 120 gives 96/90;
    # next close 66 makes both EMAs exactly 88.5 (dyadic arithmetic).
    closes = [88.] * 65 + [120., 66., 70., 70.]
    result = _run(closes, dict(ema_short=7, ema_mid=31, ema_long=60))
    strategy = result["_strategy"]
    assert strategy.ema_short[66] == strategy.ema_mid[66] == 88.5
    assert list(result["_trades"].EntryBar) == [66]
    assert list(result["_trades"].ExitBar) == [68]


def test_warmup_and_default_name():
    assert _build_ema_triple_alignment({})["min_bars"] == 120
    assert _build_ema_triple_alignment(PARAMS)["min_bars"] == 60
    assert _build_ema_triple_alignment({})["executed_name"] == "EMA Triple Alignment (20/60/120)"
    assert not _build_ema_triple_alignment({"stop_loss_pct": 5}).get("trade_on_close", False)
    result = _run([100.] * 40 + [120.] * 19)
    assert result["_trades"].empty and not result["_strategy"].position


def test_two_parameter_changes_produce_different_real_engine_trade_counts():
    closes = [100 + .01 * i + 10 * math.sin(i / 12) + 2 * math.sin(i / 3) for i in range(1000)]
    counts = [len(_run(closes, p)["_trades"]) for p in ({}, PARAMS, {"ema_short": 50, "ema_mid": 150, "ema_long": 300})]
    assert len(set(counts)) == 3, counts


@pytest.mark.parametrize("name,default,minimum,maximum", [
    ("ema_short", 20, 5, 50), ("ema_mid", 60, 20, 150), ("ema_long", 120, 60, 300),
])
def test_parameter_default_min_max_and_schema(name, default, minimum, maximum):
    props = TOOL_SPECS[TOOL]["param_schema_properties"]
    assert props[name] == dict(type="integer", default=default, minimum=minimum, maximum=maximum)
    for value in (default, minimum, maximum):
        assert _validate_params_against_schema({name: value}, props) is None
        # Keep the other legs ordered when exercising a bound.
        params = dict(ema_short=5, ema_mid=60, ema_long=300)
        if name == "ema_long":
            params["ema_mid"] = 20
        params[name] = value
        _build_ema_triple_alignment(params)
    for value in (minimum - 1, maximum + 1):
        assert _validate_params_against_schema({name: value}, props) is not None
        with pytest.raises(ValueError, match="INVALID_PARAMS"):
            _build_ema_triple_alignment({name: value})


@pytest.mark.parametrize("params", [
    {"ema_short": 20, "ema_mid": 20}, {"ema_short": 30, "ema_mid": 20},
    {"ema_mid": 120, "ema_long": 120}, {"ema_mid": 150, "ema_long": 120},
    {"ema_short": "bad"}, {"ema_mid": None}, {"ema_long": float("inf")},
])
def test_invalid_params_are_rejected(params):
    with pytest.raises(ValueError, match="INVALID_PARAMS"):
        _build_ema_triple_alignment(params)


def test_catalog_and_fixed_risk_schema_merge():
    spec = TOOL_SPECS[TOOL]
    assert spec["build"] is _build_ema_triple_alignment
    assert spec["strategy_family"] == "trend"
    assert spec["is_default"] is False
    assert "stop_loss_pct" in spec["param_schema_properties"]
    assert spec["param_schema_properties"]["exchange"] == dict(type="string", default=DEFAULT_EXCHANGE)
