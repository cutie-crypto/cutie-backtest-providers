"""P1a T3: independent DIF/DEA recurrence and strict zero-axis filtering."""
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
    DEFAULT_EXCHANGE, TOOL_SPECS, _build_macd_above_zero, _validate_params_against_schema,
)

TOOL = "local.backtesting_py.macd_above_zero"
FIXTURE = Path(__file__).parent / "fixtures/macd_above_zero_golden.json"


def _ema(values, period):
    result = [float(values[0])]
    alpha = 2 / (period + 1)
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def _indicators(closes, fast=12, slow=26, signal=9):
    dif = [a - b for a, b in zip(_ema(closes, fast), _ema(closes, slow))]
    return dif, _ema(dif, signal)


def _run(closes, params=None, opens=None):
    opens = closes if opens is None else opens
    df = pd.DataFrame(dict(Open=opens, Close=closes,
                           High=[max(o, c) + .5 for o, c in zip(opens, closes)],
                           Low=[min(o, c) - .5 for o, c in zip(opens, closes)],
                           Volume=[1] * len(closes)),
                      index=pd.date_range("2026-01-01", periods=len(closes), freq="h"))
    built = _build_macd_above_zero({} if params is None else params)
    return Backtest(df, built["strategy"], cash=100000, finalize_trades=False).run()


def test_golden_indicators_and_signals_match_independent_hand_formula():
    f = json.loads(FIXTURE.read_text())
    assert len(f["closes"]) >= 60
    dif, dea = _indicators(f["closes"], f["fast"], f["slow"], f["signal"])
    assert dif == pytest.approx(f["expected_dif"], abs=1e-10)
    assert dea == pytest.approx(f["expected_dea"], abs=1e-10)
    entries, exits, blocked, held = [], [], [], False
    for i in range(1, len(dif)):
        if held and dif[i - 1] >= dea[i - 1] and dif[i] < dea[i]:
            exits.append(i)
            held = False
        elif not held and i + 1 >= f["min_bars"] and dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]:
            if dif[i] > 0 and dea[i] > 0:
                entries.append(i)
                held = True
            else:
                blocked.append(i)
    assert entries == f["expected_entries"] == [91, 109, 128, 147, 166, 185, 204, 223, 242, 261]
    assert exits == f["expected_exits"] == [101, 120, 138, 157, 176, 195, 213, 232, 251, 270]
    assert blocked == f["expected_blocked_entries"]
    assert not held


def test_golden_matches_real_strategy_engine_execution_offset_by_one():
    f = json.loads(FIXTURE.read_text())
    result = _run(f["closes"], {k: f[k] for k in ("fast", "slow", "signal")}, f["opens"])
    trades = result["_trades"]
    assert list(trades.EntryBar) == [i + 1 for i in f["expected_entries"]]
    assert list(trades.ExitBar) == [i + 1 for i in f["expected_exits"]]
    for trade, entry, exit_ in zip(trades.itertuples(), f["expected_entries"], f["expected_exits"]):
        assert trade.EntryPrice == f["opens"][entry + 1]
        assert trade.ExitPrice == f["opens"][exit_ + 1]
        assert trade.EntryTime == pd.Timestamp("2026-01-01") + pd.Timedelta(hours=entry + 1)
        assert trade.ExitTime == pd.Timestamp("2026-01-01") + pd.Timedelta(hours=exit_ + 1)
        assert trade.Size > 0
    assert list(result["_strategy"].macd) == pytest.approx(f["expected_dif"])
    assert list(result["_strategy"].signal) == pytest.approx(f["expected_dea"])
    assert not result["_strategy"].position


def test_below_zero_cross_does_not_enter():
    result = _run([100.] * 90 + [80.] * 20, dict(fast=5, slow=10, signal=9))
    dif, dea = result["_strategy"].macd, result["_strategy"].signal
    assert dif[96] <= dea[96] and dif[97] > dea[97]
    assert dif[97] < 0 and dea[97] < 0
    assert result["_trades"].empty and not result["_strategy"].position


def test_positive_dif_with_negative_dea_cross_does_not_enter():
    result = _run([100.] * 90 + [80., 110.] + [110.] * 10, dict(fast=5, slow=10, signal=9))
    dif, dea = result["_strategy"].macd, result["_strategy"].signal
    assert dif[90] <= dea[90] and dif[91] > dea[91]
    assert dif[91] > 0 and dea[91] < 0
    assert result["_trades"].empty and not result["_strategy"].position


