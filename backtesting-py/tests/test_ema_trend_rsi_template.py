"""108 顺序 5a-P（0920 白天批 T2）：EMA 趋势 + RSI 入场组合模板（两条件 AND）。

契约唯一权威见 TokenBeep 仓
docs/features/108_策略自动发信号执行器扩容/IMPL_顺序5a_两条件AND回测.md §2/§3「5a-P」。

判定语义：在已收盘 K 线上，`EMA_fast > EMA_slow`（严格大于，趋势过滤）且
`RSI < rsi_entry_below`（严格小于，入场）、当前无仓 ⇒ 下一根开多；持仓中
`RSI > rsi_exit_above`（严格大于）⇒ 下一根平仓。均为状态条件（持续高于/低于），
不是穿越事件，不用 crossover；只做多。

`tests/fixtures/ema_trend_rsi_golden.json` 是逐 bar 手算的 golden：``expected_ema_fast``
/``expected_ema_slow`` 是纯公式 ``close.ewm(span=n, adjust=False).mean()``，
``expected_rsi`` 是 Wilder 平滑 ``ewm(alpha=1/period, adjust=False)`` 的增益/损失比值
（预热期 —— 索引 < min_bars-1 —— 记为 null，不断言）；``expected_entries``/
``expected_exits`` 是信号 bar 序号（值刚满足条件那一根，严格比较，不是
backtesting.py 执行侧因 trade_on_close=False 而延后一根的 EntryBar/ExitBar）。
本文件的 ``_hand_ema``/``_hand_rsi`` 是独立实现，不调用被测的
``_build_ema_trend_rsi``/``EmaTrendRsiStrategy``，只是复用同一组数学定义
（EMA 与 provider 的 ``EmaCrossStrategy`` 同一 ewm 公式，RSI 与 provider 共享的
``_rsi_series`` 同一 Wilder 公式）交叉验证。

fixture 的 60 根以内 close 序列覆盖四种情形（``scenario_bars`` 给出具体 bar 序号）：
- bar 20：trend=False 且 RSI 很低（0.0）——趋势不成立，不入场。
- bar 23：trend=True 且 RSI 不低（74.09）——RSI 未达标，不入场。
- bar 40：trend=True 且 RSI<30（24.70）——两条件同时成立，入场。
- bar 47：持仓中 RSI>70（74.62）——出场。
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
    _build_ema_trend_rsi,
    _validate_params_against_schema,
)

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "ema_trend_rsi_golden.json"


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


def _hand_ema(closes: list[float], span: int) -> list[float]:
    """独立实现：与 pandas ``.ewm(span=n, adjust=False).mean()`` 同一递推定义
    （alpha=2/(span+1)，seed=第一根收盘价），不调用 provider 代码。"""
    s = pd.Series(closes, dtype="float64")
    return s.ewm(span=span, adjust=False).mean().tolist()


def _hand_rsi(closes: list[float], period: int) -> list[float]:
    """独立实现：Wilder 平滑（alpha=1/period, adjust=False），与 provider 共享的
    ``_rsi_series`` 同一公式，但本函数不调用它——0/0（无涨跌记忆）填 50，
    与 provider 的 ``fillna(50.0)`` 行为一致。"""
    s = pd.Series(closes, dtype="float64")
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1.0 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1.0 / period, adjust=False).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50.0).tolist()


# ---------------------------------------------------------------------------
# 1. Fixed K-line golden.
# ---------------------------------------------------------------------------


def test_golden_indicator_values_match_hand_formula():
    fixture = _load_fixture()
    closes = fixture["closes"]
    ema_fast = _hand_ema(closes, fixture["ema_fast"])
    ema_slow = _hand_ema(closes, fixture["ema_slow"])
    rsi = _hand_rsi(closes, fixture["rsi_period"])
    for got_series, want_series in (
        (ema_fast, fixture["expected_ema_fast"]),
        (ema_slow, fixture["expected_ema_slow"]),
        (rsi, fixture["expected_rsi"]),
    ):
        assert len(got_series) == len(want_series)
        for got, want in zip(got_series, want_series):
            if want is None:
                continue
            assert math.isclose(got, want, abs_tol=1e-6)


def test_golden_entries_exits_from_hand_state_machine():
    fixture = _load_fixture()
    closes = fixture["closes"]
    ema_fast = _hand_ema(closes, fixture["ema_fast"])
    ema_slow = _hand_ema(closes, fixture["ema_slow"])
    rsi = _hand_rsi(closes, fixture["rsi_period"])
    entry_below = fixture["rsi_entry_below"]
    exit_above = fixture["rsi_exit_above"]
    warmup = fixture["min_bars"] - 1

    held = False
    entries, exits = [], []
    for i in range(len(closes)):
        if i < warmup:
            continue
        trend = ema_fast[i] > ema_slow[i]
        if not held and trend and rsi[i] < entry_below:
            entries.append(i)
            held = True
        elif held and rsi[i] > exit_above:
            exits.append(i)
            held = False

    assert entries == fixture["expected_entries"]
    assert exits == fixture["expected_exits"]

    # 四种情形逐一钉住，防止 golden 巧合凑对但实际不覆盖某一分支。
    bars = fixture["scenario_bars"]
    trend_false_rsi_low = bars["trend_false_rsi_low_no_entry"]
    assert ema_fast[trend_false_rsi_low] <= ema_slow[trend_false_rsi_low]
    assert rsi[trend_false_rsi_low] < entry_below
    assert trend_false_rsi_low not in entries

    trend_true_rsi_not_low = bars["trend_true_rsi_not_low_no_entry"]
    assert ema_fast[trend_true_rsi_not_low] > ema_slow[trend_true_rsi_not_low]
    assert rsi[trend_true_rsi_not_low] >= entry_below
    assert trend_true_rsi_not_low not in entries

    entry_bar = bars["trend_true_rsi_low_entry"]
    assert ema_fast[entry_bar] > ema_slow[entry_bar]
    assert rsi[entry_bar] < entry_below
    assert entry_bar in entries

    exit_bar = bars["holding_rsi_high_exit"]
    assert rsi[exit_bar] > exit_above
    assert exit_bar in exits


def test_golden_matches_real_strategy_engine_execution_offset_by_one():
    """backtesting.py 默认 trade_on_close=False：next() 用当根收盘价判定，成交落在
    下一根开盘 —— 真实 Trade 的 EntryBar/ExitBar 恰好是 golden 信号 bar + 1。"""
    fixture = _load_fixture()
    df = _frame(fixture["closes"])
    params = dict(
        ema_fast=fixture["ema_fast"],
        ema_slow=fixture["ema_slow"],
        rsi_period=fixture["rsi_period"],
        rsi_entry_below=fixture["rsi_entry_below"],
        rsi_exit_above=fixture["rsi_exit_above"],
    )
    built = _build_ema_trend_rsi(params)
    assert built["min_bars"] == fixture["min_bars"]
    result = Backtest(df, built["strategy"], cash=100000, finalize_trades=False).run()
    trades = result["_trades"]

    expected_entries = fixture["expected_entries"]
    expected_exits = fixture["expected_exits"]
    assert list(trades["EntryBar"]) == [i + 1 for i in expected_entries[: len(expected_exits)]]
    assert list(trades["ExitBar"]) == [i + 1 for i in expected_exits]
    assert all(size > 0 for size in trades["Size"])  # long only


# ---------------------------------------------------------------------------
# 2. Mutation-sensitive isolation: trend-false-but-RSI-low must never enter.
# ---------------------------------------------------------------------------


def test_no_entry_when_trend_false_even_though_rsi_is_low():
    """截到 golden bar 20（scenario_bars.trend_false_rsi_low_no_entry）为止：该 bar
    trend=False 且 RSI=0.0（远低于入场阈值 30），若不看趋势条件必然误入场。

    变异自证（本次实施已手工执行，回报里写 MUTANT_EXIT= 行）：临时把
    `next()` 里的 `fast > slow and` 去掉，只留 `rsi < rsi_entry_below`，重跑本测试
    ——必须由绿转红（trades 从 0 变成 1），再恢复代码、确认转回绿。
    """
    fixture = _load_fixture()
    # +2（不是 +1）：signal bar 20 的假想入场要等下一根开盘才能成交
    # （trade_on_close=False），切到 bar 20 为止会让委托没有下一根可成交、
    # trades 恒为空掩盖了变异——必须多留一根让委托有机会真正成交。
    cutoff = fixture["scenario_bars"]["trend_false_rsi_low_no_entry"] + 2
    closes = fixture["closes"][:cutoff]
    df = _frame(closes)
    params = dict(
        ema_fast=fixture["ema_fast"],
        ema_slow=fixture["ema_slow"],
        rsi_period=fixture["rsi_period"],
        rsi_entry_below=fixture["rsi_entry_below"],
        rsi_exit_above=fixture["rsi_exit_above"],
    )
    built = _build_ema_trend_rsi(params)
    result = Backtest(df, built["strategy"], cash=100000, finalize_trades=True).run()
    assert len(result["_trades"]) == 0


def test_no_entry_when_rsi_not_low_even_though_trend_true():
    """截到 golden bar 39（entry bar 40 之前一根）为止：区间内多根 bar trend=True
    但 RSI 从未跌破 30——RSI 条件缺失时才会在这段误入场（第一段 scenario C 的
    互补钉子）。"""
    fixture = _load_fixture()
    entry_bar = fixture["scenario_bars"]["trend_true_rsi_low_entry"]
    closes = fixture["closes"][:entry_bar]  # 不含真正的入场 bar
    df = _frame(closes)
    params = dict(
        ema_fast=fixture["ema_fast"],
        ema_slow=fixture["ema_slow"],
        rsi_period=fixture["rsi_period"],
        rsi_entry_below=fixture["rsi_entry_below"],
        rsi_exit_above=fixture["rsi_exit_above"],
    )
    built = _build_ema_trend_rsi(params)
    result = Backtest(df, built["strategy"], cash=100000, finalize_trades=True).run()
    assert len(result["_trades"]) == 0


# ---------------------------------------------------------------------------
# 2b. Warm-up guard: min_bars = max(ema_slow, rsi_period + 1) must gate entry
# judgment, not just the overall data-length check in the router.
# ---------------------------------------------------------------------------


def test_no_entry_before_min_bars_even_though_conditions_hold_early():
    """ema_fast=2/ema_slow=20/rsi_period=2：close 以 [100,110,105,105] 开头，之后
    补足 105.0 到共 25 根。EMA(ewm) 与 RSI（NaN 填 50）在第 3 根（索引 2）就已经
    双双满足入场条件（trend=True 且 RSI=66.667% < rsi_entry_below=70）并一路保持
    ——`math.isfinite` 检查拦不住这种早熟信号，`min_bars = max(ema_slow, rsi_period+1)
    = 20` 才是唯一的预热门槛。

    断言预热期内（signal bar < min_bars-1 == 19）没有任何成交，且预热期结束后
    （signal bar == 19，长度达到 min_bars 的第一根）条件仍满足时能正常入场
    ——证明门槛只是延后判定，不是把入场永久关掉。

    变异自证（本次实施已手工执行，回报里写 MUTANT_P2_EXIT= 行）：临时删掉
    `next()` 里 `len(self.data) < min_bars` 的门槛，重跑本测试——必须由绿转红
    （EntryBar 从 20 提前到 3），再恢复代码、确认转回绿。
    """
    closes = [100.0, 110.0, 105.0, 105.0] + [105.0] * 21  # 共 25 根
    df = _frame(closes)
    params = dict(
        ema_fast=2, ema_slow=20, rsi_period=2, rsi_entry_below=70, rsi_exit_above=90
    )
    built = _build_ema_trend_rsi(params)
    assert built["min_bars"] == 20  # max(ema_slow=20, rsi_period+1=3)

    result = Backtest(df, built["strategy"], cash=100000, finalize_trades=True).run()
    trades = result["_trades"]
    assert len(trades) == 1
    # signal bar 19（长度达到 min_bars=20 的第一根）下一根开盘成交 -> EntryBar 20。
    assert list(trades["EntryBar"]) == [20]
    assert all(bar >= built["min_bars"] for bar in trades["EntryBar"])


# ---------------------------------------------------------------------------
# 3. Contract boundary cases.
# ---------------------------------------------------------------------------


def test_rsi_equal_entry_threshold_does_not_enter():
    """closes=[100,96,99,103,103], ema_fast=2/ema_slow=3/rsi_period=2：bar3 trend=True
    且 RSI 精确等于 73.33333333333333（Wilder ewm 精确计算，非四舍五入巧合）
    ——严格 `<` 要求，恰好相等不入场。

    追加的第 5 根（close 复用 103.0）不是凑数：若把 `next()` 里的入场比较误写成
    `<=`，bar3 会误发买单，但 trade_on_close=False 下委托要等下一根开盘才能成交；
    若序列只到 bar3 为止，委托没有下一根可成交，`_trades` 恒为空，误判仍会显示
    0 trades 掩盖 bug。第 5 根 close 沿用 103.0 使 RSI 保持等于同一阈值（同一比率
    下 Wilder 平滑值不变，见测试文件顶部 docstring 的 ewm 公式），因此正确实现
    在 bar4 上同样不满足 `<` 而不会独立入场——`_trades` 非空只能来自 bar3 误发
    的委托在 bar4 开盘成交，真正抓住 `<` vs `<=` 的边界。

    变异自证（本次实施已手工执行，回报里写 MUTANT_P3_EXIT= 行）：临时把
    `next()` 里的 `rsi < rsi_entry_below` 改成 `rsi <= rsi_entry_below`，重跑本
    测试——必须由绿转红（trades 从 0 变成 1），再恢复代码、确认转回绿。
    """
    closes = [100.0, 96.0, 99.0, 103.0, 103.0]
    threshold = _hand_rsi(closes, 2)[3]
    assert threshold == 73.33333333333333
    assert _hand_rsi(closes, 2)[4] == threshold  # 第 5 根维持同一阈值，不引入独立入场
    df = _frame(closes)
    built = _build_ema_trend_rsi(
        dict(ema_fast=2, ema_slow=3, rsi_period=2, rsi_entry_below=threshold, rsi_exit_above=100)
    )
    result = Backtest(df, built["strategy"], cash=100000, finalize_trades=True).run()
    assert len(result["_trades"]) == 0


# ---------------------------------------------------------------------------
# 4. Cross-field validation rejected (INVALID_PARAMS, same shape as existing tools).
# ---------------------------------------------------------------------------


def test_ema_fast_not_less_than_ema_slow_is_rejected():
    with pytest.raises(ValueError, match="INVALID_PARAMS:ema_fast must be less than ema_slow"):
        _build_ema_trend_rsi(dict(ema_fast=50, ema_slow=50))
    with pytest.raises(ValueError, match="INVALID_PARAMS:ema_fast must be less than ema_slow"):
        _build_ema_trend_rsi(dict(ema_fast=201, ema_slow=200))


def test_rsi_exit_above_not_greater_than_entry_below_is_rejected():
    with pytest.raises(ValueError, match="INVALID_PARAMS:rsi_exit_above must be > rsi_entry_below"):
        _build_ema_trend_rsi(dict(rsi_entry_below=30, rsi_exit_above=30))
    with pytest.raises(ValueError, match="INVALID_PARAMS:rsi_exit_above must be > rsi_entry_below"):
        _build_ema_trend_rsi(dict(rsi_entry_below=70, rsi_exit_above=30))


def test_rsi_exit_above_equal_entry_below_is_the_boundary_not_allowed():
    built_ok = _build_ema_trend_rsi(dict(rsi_entry_below=30, rsi_exit_above=30.0000001))
    assert built_ok["min_bars"] == 200
    with pytest.raises(ValueError, match="INVALID_PARAMS:rsi_exit_above must be > rsi_entry_below"):
        _build_ema_trend_rsi(dict(rsi_entry_below=30, rsi_exit_above=30.0))


# ---------------------------------------------------------------------------
# 5. Catalog / schema wiring.
# ---------------------------------------------------------------------------


def test_tool_spec_registered_with_expected_schema():
    spec = TOOL_SPECS["local.backtesting_py.ema_trend_rsi"]
    assert spec["strategy_family"] == "trend"
    assert spec["is_default"] is False
    props = spec["param_schema_properties"]
    assert props["ema_fast"] == {"type": "integer", "default": 50, "minimum": 2, "maximum": 399}
    assert props["ema_slow"] == {"type": "integer", "default": 200, "minimum": 3, "maximum": 400}
    assert props["rsi_period"] == {"type": "integer", "default": 14, "minimum": 2, "maximum": 100}
    assert props["rsi_entry_below"] == {"type": "number", "default": 30, "minimum": 0, "maximum": 100}
    assert props["rsi_exit_above"] == {"type": "number", "default": 70, "minimum": 0, "maximum": 100}
    # A6 固定风控覆盖层自动合并。
    for key in ("stop_loss_pct", "take_profit_pct", "position_size_pct", "position_size_notional"):
        assert key in props


def test_schema_validation_rejects_out_of_range_params():
    props = TOOL_SPECS["local.backtesting_py.ema_trend_rsi"]["param_schema_properties"]
    assert _validate_params_against_schema({"ema_fast": 1}, props) is not None
    assert _validate_params_against_schema({"ema_fast": 400}, props) is not None
    assert _validate_params_against_schema({"ema_slow": 2}, props) is not None
    assert _validate_params_against_schema({"ema_slow": 401}, props) is not None
    assert _validate_params_against_schema({"rsi_period": 1}, props) is not None
    assert _validate_params_against_schema({"rsi_period": 101}, props) is not None
    assert _validate_params_against_schema({"rsi_entry_below": -1}, props) is not None
    assert _validate_params_against_schema({"rsi_entry_below": 101}, props) is not None
    assert _validate_params_against_schema({"rsi_exit_above": -1}, props) is not None
    assert _validate_params_against_schema({"rsi_exit_above": 101}, props) is not None
    assert _validate_params_against_schema({"ema_fast": 50, "ema_slow": 200}, props) is None


def test_default_params_build_without_error():
    built = _build_ema_trend_rsi({})
    assert built["min_bars"] == 200  # max(ema_slow=200, rsi_period+1=15)


def test_min_bars_uses_larger_of_ema_slow_and_rsi_period_plus_one():
    # ema_slow 更大时 min_bars = ema_slow。
    built = _build_ema_trend_rsi(dict(ema_fast=5, ema_slow=20, rsi_period=5))
    assert built["min_bars"] == 20
    # rsi_period+1 更大时 min_bars = rsi_period+1。
    built2 = _build_ema_trend_rsi(dict(ema_fast=3, ema_slow=8, rsi_period=50))
    assert built2["min_bars"] == 51


def test_never_calls_sell_no_short_branch():
    import inspect

    built = _build_ema_trend_rsi(dict(ema_fast=5, ema_slow=20, rsi_period=5))
    source = inspect.getsource(built["strategy"])
    assert "self.sell(" not in source and "_risk_sell" not in source
    assert "crossover(" not in source
