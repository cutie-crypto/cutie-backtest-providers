"""J4-P2-CCI-R1：CCI 超买超卖单独成立时，入场、出场、预热都不依赖 RSI。

`_build_cci_rsi` 的 `rsi_optional`（默认 False）为 True 时：
1. 入场只看 CCI（J4-P2）；
2. 出场只按 CCI 回到 0 轴，屏蔽 `RSI > 50` / `RSI < 50` 腿（J4-P2-CCI-R1 复审 critical）；
3. 预热只取 `cci_period * 3 + 1`，不再要求 RSI 有限（复审 medium）；
4. HTTP 入口的 `_validate_params_against_schema` 不传 RSI 参数即可通过，但超范围 RSI 仍拒。
不传或 False 时行为逐字节不变（入场/出场/预热都回退到双指标口径）。
实盘可达（ARMING_TOOL_EVALUATORS[cci_rsi]），回测与 `strategy_entry_evaluators` 必须同口径。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import pytest
from backtesting import Backtest
from cutie_backtesting_provider import (
    TOOL_SPECS,
    _build_cci_rsi,
    _validate_params_against_schema,
)
from strategy_entry_evaluators import (
    Bar,
    evaluate_entry,
    evaluate_exit,
    required_warmup_bars,
)

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

# 出场用例数据：前 37 根线性上行（CCI 恒 +66.67，mad 非 0，指标尽早暖机），随后深跌把 CCI
# 打到 -332，再小幅反弹。反弹期第 45 根起 RSI(4) 已 > 50 而 CCI 仍为 -33.54 / -5.79，
# 正是复审 critical 描述的"CCI 超卖入场、RSI 本来就 > 50"场景。
EXIT_VALUES = [100 + 0.5 * i for i in range(37)] + [92, 84, 76, 68, 60, 52] + [58, 64, 70, 76] + [
    88,
    100,
    112,
    124,
]
EXIT_STANDALONE = dict(
    cci_period=12,
    rsi_period=4,
    cci_oversold=-80,
    cci_overbought=80,
    direction="long",
    rsi_optional=True,
)
# bar 37 下单，bar 38 成交；bar 45/46（RSI > 50、CCI < 0）不平；bar 47 CCI 转正才平。
EXIT_TRADES = [[38, 48]]
# 同一份数据走双指标口径（不传 rsi_optional）：bar 45 的 RSI > 50 腿仍然生效 → bar 46 平仓。
EXIT_DUAL_PARAMS = {k: v for k, v in EXIT_STANDALONE.items() if k != "rsi_optional"}
EXIT_DUAL_TRADES = [[38, 46]]

# 预热用例数据：16 根线性上行（CCI 恒 +66.67）+ 6 根下跌，共 22 根。
# rsi_period=14 时旧口径要 43 根预热，22 根数据根本跑不出信号。
WARMUP_VALUES = [100 + 0.2 * i for i in range(16)] + [95, 88, 81, 74, 67, 60]
WARMUP_PARAMS = dict(
    cci_period=5,
    rsi_period=14,
    cci_oversold=-80,
    cci_overbought=80,
    direction="long",
    rsi_optional=True,
)


def frame(values):
    return pd.DataFrame(
        dict(
            Open=values,
            High=[v + 1 for v in values],
            Low=[v - 1 for v in values],
            Close=values,
            Volume=[1] * len(values),
        ),
        index=pd.date_range("2026-01-01", periods=len(values), freq="h"),
    )


def run(values, params):
    return Backtest(
        frame(values), _build_cci_rsi(params)["strategy"], cash=100000, finalize_trades=True
    ).run()["_trades"]


def bars(values):
    return [
        Bar(i * 3600, (i + 1) * 3600, r.Open, r.High, r.Low, r.Close)
        for i, (_, r) in enumerate(frame(values).iterrows())
    ]


def evaluator_sequence(values, params, direction="long"):
    series = bars(values)
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
    standalone = run(VALUES, STANDALONE)
    assert standalone[["EntryBar", "ExitBar"]].values.tolist() == [[11, 16], [26, 32]]

    dual_params = {**STANDALONE, "rsi_optional": False}
    assert run(VALUES, dual_params).empty

    entry = evaluate_entry("cci_rsi", STANDALONE, bars(VALUES)[:11], "long")
    assert entry is not None
    assert evaluate_entry("cci_rsi", dual_params, bars(VALUES)[:11], "long") is None


def test_cci_standalone_exit_ignores_rsi():
    """修前为什么红：出场仍写 `or rsi > 50`，第 45 根（RSI 52.09、CCI -33.54）就平仓 → ExitBar 46。

    变异点：把 `next()` / `evaluate_cci_rsi_exit` 的 RSI 腿恢复成无条件生效，本用例必须红。
    """
    assert run(EXIT_VALUES, EXIT_STANDALONE)[["EntryBar", "ExitBar"]].values.tolist() == EXIT_TRADES
    series = bars(EXIT_VALUES)
    # bar 45：RSI > 50 且 CCI < 0 —— 纯 CCI 口径不平仓（旧口径在这里秒平）。
    assert evaluate_exit("cci_rsi", EXIT_STANDALONE, series[:46], "long") is None
    # bar 46：CCI 仍 < 0（-5.79），继续持有。
    assert evaluate_exit("cci_rsi", EXIT_STANDALONE, series[:47], "long") is None
    # bar 47：CCI 转正才出场。
    exit_trigger = evaluate_exit("cci_rsi", EXIT_STANDALONE, series[:48], "long")
    assert exit_trigger is not None and exit_trigger.action == "exit"
    assert exit_trigger.indicators["cci"] > 0


def test_cci_standalone_live_exit_matches_backtest():
    """同一份数据回测与判定器出场必须一致。

    变异点：只改回测漏判定器（或反过来），本用例必须红——判定器旧口径会在 bar 45 出场(46)。
    """
    trades = run(EXIT_VALUES, EXIT_STANDALONE)
    expected = trades[["EntryBar", "ExitBar"]].values.tolist()
    assert expected == EXIT_TRADES
    assert evaluator_sequence(EXIT_VALUES, EXIT_STANDALONE) == [tuple(row) for row in expected]
    # 单腿数据（入场用例）两边也保持一致。
    standalone = run(VALUES, STANDALONE)
    standalone_expected = standalone[["EntryBar", "ExitBar"]].values.tolist()
    assert evaluator_sequence(VALUES, STANDALONE) == [tuple(row) for row in standalone_expected]


def test_cci_standalone_warmup_uses_cci_only():
    """rsi_optional=True 时预热只看 CCI：cci_period=5 只需要 16 根，不是 max(5,14)*3+1=43。"""
    assert _build_cci_rsi(WARMUP_PARAMS)["min_bars"] == 16
    assert required_warmup_bars("cci_rsi", WARMUP_PARAMS) == 16
    # 22 根数据（>16 且 <43）就能跑出交易；旧口径在同一份数据上完全静默。
    standalone = run(WARMUP_VALUES, WARMUP_PARAMS)
    assert standalone[["EntryBar", "ExitBar"]].values.tolist() == [[17, 21]]

    dual_params = {**WARMUP_PARAMS, "rsi_optional": False}
    assert _build_cci_rsi(dual_params)["min_bars"] == 43
    assert required_warmup_bars("cci_rsi", dual_params) == 43
    assert run(WARMUP_VALUES, dual_params).empty


def test_cci_standalone_passes_schema_without_rsi_params():
    """真实入口闸门：HTTP /cutie/backtest 就是 `_validate_params_against_schema(params,
    tool_spec["param_schema_properties"])`（cutie_backtesting_provider.py 的 `_run_backtest_sync`）。

    修前为什么红：旧用例只测 `_build_cci_rsi` 内部函数，闸门是否放行无人验证；
    这里直接用 catalog schema 走同一句校验。变异点：把 RSI 参数重新做成必填/新增 bound 前置校验即红。
    """
    schema = TOOL_SPECS["local.backtesting_py.cci_rsi"]["param_schema_properties"]
    assert schema["rsi_optional"]["default"] is False
    # 不传 RSI 参数（保留默认）必须过闸门。
    assert _validate_params_against_schema({"rsi_optional": True}, schema) is None
    assert _validate_params_against_schema({"rsi_optional": True, "cci_period": 5}, schema) is None
    # RSI 传了值且超范围：schema 继续拒，不因 rsi_optional 放宽。
    assert _validate_params_against_schema({"rsi_optional": True, "rsi_overbought": 130}, schema) == (
        "rsi_overbought must be <= 99 (got 130)"
    )
    assert _validate_params_against_schema({"rsi_optional": True, "rsi_oversold": 0}, schema) == (
        "rsi_oversold must be >= 1 (got 0)"
    )
    # 过闸门后 build 也接受（RSI 落默认值，预热只按 CCI）。
    assert _build_cci_rsi({"rsi_optional": True, "cci_period": 5})["min_bars"] == 16
    # 关闭 rsi_optional 时 build 自己的边界校验仍在。
    with pytest.raises(ValueError, match="rsi_oversold"):
        _build_cci_rsi(
            {"rsi_oversold": 30, "rsi_overbought": 130, "rsi_optional": False}
        )


def test_cci_rsi_default_unchanged():
    """不传 rsi_optional 与显式 False 完全一致，且入场/出场交易序列与改前相同。"""
    default = run(VALUES, DEFAULT_PARAMS)
    explicit_false = run(VALUES, {**DEFAULT_PARAMS, "rsi_optional": False})
    pd.testing.assert_frame_equal(default, explicit_false)
    assert default[["EntryBar", "ExitBar"]].values.tolist() == DEFAULT_TRADES
    assert _build_cci_rsi(DEFAULT_PARAMS)["min_bars"] == 10
    assert required_warmup_bars("cci_rsi", DEFAULT_PARAMS) == 10

    # 出场路径断言：默认口径下 RSI 腿仍然生效——同一份出场数据里 bar 45 的 RSI > 50
    # 就足够平仓（ExitBar 46），不会等到 CCI 转正（48）。
    dual = run(EXIT_VALUES, EXIT_DUAL_PARAMS)
    assert dual[["EntryBar", "ExitBar"]].values.tolist() == EXIT_DUAL_TRADES
    dual_exit = evaluate_exit("cci_rsi", EXIT_DUAL_PARAMS, bars(EXIT_VALUES)[:46], "long")
    assert dual_exit is not None and dual_exit.action == "exit"
    assert dual_exit.indicators["cci"] < 0 and dual_exit.indicators["rsi"] > 50
