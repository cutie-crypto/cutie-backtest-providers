"""J4-P2-CCI：CCI 超买超卖可以单独成立，不必同时填 RSI。

`_build_cci_rsi` 新增 `rsi_optional`（默认 False）：为 True 时入场只看 CCI，且不再强制
`0 < rsi_oversold < rsi_overbought < 100`；不传或 False 时行为逐字节不变。
实盘可达（ARMING_TOOL_EVALUATORS[cci_rsi]），回测与 `strategy_entry_evaluators` 必须同口径。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import pytest
from backtesting import Backtest
from cutie_backtesting_provider import _build_cci_rsi
from strategy_entry_evaluators import Bar, evaluate_entry, evaluate_exit

VALUES = [100, 101, 99, 101, 99, 100, 100, 100, 100, 100] + [
    90, 80, 70, 60, 60, 70, 80, 90, 100, 110, 120, 130, 140, 140, 130, 120, 110, 100, 90, 80, 70, 80, 90, 100
]
# rsi_oversold=1：RSI 在样本里远高于 1，所以只有 CCI 腿能满足——用来证明"单独 CCI 可入场"。
STANDALONE = dict(
    cci_period=3,
    rsi_period=3,
    cci_oversold=-80,
    cci_overbought=80,
    rsi_oversold=1,
    rsi_overbought=99,
    direction="long",
    rsi_optional=True,
)
# 改前既有参数（不传 rsi_optional）。
DEFAULT_PARAMS = dict(
    cci_period=3,
    rsi_period=3,
    cci_oversold=-80,
    cci_overbought=80,
    rsi_overbought=65,
)
DEFAULT_TRADES = [[11, 16], [17, 25]]


def frame():
    return pd.DataFrame(
        dict(
            Open=VALUES,
            High=[v + 1 for v in VALUES],
            Low=[v - 1 for v in VALUES],
            Close=VALUES,
            Volume=[1] * len(VALUES),
        ),
        index=pd.date_range("2026-01-01", periods=len(VALUES), freq="h"),
    )


def run(params):
    return Backtest(frame(), _build_cci_rsi(params)["strategy"], cash=100000, finalize_trades=True).run()["_trades"]


def bars():
    return [Bar(i * 3600, (i + 1) * 3600, r.Open, r.High, r.Low, r.Close) for i, (_, r) in enumerate(frame().iterrows())]


def evaluator_sequence(params, direction="long"):
    series = bars()
    held, entries, exits = False, [], []
    for i in range(9, len(series) - 1):
        if held and evaluate_exit("cci_rsi", params, series[: i + 1], direction):
            exits.append(i + 1)
            held = False
        elif not held and evaluate_entry("cci_rsi", params, series[: i + 1], direction):
            entries.append(i + 1)
            held = True
    assert not held
    return list(zip(entries, exits))


def test_cci_standalone_entry():
    """只有 CCI 满足时也入场；RSI 腿不满足时双指标口径不入场。"""
    standalone = run(STANDALONE)
    assert standalone[["EntryBar", "ExitBar"]].values.tolist() == [[11, 16], [26, 32]]

    dual_params = {**STANDALONE, "rsi_optional": False}
    assert run(dual_params).empty

    entry = evaluate_entry("cci_rsi", STANDALONE, bars()[:11], "long")
    assert entry is not None
    assert evaluate_entry("cci_rsi", dual_params, bars()[:11], "long") is None


def test_cci_rsi_default_unchanged():
    """不传 rsi_optional 与显式 False 完全一致，且交易序列与改前相同。"""
    default = run(DEFAULT_PARAMS)
    explicit_false = run({**DEFAULT_PARAMS, "rsi_optional": False})
    pd.testing.assert_frame_equal(default, explicit_false)
    assert default[["EntryBar", "ExitBar"]].values.tolist() == DEFAULT_TRADES


def test_cci_standalone_skips_rsi_bound_check():
    """变异点：rsi_optional=True 时仍强制 0<os<ob<100，本用例必须红。"""
    invalid_rsi = {**STANDALONE, "rsi_oversold": 30, "rsi_overbought": 130}
    _build_cci_rsi(invalid_rsi)  # 不应抛错
    with pytest.raises(ValueError, match="rsi_oversold"):
        _build_cci_rsi({**invalid_rsi, "rsi_optional": False})


def test_cci_standalone_live_evaluator_matches_backtest():
    """变异点：只改回测漏判定器，本用例必须红。"""
    trades = run(STANDALONE)
    expected = trades[["EntryBar", "ExitBar"]].values.tolist()
    assert expected == [[11, 16], [26, 32]]
    assert evaluator_sequence(STANDALONE) == [tuple(row) for row in expected]
