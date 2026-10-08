"""R1-T2：Supertrend 翻转模板（本批只做多）。

`tests/fixtures/supertrend_golden.json` 是独立纯 Python 循环（不 import 被测代码）算出的
逐 bar golden：Wilder ATR(alpha=1/n, adjust=False)、final_ub / final_lb（沿用 / 重置 / 收紧三种
分支）、trend(+1 up / -1 down)。``expected_entries`` / ``expected_exits`` 是信号 bar 序号（trend
翻转那一根）；真引擎的 EntryBar / ExitBar 恰好是信号 bar + 1。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import pytest
from backtesting import Backtest

from cutie_backtesting_provider import (
    TOOL_SPECS,
    _build_supertrend,
    _validate_params_against_schema,
)

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "supertrend_golden.json"


def _load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text())


def _frame(closes, highs, lows) -> pd.DataFrame:
    return pd.DataFrame(
        dict(Open=closes, High=highs, Low=lows, Close=closes, Volume=[1] * len(closes)),
        index=pd.date_range("2026-01-01", periods=len(closes), freq="h"),
    )


def _hand_supertrend(closes, highs, lows, n, m):
    """独立纯 Python 复算（RMA 递推，不用 pandas）。"""
    alpha = 1.0 / n
    atr, ub, lb, trend, branches = [], [], [], [], []
    for i in range(len(closes)):
        if i == 0:
            tr = highs[0] - lows[0]
            atr.append(tr)
        else:
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
            atr.append(alpha * tr + (1 - alpha) * atr[-1])
        hl2 = (highs[i] + lows[i]) / 2
        bu, bl = hl2 + m * atr[i], hl2 - m * atr[i]
        if i == 0:
            ub.append(bu)
            lb.append(bl)
            trend.append(-1)
            branches.append("init")
            continue
        if bu < ub[-1]:
            ub.append(bu)
            branches.append("tighten")
        elif closes[i - 1] > ub[-1]:
            ub.append(bu)
            branches.append("reset")
        else:
            ub.append(ub[-1])
            branches.append("keep")
        lb.append(bl if (bl > lb[-1] or closes[i - 1] < lb[-1]) else lb[-1])
        if trend[-1] > 0:
            trend.append(-1 if closes[i] < lb[i] else 1)
        else:
            trend.append(1 if closes[i] > ub[i] else -1)
    return atr, ub, lb, trend, branches


def _signals(trend):
    entries, exits, held = [], [], False
    for i in range(1, len(trend)):
        if not held and trend[i - 1] < 0 and trend[i] > 0:
            entries.append(i)
            held = True
        elif held and trend[i - 1] > 0 and trend[i] < 0:
            exits.append(i)
            held = False
    return entries, exits


# ---------------------------------------------------------------------------
# 1. Golden.
# ---------------------------------------------------------------------------


def test_golden_series_matches_hand_formula_per_bar():
    fx = _load_fixture()
    atr, ub, lb, trend, _ = _hand_supertrend(
        fx["closes"], fx["highs"], fx["lows"], fx["atr_period"], fx["multiplier"]
    )
    assert len(atr) == len(fx["expected_atr"]) == len(fx["closes"])
    for i in range(len(atr)):
        assert atr[i] == pytest.approx(fx["expected_atr"][i], abs=1e-9), i
        assert ub[i] == pytest.approx(fx["expected_final_ub"][i], abs=1e-9), i
        assert lb[i] == pytest.approx(fx["expected_final_lb"][i], abs=1e-9), i
        assert trend[i] == fx["expected_trend"][i], i


def test_golden_covers_flips_and_both_final_ub_branches():
    fx = _load_fixture()
    _, _, _, trend, branches = _hand_supertrend(
        fx["closes"], fx["highs"], fx["lows"], fx["atr_period"], fx["multiplier"]
    )
    ups = [i for i in range(1, len(trend)) if trend[i - 1] < 0 < trend[i]]
    downs = [i for i in range(1, len(trend)) if trend[i - 1] > 0 > trend[i]]
    assert len(ups) >= 2 and len(downs) >= 2
    assert "keep" in branches and "reset" in branches and "tighten" in branches


def test_golden_entries_exits_from_hand_state_machine():
    fx = _load_fixture()
    entries, exits = _signals(fx["expected_trend"])
    assert entries == fx["expected_entries"]
    assert exits == fx["expected_exits"]
    assert len(entries) >= 2 and len(exits) >= 2


def test_golden_matches_real_strategy_engine_execution_offset_by_one():
    fx = _load_fixture()
    df = _frame(fx["closes"], fx["highs"], fx["lows"])
    built = _build_supertrend(dict(atr_period=fx["atr_period"], multiplier=fx["multiplier"]))
    assert built["min_bars"] == fx["atr_period"] + 1
    result = Backtest(df, built["strategy"], cash=100000, finalize_trades=False).run()
    trades = result["_trades"]
    entries, exits = fx["expected_entries"], fx["expected_exits"]
    held_at_end = len(entries) > len(exits)
    assert list(trades["EntryBar"]) == [i + 1 for i in entries[: len(exits)]]
    assert list(trades["ExitBar"]) == [i + 1 for i in exits]
    assert bool(result["_strategy"].position) == held_at_end
    assert all(size > 0 for size in trades["Size"])  # long only


def test_changing_params_changes_trade_count():
    fx = _load_fixture()
    df = _frame(fx["closes"], fx["highs"], fx["lows"])

    def count(**p):
        built = _build_supertrend(p)
        return len(Backtest(df, built["strategy"], cash=100000, finalize_trades=True).run()["_trades"])

    default = count()  # atr_period=10, multiplier=3
    other_a = count(atr_period=5, multiplier=2)
    other_b = count(atr_period=5, multiplier=1)
    assert default != other_a
    assert default != other_b


# ---------------------------------------------------------------------------
# 2. Boundary: Close == final_ub does not flip up (strict >).
# ---------------------------------------------------------------------------


def _flat_then(last_close):
    closes = [100.0] * 10 + [last_close, last_close + 5]
    highs = [101.0] * 10 + [last_close, last_close + 5]
    lows = [99.0] * 10 + [100.0, last_close]
    return closes, highs, lows


def test_close_equal_final_ub_does_not_flip_up():
    closes, highs, lows = _flat_then(102.0)
    # Make the last bar neutral so a flip on it cannot be what we are testing:
    closes, highs, lows = closes[:11], highs[:11], lows[:11]
    _, ub, _, trend, _ = _hand_supertrend(closes, highs, lows, 5, 1.0)
    assert ub[-1] == closes[-1] == 102.0  # setup: close sits exactly on final_ub
    assert trend[-1] == -1
    # Pad one more bar at the same close so a (wrong) flip on bar 10 would fill at bar 11.
    closes.append(102.0)
    highs.append(102.0)
    lows.append(102.0)
    _, _, _, trend2, _ = _hand_supertrend(closes, highs, lows, 5, 1.0)
    assert trend2[10] == -1
    built = _build_supertrend(dict(atr_period=5, multiplier=1))
    result = Backtest(_frame(closes, highs, lows), built["strategy"], cash=100000, finalize_trades=False).run()
    assert len(result["_trades"]) == 0
    assert not result["_strategy"].position


def test_close_just_above_final_ub_flips_up_and_enters():
    closes = [100.0] * 10 + [102.5, 102.5]
    highs = [101.0] * 10 + [102.5, 102.5]
    lows = [99.0] * 10 + [100.0, 102.5]
    _, ub, _, trend, _ = _hand_supertrend(closes, highs, lows, 5, 1.0)
    assert ub[10] == 102.0 and trend[10] == 1 and trend[9] == -1
    built = _build_supertrend(dict(atr_period=5, multiplier=1))
    result = Backtest(_frame(closes, highs, lows), built["strategy"], cash=100000, finalize_trades=False).run()
    assert bool(result["_strategy"].position)
    assert result["_strategy"].trades[0].entry_bar == 11


def test_warmup_guard_blocks_entry_before_min_bars():
    # Flip up at bar 3 (< min_bars=11 for atr_period=10): must not enter.
    closes = [100.0, 99.0, 98.0, 110.0, 120.0, 130.0, 131.0, 132.0]
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    built = _build_supertrend(dict(atr_period=10, multiplier=1))
    result = Backtest(_frame(closes, highs, lows), built["strategy"], cash=100000, finalize_trades=False).run()
    assert len(result["_trades"]) == 0
    assert not result["_strategy"].position


# ---------------------------------------------------------------------------
# 3. Param validation.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        dict(atr_period=4),
        dict(atr_period=31),
        dict(multiplier=0.9),
        dict(multiplier=6.1),
        dict(atr_period="x"),
        dict(multiplier="nan"),
    ],
)
def test_invalid_params_rejected(params):
    with pytest.raises(ValueError, match="INVALID_PARAMS"):
        _build_supertrend(params)


def test_defaults_build():
    built = _build_supertrend({})
    assert built["min_bars"] == 11


# ---------------------------------------------------------------------------
# 4. Catalog / schema wiring.
# ---------------------------------------------------------------------------


def test_tool_spec_registered_with_expected_schema():
    spec = TOOL_SPECS["local.backtesting_py.supertrend"]
    assert spec["strategy_family"] == "trend"
    assert spec["is_default"] is False
    assert "Supertrend / 超级趋势" in spec["description"]
    props = spec["param_schema_properties"]
    assert props["atr_period"] == {"type": "integer", "default": 10, "minimum": 5, "maximum": 30}
    assert props["multiplier"] == {"type": "number", "default": 3, "minimum": 1, "maximum": 6}
    for key in ("stop_loss_pct", "take_profit_pct", "position_size_pct", "position_size_notional"):
        assert key in props


def test_schema_validation_rejects_out_of_range_params():
    props = TOOL_SPECS["local.backtesting_py.supertrend"]["param_schema_properties"]
    assert _validate_params_against_schema({"atr_period": 4}, props) is not None
    assert _validate_params_against_schema({"atr_period": 31}, props) is not None
    assert _validate_params_against_schema({"multiplier": 0.5}, props) is not None
    assert _validate_params_against_schema({"multiplier": 7}, props) is not None
    assert _validate_params_against_schema({"atr_period": 10, "multiplier": 3}, props) is None
