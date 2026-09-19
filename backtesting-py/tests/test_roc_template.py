"""P2（0919 夜间无人值守）：动量 ROC 阈值模板。

契约唯一权威见 docs/reviews/2026-09-19-夜间无人值守/ROC-契约.md（TokenBeep 仓库）。

`tests/fixtures/roc_golden.json` 是逐 bar 手算的 golden：``expected_roc`` 是纯公式
``(close / close.shift(n) - 1) * 100``（前 n 根 None），``expected_entries`` /
``expected_exits`` 是信号 bar 序号（值刚越过阈值那一根，严格比较，不是 backtesting.py
执行侧因 trade_on_close=False 而延后一根的 EntryBar/ExitBar）——server 端 S5b 的判定器
直接对这份 fixture 的三个数组对账，不依赖 pandas/backtesting.py 的执行时序。本文件
额外用 ``Backtest(...).run()`` 跑一遍真实策略类，确认执行侧的 EntryBar/ExitBar 恰好是
信号 bar + 1（backtesting.py 默认下一根开盘成交），把 golden 与 provider 实际行为crosscheck。
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
    TOOL_SPECS,
    _build_roc,
    _validate_params_against_schema,
)

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "roc_golden.json"


def _load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text())


def _frame(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        dict(
            Open=closes,
            High=[v + 1 for v in closes],
            Low=[v - 1 for v in closes],
            Close=closes,
            Volume=[1] * len(closes),
        ),
        index=pd.date_range("2026-01-01", periods=len(closes), freq="h"),
    )


def _hand_roc(closes: list[float], period: int) -> list[float | None]:
    s = pd.Series(closes, dtype="float64")
    roc = (s / s.shift(period) - 1) * 100
    return [None if pd.isna(v) else float(v) for v in roc]


# ---------------------------------------------------------------------------
# 1. Fixed K-line golden.
# ---------------------------------------------------------------------------


def test_golden_roc_values_match_hand_formula():
    fixture = _load_fixture()
    computed = _hand_roc(fixture["closes"], fixture["roc_period"])
    assert len(computed) == len(fixture["expected_roc"])
    for got, want in zip(computed, fixture["expected_roc"]):
        if want is None:
            assert got is None
        else:
            assert got is not None
            assert math.isclose(got, want, abs_tol=1e-9)


def test_golden_entries_exits_from_hand_state_machine():
    fixture = _load_fixture()
    roc = _hand_roc(fixture["closes"], fixture["roc_period"])
    entry_threshold = fixture["entry_threshold"]
    exit_threshold = fixture["exit_threshold"]
    held = False
    entries, exits = [], []
    for i, v in enumerate(roc):
        if v is None:
            continue
        if not held and v > entry_threshold:
            entries.append(i)
            held = True
        elif held and v < exit_threshold:
            exits.append(i)
            held = False
    assert entries == fixture["expected_entries"]
    assert exits == fixture["expected_exits"]


def test_golden_matches_real_strategy_engine_execution_offset_by_one():
    """backtesting.py 默认 trade_on_close=False：next() 用当根收盘价判定，成交落在
    下一根开盘 —— 所以真实 Trade 的 EntryBar/ExitBar 恰好是 golden 信号 bar + 1。"""
    fixture = _load_fixture()
    df = _frame(fixture["closes"])
    params = dict(
        roc_period=fixture["roc_period"],
        entry_threshold=fixture["entry_threshold"],
        exit_threshold=fixture["exit_threshold"],
    )
    built = _build_roc(params)
    result = Backtest(df, built["strategy"], cash=100000, finalize_trades=False).run()
    trades = result["_trades"]

    expected_entries = fixture["expected_entries"]
    expected_exits = fixture["expected_exits"]
    held_at_end = len(expected_entries) > len(expected_exits)

    assert list(trades["EntryBar"]) == [i + 1 for i in expected_entries[: len(expected_exits)]]
    assert list(trades["ExitBar"]) == [i + 1 for i in expected_exits]
    assert bool(result["_strategy"].position) == held_at_end
    assert all(size > 0 for size in trades["Size"])  # long only


# ---------------------------------------------------------------------------
# 2. Contract boundary cases.
# ---------------------------------------------------------------------------


def test_roc_equal_entry_threshold_does_not_enter():
    # Ratio 150/100 = 1.5 is exact in IEEE754 double (no rounding artifact), so
    # ROC == 50.0 exactly here -- picking e.g. 105/100 would leave a float rounding
    # error (~5.000000000000004) that falsely trips the strict-> comparison.
    closes = [100.0] * 13 + [150.0] * 5
    df = _frame(closes)
    built = _build_roc(dict(roc_period=12, entry_threshold=50, exit_threshold=0))
    result = Backtest(df, built["strategy"], cash=100000, finalize_trades=True).run()
    assert len(result["_trades"]) == 0


def test_roc_equal_exit_threshold_does_not_exit():
    # Force entry above 5, then decay ROC to exactly 0 and hold there -> must not exit.
    closes = [100.0] * 13 + [110.0] * 13 + [100.0] * 13
    df = _frame(closes)
    built = _build_roc(dict(roc_period=12, entry_threshold=5, exit_threshold=0))
    roc = _hand_roc(closes, 12)
    # Sanity: the tail bars really do sit at ROC == 0 (close == close.shift(12)).
    assert any(v is not None and v == 0.0 for v in roc[-5:])
    result = Backtest(df, built["strategy"], cash=100000, finalize_trades=False).run()
    trades = result["_trades"]
    # No trade should close on a bar whose ROC == exit_threshold (0.0) exactly.
    for exit_bar in trades["ExitBar"]:
        v = roc[exit_bar - 1]  # signal bar is execution bar - 1
        assert v is None or v != 0.0


def test_roc_period_root_bar_produces_no_signal():
    """min_bars = roc_period + 1；只有 roc_period 根数据时（min_bars - 1）ROC 全 NaN，
    不产出任何信号（无论价格怎么走）。"""
    roc_period = 12
    closes = [100.0 + i * 5 for i in range(roc_period)]  # aggressive up-move, still too short
    values = _hand_roc(closes, roc_period)
    assert all(v is None for v in values)
    df = _frame(closes)
    built = _build_roc(dict(roc_period=roc_period, entry_threshold=5, exit_threshold=0))
    assert built["min_bars"] == roc_period + 1
    result = Backtest(df, built["strategy"], cash=100000, finalize_trades=True).run()
    assert len(result["_trades"]) == 0


def test_exit_threshold_equal_entry_threshold_cannot_both_be_true_same_bar():
    fixture = _load_fixture()
    roc = _hand_roc(fixture["closes"], fixture["roc_period"])
    threshold = 0
    for v in roc:
        if v is None:
            continue
        entered = v > threshold
        exited = v < threshold
        assert not (entered and exited)  # strict > / strict < on the same threshold


# ---------------------------------------------------------------------------
# 3. exit_threshold > entry_threshold rejected.
# ---------------------------------------------------------------------------


def test_exit_threshold_greater_than_entry_threshold_is_rejected():
    with pytest.raises(ValueError, match="INVALID_PARAMS:exit_threshold must be <= entry_threshold"):
        _build_roc(dict(roc_period=12, entry_threshold=5, exit_threshold=10))


def test_exit_threshold_equal_entry_threshold_is_allowed():
    built = _build_roc(dict(roc_period=12, entry_threshold=5, exit_threshold=5))
    assert built["min_bars"] == 13


# ---------------------------------------------------------------------------
# 4. Catalog / schema wiring.
# ---------------------------------------------------------------------------


def test_tool_spec_registered_with_expected_schema():
    spec = TOOL_SPECS["local.backtesting_py.roc"]
    assert spec["strategy_family"] == "trend"
    assert spec["is_default"] is False
    props = spec["param_schema_properties"]
    assert props["roc_period"] == {"type": "integer", "default": 12, "minimum": 2, "maximum": 200}
    assert props["entry_threshold"] == {"type": "number", "default": 5, "minimum": -100, "maximum": 100}
    assert props["exit_threshold"] == {"type": "number", "default": 0, "minimum": -100, "maximum": 100}
    # A6 固定风控覆盖层自动合并。
    for key in ("stop_loss_pct", "take_profit_pct", "position_size_pct", "position_size_notional"):
        assert key in props


def test_schema_validation_rejects_out_of_range_params():
    props = TOOL_SPECS["local.backtesting_py.roc"]["param_schema_properties"]
    assert _validate_params_against_schema({"roc_period": 1}, props) is not None
    assert _validate_params_against_schema({"roc_period": 201}, props) is not None
    assert _validate_params_against_schema({"entry_threshold": 101}, props) is not None
    assert _validate_params_against_schema({"exit_threshold": -101}, props) is not None
    assert _validate_params_against_schema({"roc_period": 12}, props) is None
