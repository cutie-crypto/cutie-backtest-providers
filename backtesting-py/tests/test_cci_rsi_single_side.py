import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import pytest
from backtesting import Backtest
from cutie_backtesting_provider import _build_cci_rsi, TOOL_SPECS, _validate_params_against_schema
from strategy_entry_evaluators import Bar, evaluate_entry, evaluate_exit

VALUES = [100,101,99,101,99,100,100,100,100,100]+[90,80,70,60,60,70,80,90,100,110,120,130,140,140,130,120,110,100,90,80,70,80,90,100]
PARAMS = dict(cci_period=3, rsi_period=3, cci_oversold=-80, cci_overbought=80, rsi_overbought=65)


def frame():
    return pd.DataFrame(dict(Open=VALUES, High=[v+1 for v in VALUES], Low=[v-1 for v in VALUES],
        Close=VALUES, Volume=[1]*len(VALUES)), index=pd.date_range("2026-01-01", periods=len(VALUES), freq="h"))


def run(params):
    return Backtest(frame(), _build_cci_rsi(params)["strategy"], cash=100000, finalize_trades=True).run()["_trades"]


@pytest.mark.parametrize("direction,expected", [("long", [[11,16,80,80]]), ("short", [[17,25,90,120]])])
def test_single_side_full_sequence_matches_runtime(direction, expected):
    params = {**PARAMS, "direction": direction}
    trades = run(params)
    assert trades[["EntryBar", "ExitBar", "EntryPrice", "ExitPrice"]].values.tolist() == expected
    assert all((size > 0) == (direction == "long") for size in trades.Size)
    bars = [Bar(i*3600, (i+1)*3600, r.Open, r.High, r.Low, r.Close) for i, (_, r) in enumerate(frame().iterrows())]
    held, entries, exits = False, [], []
    for i in range(9, len(bars)-1):
        if held and evaluate_exit("cci_rsi", params, bars[:i+1], direction):
            exits.append(i+1)
            held = False
        elif not held and evaluate_entry("cci_rsi", params, bars[:i+1], direction):
            entries.append(i+1)
            held = True
    assert list(zip(entries, exits)) == [tuple(row[:2]) for row in expected]
    assert not held


def test_default_both_full_trades_unchanged_and_explicit_schema():
    pd.testing.assert_frame_equal(run(PARAMS), run({**PARAMS, "direction": "both"}))
    assert run(PARAMS)[["EntryBar", "ExitBar"]].values.tolist() == [[11,16],[17,25]]
    schema = TOOL_SPECS["local.backtesting_py.cci_rsi"]["param_schema_properties"]
    assert schema["direction"]["default"] == "both"
    assert _validate_params_against_schema({"direction": "sideways"}, schema)
    with pytest.raises(ValueError, match="direction"):
        _build_cci_rsi({"direction": "sideways"})


@pytest.mark.parametrize("params", [{}, {"direction": "both"}, {"direction": "long"}])
def test_short_signal_replay_rejects_nonmatching_backtest(params):
    from strategy_intent_replay import generate_intents
    with pytest.raises(ValueError, match="explicit matching single-side"):
        generate_intents(bars=[], evaluator="cci_rsi", params=params, direction="short", leverage=3,
                         symbol="BTCUSDT", execution_policy={}, start_at=0)
