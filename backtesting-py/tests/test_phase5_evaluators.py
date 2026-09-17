import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import math
import pandas as pd
import pytest
from backtesting import Backtest
from cutie_backtesting_provider import _build_macd, _build_bollinger_reversal, _build_bollinger_breakout
from strategy_entry_evaluators import Bar, evaluate_entry, evaluate_exit, required_warmup_bars

CASES = [("macd", {"fast": 3, "slow": 6, "signal": 3}, _build_macd),
         ("bollinger_reversal", {"bb_period": 5, "bb_std": 1}, _build_bollinger_reversal),
         ("bollinger_breakout", {"bb_period": 5, "bb_std": 1}, _build_bollinger_breakout)]


@pytest.mark.parametrize("name,params,builder", CASES)
def test_full_trade_sequence_matches_provider(name, params, builder):
    values = [100.] * 50 + [100 + 10 * math.sin(i * math.pi / 10) for i in range(100)] + [100.] * 30
    df = pd.DataFrame(dict(Open=values, High=[v+1 for v in values], Low=[v-1 for v in values],
        Close=values, Volume=[1]*len(values)), index=pd.date_range("2026-01-01", periods=len(values), freq="h"))
    result = Backtest(df, builder(params)["strategy"], cash=100000, finalize_trades=False).run()
    trades = result["_trades"]
    bars = [Bar(i*3600, (i+1)*3600, value, value+1, value-1, value) for i,value in enumerate(values)]
    held, entries, exits = False, [], []
    for i in range(required_warmup_bars(name, params)-1, len(bars)-1):
        entry = evaluate_entry(name, params, bars[:i+1], "long")
        exit_hit = evaluate_exit(name, params, bars[:i+1], "long")
        assert not (entry and exit_hit)
        assert evaluate_entry(name, params, bars[:i+1], "short") is None
        assert evaluate_exit(name, params, bars[:i+1], "short") is None
        if held and exit_hit:
            exits.append(i+1)
            held = False
        elif not held and entry:
            entries.append(i+1)
            held = True
    assert bool(result["_strategy"].position) == held
    assert len(entries) == len(exits) + int(held)
    assert len(trades) >= 4
    assert trades[["EntryBar", "ExitBar"]].values.tolist() == [list(pair) for pair in zip(entries, exits)]
    assert trades.EntryPrice.tolist() == [values[i] for i in entries[:len(exits)]]
    assert trades.ExitPrice.tolist() == [values[i] for i in exits]
    assert all(trades.Size > 0)