@pytest.mark.parametrize("close,expected_dif,expected_dea", [(104., 0., -1.), (112., 2., 0.)])
def test_exact_zero_on_either_line_blocks_entry(close, expected_dif, expected_dea):
    # Dyadic EMAs: 100 -> 84 gives DIF=-4, DEA=-2. Next close 104 gives
    # DIF=0, DEA=-1; 112 gives DIF=2, DEA=0. Both are genuine upward crosses.
    result = _run([100.] * 30 + [84., close, close], dict(fast=3, slow=7, signal=3))
    dif, dea = result["_strategy"].macd, result["_strategy"].signal
    assert dif[30] <= dea[30] and dif[31] > dea[31]
    assert dif[31] == expected_dif and dea[31] == expected_dea
    assert result["_trades"].empty and not result["_strategy"].position


def test_cross_from_previous_equality_enters_and_negative_axis_death_cross_exits():
    result = _run([100.] * 90 + [101., 1., 1.])
    strategy = result["_strategy"]
    assert strategy.macd[89] == strategy.signal[89] == 0
    assert strategy.macd[90] > strategy.signal[90] > 0
    assert strategy.macd[91] < strategy.signal[91] < 0
    assert list(result["_trades"].EntryBar) == [91]
    assert list(result["_trades"].ExitBar) == [92]


def test_current_equality_does_not_enter():
    result = _run([100. + i for i in range(100)], dict(fast=5, slow=10, signal=1))
    assert list(result["_strategy"].macd) == list(result["_strategy"].signal)
    assert result["_trades"].empty and not result["_strategy"].position


def test_warmup_guard_and_default_name():
    result = _run([100.] * 30 + [120.] * 57)
    assert result["_trades"].empty and not result["_strategy"].position
    assert _build_macd_above_zero({})["min_bars"] == 88
    assert _build_macd_above_zero({})["executed_name"] == "MACD Above-Zero Cross (12/26/9)"
    assert not _build_macd_above_zero({"stop_loss_pct": 5}).get("trade_on_close", False)


@pytest.mark.parametrize("risk,close", [({"stop_loss_pct": 5}, 90.), ({"take_profit_pct": 5}, 110.)])
def test_fixed_risk_exit_uses_next_open(risk, close):
    result = _run([100.] * 90 + [101., 101., close, 99.], risk)
    assert list(result["_trades"].EntryBar) == [91]
    assert list(result["_trades"].ExitBar) == [93]
    assert result["_trades"].iloc[0].ExitPrice == 99.


def test_two_parameter_changes_produce_different_real_engine_trade_counts():
    closes = [100 + .2 * i + 3 * math.sin(i / 3) for i in range(600)]
    counts = [len(_run(closes, p)["_trades"]) for p in (
        {}, {"fast": 3, "slow": 6, "signal": 3}, {"fast": 50, "slow": 100, "signal": 30},
    )]
    assert len(set(counts)) == 3, counts


@pytest.mark.parametrize("name,default,minimum,maximum", [
    ("fast", 12, 2, 100), ("slow", 26, 3, 300), ("signal", 9, 1, 100),
])
def test_parameter_default_min_max_and_schema(name, default, minimum, maximum):
    props = TOOL_SPECS[TOOL]["param_schema_properties"]
    assert props[name] == dict(type="integer", default=default, minimum=minimum, maximum=maximum)
    assert props[name] == TOOL_SPECS["local.backtesting_py.macd"]["param_schema_properties"][name]
    for value in (default, minimum, maximum):
        assert _validate_params_against_schema({name: value}, props) is None
        params = dict(fast=2, slow=300, signal=9)
        params[name] = value
        _build_macd_above_zero(params)
    for value in (minimum - 1, maximum + 1):
        assert _validate_params_against_schema({name: value}, props) is not None
        with pytest.raises(ValueError, match="INVALID_PARAMS"):
            _build_macd_above_zero({name: value})


@pytest.mark.parametrize("params", [
    {"fast": 26, "slow": 26}, {"fast": 27, "slow": 26},
    {"fast": "bad"}, {"slow": None}, {"signal": "bad"}, {"signal": float("inf")},
])
def test_invalid_params_are_rejected(params):
    with pytest.raises(ValueError, match="INVALID_PARAMS"):
        _build_macd_above_zero(params)


def test_catalog_and_fixed_risk_schema_merge():
    spec = TOOL_SPECS[TOOL]
    assert spec["build"] is _build_macd_above_zero
    assert spec["strategy_family"] == "trend"
    assert spec["is_default"] is False
    assert "stop_loss_pct" in spec["param_schema_properties"]
    assert spec["param_schema_properties"]["exchange"] == dict(type="string", default=DEFAULT_EXCHANGE)
