import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import pytest
from backtesting import Backtest
from cutie_backtesting_provider import _build_breakout, TOOL_SPECS, _validate_params_against_schema
from strategy_entry_evaluators import Bar, evaluate_breakout, evaluate_breakout_exit

VALUES = [100, 100, 100, 105, 110, 112, 108, 100, 95, 90, 92, 96, 103, 105, 100, 98, 98, 98]


def frame(direction):
    values = VALUES if direction == "long" else [200 - v for v in VALUES]
    return pd.DataFrame(dict(Open=values, High=[v+1 for v in values], Low=[v-1 for v in values],
        Close=values, Volume=[1]*len(values)), index=pd.date_range("2026-01-01", periods=len(values), freq="h"))


@pytest.mark.parametrize("direction", ["long", "short"])
def test_complete_engine_sequence_matches_runtime_channel_decisions(direction):
    params = dict(lookback=2, exit_lookback=1, direction=direction)
    df = frame(direction)
    trades = Backtest(df, _build_breakout(params)["strategy"], cash=100000, finalize_trades=True).run()["_trades"]
    assert trades[["EntryBar", "ExitBar"]].values.tolist() == [[4, 7], [12, 15]]
    assert all((size > 0) == (direction == "long") for size in trades.Size)
    expected_prices = [[110, 100], [103, 98]] if direction == "long" else [[90, 100], [97, 102]]
    assert trades[["EntryPrice", "ExitPrice"]].values.tolist() == expected_prices
    bars = [Bar(i*3600, (i+1)*3600, r.Open, r.High, r.Low, r.Close) for i, (_, r) in enumerate(df.iterrows())]
    # Walk the actual position state, rather than comparing isolated predicates.
    held, entries, exits = False, [], []
    for index in range(3, len(bars)-1):
        if not held and evaluate_breakout(params, bars[:index+1], direction):
            entries.append(index+1)
            held = True
        elif held and evaluate_breakout_exit(params, bars[:index+1], direction):
            exits.append(index+1)
            held = False
    assert list(zip(entries, exits)) == [(4, 7), (12, 15)]
    assert not held


def test_default_long_is_unchanged_and_direction_schema_is_explicit():
    df = frame("long")
    base = dict(lookback=2, exit_lookback=1)
    def run(params):
        return Backtest(df, _build_breakout(params)["strategy"], cash=100000, finalize_trades=True).run()["_trades"]
    pd.testing.assert_frame_equal(run(base), run({**base, "direction": "long"}))
    schema = TOOL_SPECS["local.backtesting_py.breakout"]["param_schema_properties"]
    assert schema["direction"] == {"type": "string", "enum": ["long", "short"], "default": "long"}
    assert _validate_params_against_schema({**base, "direction": "both"}, schema)
    with pytest.raises(ValueError, match="direction"):
        _build_breakout({**base, "direction": "both"})
