"""P1a T1: independent EMA recurrence, close signals and next-open fills."""
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
    DEFAULT_EXCHANGE, TOOL_SPECS, _build_bias_reversion, _validate_params_against_schema,
)

TOOL = "local.backtesting_py.bias_reversion"
FIXTURE = Path(__file__).parent / "fixtures/bias_reversion_golden.json"


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
    built = _build_bias_reversion({} if params is None else params)
    return Backtest(df, built["strategy"], cash=100000, finalize_trades=False).run()


def test_golden_indicators_and_signals_match_independent_hand_formula():
    f = json.loads(FIXTURE.read_text())
    assert len(f["closes"]) >= 60
    ema = _ema(f["closes"], f["ema_period"])
    bias = [(c - e) / e * 100 for c, e in zip(f["closes"], ema)]
    assert ema == pytest.approx(f["expected_ema"], abs=1e-10)
    assert bias == pytest.approx(f["expected_bias"], abs=1e-10)
    entries, exits, held = [], [], False
    for i, value in enumerate(bias):
        if held and value >= 0:
            exits.append(i)
            held = False
        elif not held and i + 1 >= f["min_bars"] and value <= -f["bias_entry_pct"]:
            entries.append(i)
            held = True
    assert entries == f["expected_entries"] == [25, 45]
    assert exits == f["expected_exits"] == [28, 48]
    assert not held


def test_golden_matches_real_strategy_engine_execution_offset_by_one():
    f = json.loads(FIXTURE.read_text())
    result = _run(f["closes"], {k: f[k] for k in ("ema_period", "bias_entry_pct")}, f["opens"])
    trades = result["_trades"]
    assert list(trades.EntryBar) == [i + 1 for i in f["expected_entries"]]
    assert list(trades.ExitBar) == [i + 1 for i in f["expected_exits"]]
    for trade, entry, exit_ in zip(trades.itertuples(), f["expected_entries"], f["expected_exits"]):
        assert trade.EntryPrice == f["opens"][entry + 1]
        assert trade.ExitPrice == f["opens"][exit_ + 1]
        assert trade.EntryTime == pd.Timestamp("2026-01-01") + pd.Timedelta(hours=entry + 1)
        assert trade.ExitTime == pd.Timestamp("2026-01-01") + pd.Timedelta(hours=exit_ + 1)
        assert trade.Size > 0
    assert list(result["_strategy"].bias) == pytest.approx(f["expected_bias"])
    assert not result["_strategy"].position


@pytest.mark.parametrize("close,enters", [(84.0, True), (84.001, False), (83.999, True)])
def test_equal_negative_entry_threshold_enters(close, enters):
    # EMA15 after 88 -> 84 is exactly 87.5, BIAS == -4 exactly.
    result = _run([88.] * 20 + [close, 88., 88.], {"ema_period": 15, "bias_entry_pct": 4})
    if close == 84:
        assert result["_strategy"].bias[20] == -4
    assert list(result["_trades"].EntryBar) == ([21] if enters else [])


def test_repeated_oversold_bars_do_not_add_positions():
    result = _run([100.] * 25 + [94., 93., 92., 104., 104.])
    assert all(result["_strategy"].bias[i] <= -3 for i in (25, 26, 27))
    assert list(result["_trades"].EntryBar) == [26]
    assert list(result["_trades"].ExitBar) == [29]
    assert not result["_strategy"].trades


def test_exit_equal_zero_closes_at_next_open():
    # EMA15: 88 -> 80 gives 87; close 87 next makes BIAS exactly zero.
    result = _run([88.] * 20 + [80., 87., 90.], {"ema_period": 15})
    assert result["_strategy"].bias[21] == 0
    assert list(result["_trades"].ExitBar) == [22]


def test_warmup_guard_blocks_early_oversold():
    result = _run([100.] * 10 + [80.] * 9)
    assert result["_trades"].empty and not result["_strategy"].position
    assert _build_bias_reversion({})["min_bars"] == 20
    assert _build_bias_reversion({})["executed_name"] == "BIAS Reversion (20/-3%)"
    assert not _build_bias_reversion({"stop_loss_pct": 5}).get("trade_on_close", False)


@pytest.mark.parametrize("risk,close", [({"stop_loss_pct": 5}, 85.), ({"take_profit_pct": 5}, 99.)])
def test_fixed_risk_exit_uses_next_open(risk, close):
    result = _run([100.] * 25 + [94., 94., close, 98.], risk)
    assert list(result["_trades"].EntryBar) == [26]
    assert list(result["_trades"].ExitBar) == [28]
    assert result["_trades"].iloc[0].ExitPrice == 98


def test_two_parameter_changes_produce_different_real_engine_trade_counts():
    closes = [100 + 10 * math.sin(i / 12) + 2 * math.sin(i / 3) for i in range(600)]
    counts = [len(_run(closes, p)["_trades"]) for p in ({}, {"ema_period": 60}, {"bias_entry_pct": 10})]
    assert len(set(counts)) == 3, counts


@pytest.mark.parametrize("name,kind,default,minimum,maximum", [
    ("ema_period", "integer", 20, 10, 60), ("bias_entry_pct", "number", 3, 1, 10),
])
def test_parameter_default_min_max_and_schema(name, kind, default, minimum, maximum):
    props = TOOL_SPECS[TOOL]["param_schema_properties"]
    assert props[name] == dict(type=kind, default=default, minimum=minimum, maximum=maximum)
    for value in (default, minimum, maximum):
        assert _validate_params_against_schema({name: value}, props) is None
        _build_bias_reversion({name: value})
    for value in (minimum - 1, maximum + 1):
        assert _validate_params_against_schema({name: value}, props) is not None
        with pytest.raises(ValueError, match="INVALID_PARAMS"):
            _build_bias_reversion({name: value})


@pytest.mark.parametrize("params", [{"ema_period": "bad"}, {"ema_period": None},
                                    {"bias_entry_pct": "bad"}, {"bias_entry_pct": float("nan")},
                                    {"bias_entry_pct": float("inf")}])
def test_invalid_params_are_rejected(params):
    with pytest.raises(ValueError, match="INVALID_PARAMS"):
        _build_bias_reversion(params)


def test_catalog_and_fixed_risk_schema_merge():
    spec = TOOL_SPECS[TOOL]
    assert spec["build"] is _build_bias_reversion
    assert spec["strategy_family"] == TOOL_SPECS["local.backtesting_py.rsi_reversal"]["strategy_family"]
    assert spec["is_default"] is False
    assert "stop_loss_pct" in spec["param_schema_properties"]
    assert spec["param_schema_properties"]["exchange"] == dict(type="string", default=DEFAULT_EXCHANGE)
