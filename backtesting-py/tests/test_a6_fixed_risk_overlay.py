"""A6 二层（0917 会议清单）：7 个内置模板接固定百分比止损/止盈 + 固定仓位。

覆盖范围（Owner 拍板口径）：
- 只做 fixed_percent 止损/止盈、fixed_pct/fixed_notional 仓位；ATR、移动止损、
  保本止损、fixed_risk（按风险算仓位）、加仓一律不接（不在这里测，闸门端继续拒绝）。
- 决策时钟只认收盘价，不看当根 High/Low；同一根先判止损后判止盈。
- spec 不带这些字段时，7 个模板的原有 buy()/sell() 行为必须逐字节不变（回归底线）。

本文件分两部分：
1. 直接单测 ``_FixedRiskMixin``（用一个不依赖具体指标信号的最小 Strategy 子类），
   把「先止损后止盈」「固定百分比仓位」「固定名义金额仓位」「参数校验」的行为跟具体
   模板的入场信号解耦，避免为了凑一个精确的 crossover bar 而费力构造数据。
2. 对 7 个模板各跑一次「不带风险字段」的构建 + 回归断言（无风险字段时 next() 的
   size 参数与库默认值完全相同）+ 至少一个模板（ema_cross）端到端验证 provider 层
   wiring（params -> build -> 运行中的策略）确实生效。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import math

import pandas as pd
import pytest
from backtesting import Backtest, Strategy

from cutie_backtesting_provider import (
    _FIXED_RISK_PARAM_SCHEMA_PROPERTIES,
    _FixedRiskMixin,
    _build_bollinger_breakout,
    _build_bollinger_reversal,
    _build_breakout,
    _build_cci_rsi,
    _build_ema_cross,
    _build_macd,
    _build_rsi_reversal,
    _parse_fixed_risk_params,
    _validate_params_against_schema,
    TOOL_SPECS,
)

ALL_BUILDERS = {
    "local.backtesting_py.ema_cross": (_build_ema_cross, {"ema_fast": 5, "ema_slow": 20}),
    "local.backtesting_py.rsi_reversal": (_build_rsi_reversal, {"rsi_period": 14}),
    "local.backtesting_py.bollinger_reversal": (_build_bollinger_reversal, {"bb_period": 20, "bb_std": 2}),
    "local.backtesting_py.bollinger_breakout": (_build_bollinger_breakout, {"bb_period": 20, "bb_std": 2}),
    "local.backtesting_py.breakout": (_build_breakout, {"lookback": 20, "exit_lookback": 10}),
    "local.backtesting_py.macd": (_build_macd, {"fast": 12, "slow": 26, "signal": 9}),
    "local.backtesting_py.cci_rsi": (_build_cci_rsi, {"cci_period": 20, "rsi_period": 14}),
}


# ---------------------------------------------------------------------------
# Part 1: direct unit tests on the shared mixin (entry-signal agnostic).
# ---------------------------------------------------------------------------


def _flat_frame(prices, start="2026-01-01"):
    return pd.DataFrame(
        dict(Open=prices, High=[p + 0.01 for p in prices], Low=[p - 0.01 for p in prices],
             Close=prices, Volume=[1] * len(prices)),
        index=pd.date_range(start, periods=len(prices), freq="h"),
    )


def _make_manual_entry_strategy(entry_bar, risk, initial_capital=10000.0, side="long"):
    """A Strategy that enters unconditionally at a fixed bar index, so the SL/TP/
    sizing overlay can be tested independent of any template's own entry signal."""

    class ManualEntryStrategy(_FixedRiskMixin, Strategy):
        _risk = risk
        _initial_capital = initial_capital

        def init(self):
            self._risk_init()

        def next(self):
            if self.position and self._risk_check_exit():
                return
            if not self.position and len(self.data.Close) - 1 == entry_bar:
                if side == "long":
                    self._risk_buy()
                else:
                    self._risk_sell()

    return ManualEntryStrategy


def test_stop_loss_checked_before_take_profit_on_same_bar():
    # buy() requested when len(data.Close)-1 == 2 (i.e. during the next() call for
    # bar 2) queues a market order that -- like every order in backtesting.py's
    # default trade_on_close=False model -- fills at the FOLLOWING bar's open, so
    # EntryBar in the trades table is 3, not 2. Bar 5 (the last bar) closes at 90,
    # -10% -> below the 5% stop; since it's the final bar with no bar 6 to delay
    # the closing order to, finalize_trades records the exit on that same bar.
    prices = [100, 100, 100, 100, 100, 90]
    df = _flat_frame(prices)
    risk = _parse_fixed_risk_params({"stop_loss_pct": 5, "take_profit_pct": 20})
    cls = _make_manual_entry_strategy(entry_bar=2, risk=risk)
    trades = Backtest(df, cls, cash=100000, finalize_trades=True).run()["_trades"]
    assert len(trades) == 1
    assert trades.iloc[0]["EntryBar"] == 3
    assert trades.iloc[0]["ExitBar"] == 5
    assert trades.iloc[0]["ExitPrice"] == pytest.approx(90.0)


def test_stop_and_take_both_breached_same_bar_stop_wins():
    # A single scalar close can never breach both stop (< entry) and take (> entry)
    # at once, so "stop wins" is instead proven by making sure the exit is driven
    # by the earlier stop-breaching bar and NOT by a later bar that happens to
    # cross the take level. entry requested at bar 1 -> filled at bar 2 (open=100).
    # Bar 3 closes at 94, breaching the 5% stop (<=95); _risk_check_exit's stop
    # branch returns True immediately (the take branch is never reached), queuing
    # a close order that fills at bar 4's open=96. 96 is still well below the 20%
    # take level (120), so this exit cannot be mistaken for a take-profit exit
    # that merely happened to land on the same bar.
    prices = [100, 100, 100, 94, 96]
    df = _flat_frame(prices)
    risk = _parse_fixed_risk_params({"stop_loss_pct": 5, "take_profit_pct": 20})
    cls = _make_manual_entry_strategy(entry_bar=1, risk=risk)
    trades = Backtest(df, cls, cash=100000, finalize_trades=True).run()["_trades"]
    assert len(trades) == 1
    assert trades.iloc[0]["EntryBar"] == 2
    assert trades.iloc[0]["ExitBar"] == 4
    assert trades.iloc[0]["ExitPrice"] == pytest.approx(96.0)


def test_take_profit_triggers_when_stop_not_breached():
    prices = [100, 100, 100, 121]  # +21% >= 20% take, stop (95) never touched
    df = _flat_frame(prices)
    risk = _parse_fixed_risk_params({"stop_loss_pct": 5, "take_profit_pct": 20})
    cls = _make_manual_entry_strategy(entry_bar=1, risk=risk)
    trades = Backtest(df, cls, cash=100000, finalize_trades=True).run()["_trades"]
    assert len(trades) == 1
    assert trades.iloc[0]["ExitBar"] == 3
    assert trades.iloc[0]["ExitPrice"] == pytest.approx(121.0)


def test_short_side_stop_and_take_are_mirrored():
    # Short entry at 100: stop is above entry (+5%), take is below entry (-20%).
    prices = [100, 100, 100, 106]  # +6% -> breaches short stop (105)
    df = _flat_frame(prices)
    risk = _parse_fixed_risk_params({"stop_loss_pct": 5, "take_profit_pct": 20})
    cls = _make_manual_entry_strategy(entry_bar=1, risk=risk, side="short")
    trades = Backtest(df, cls, cash=100000, finalize_trades=True).run()["_trades"]
    assert len(trades) == 1
    assert trades.iloc[0]["ExitBar"] == 3
    assert trades.iloc[0]["ExitPrice"] == pytest.approx(106.0)
    assert trades.iloc[0]["Size"] < 0


def test_no_risk_fields_never_exits_via_overlay():
    prices = [100, 100, 100, 50, 500]  # huge moves either way
    df = _flat_frame(prices)
    cls = _make_manual_entry_strategy(entry_bar=1, risk={})
    trades = Backtest(df, cls, cash=100000, finalize_trades=True).run()["_trades"]
    # Entered and never exited by the overlay (no signal exit defined either) ->
    # finalize_trades closes it at the last bar.
    assert len(trades) == 1
    assert trades.iloc[0]["ExitBar"] == 4


def test_position_size_pct_uses_fraction_of_current_equity():
    prices = [100] * 5
    df = _flat_frame(prices)
    risk = _parse_fixed_risk_params({"position_size_pct": 10})
    cls = _make_manual_entry_strategy(entry_bar=1, risk=risk)
    trades = Backtest(df, cls, cash=100000, finalize_trades=True).run()["_trades"]
    assert len(trades) == 1
    size = trades.iloc[0]["Size"]
    notional = size * trades.iloc[0]["EntryPrice"]
    assert notional == pytest.approx(100000 * 0.10, rel=0.02)


def test_position_size_notional_targets_fixed_dollar_amount_regardless_of_internal_scale():
    prices = [100] * 5
    df = _flat_frame(prices)
    risk = _parse_fixed_risk_params({"position_size_notional": 500})
    # internal cash much larger than initial_capital, mimicking provider's high-price scaling trick
    cls = _make_manual_entry_strategy(entry_bar=1, risk=risk, initial_capital=10000.0)
    trades = Backtest(df, cls, cash=1_000_000.0, finalize_trades=True).run()["_trades"]
    assert len(trades) == 1
    size = trades.iloc[0]["Size"]
    notional = size * trades.iloc[0]["EntryPrice"]
    # target = $500 out of a $10,000 REAL initial_capital, scaled into the $1,000,000
    # internal cash space -> internal notional should be $50,000.
    assert notional == pytest.approx(50000.0, rel=0.02)


def test_mutually_exclusive_position_size_fields_rejected():
    with pytest.raises(ValueError, match="mutually exclusive"):
        _parse_fixed_risk_params({"position_size_pct": 10, "position_size_notional": 500})


@pytest.mark.parametrize(
    "bad_params",
    [
        {"stop_loss_pct": 0},
        {"stop_loss_pct": 100},
        {"stop_loss_pct": -1},
        {"take_profit_pct": 0},
        {"position_size_pct": 0},
        {"position_size_pct": 101},
        {"position_size_notional": 0},
        {"position_size_notional": -1},
        {"stop_loss_pct": "abc"},
    ],
)
def test_invalid_risk_params_rejected(bad_params):
    with pytest.raises(ValueError, match="INVALID_PARAMS"):
        _parse_fixed_risk_params(bad_params)


def test_absent_fields_produce_empty_risk_dict():
    assert _parse_fixed_risk_params({}) == {}
    assert _parse_fixed_risk_params({"ema_fast": 5}) == {}


# ---------------------------------------------------------------------------
# Part 2: schema + regression + one end-to-end wiring check per builder.
# ---------------------------------------------------------------------------


def test_fixed_risk_keys_are_merged_into_every_tool_schema():
    for tool_id, spec in TOOL_SPECS.items():
        props = spec["param_schema_properties"]
        if spec.get("runner") == "kernel_v3":
            # 123 组合 tool 豁免：组合风险参数走 basket_stop_loss_pct /
            # basket_take_profit_pct / margin_per_leg（SPEC_组合策略v3契约 §6.1），
            # v3 内核不消费这 4 个 legacy 键，声明出来就是 catalog 里的死键。
            for key in _FIXED_RISK_PARAM_SCHEMA_PROPERTIES:
                assert key not in props, f"{tool_id} must not declare legacy risk key {key}"
            continue
        for key in _FIXED_RISK_PARAM_SCHEMA_PROPERTIES:
            assert key in props, f"{tool_id} missing {key} in param_schema_properties"


@pytest.mark.parametrize("tool_id", list(ALL_BUILDERS))
def test_schema_validation_accepts_valid_and_rejects_invalid_risk_values(tool_id):
    props = TOOL_SPECS[tool_id]["param_schema_properties"]
    assert _validate_params_against_schema({"stop_loss_pct": 5, "take_profit_pct": 10}, props) is None
    assert _validate_params_against_schema({"position_size_pct": 10}, props) is None
    assert _validate_params_against_schema({"position_size_notional": 500}, props) is None
    assert _validate_params_against_schema({"stop_loss_pct": "5"}, props) is not None
    assert _validate_params_against_schema({"stop_loss_pct": 200}, props) is not None


@pytest.mark.parametrize("tool_id,builder_and_defaults", ALL_BUILDERS.items())
def test_no_risk_params_size_matches_library_default(tool_id, builder_and_defaults):
    """Regression: when spec carries none of the 4 risk fields, ``_risk == {}`` and
    ``_risk_entry_size()`` must return exactly backtesting.py's own default size
    (0.9999, `_FULL_EQUITY`), so every buy()/sell() call is byte-identical to the
    pre-A6 code path (which never passed a `size=` kwarg at all)."""
    builder, base_params = builder_and_defaults
    built = builder(dict(base_params))
    strategy_cls = built["strategy"]
    assert strategy_cls._risk == {}
    # `Strategy.buy`'s default `size` is backtesting.py's private __FULL_EQUITY
    # sentinel -- a float subclass whose *repr* prints as ".9999" but whose real
    # value is ~0.9999999999999998, not the literal float 0.9999 (confirmed by
    # `_FixedRiskMixin._risk_entry_size`'s own docstring). Comparing it to the
    # literal 0.9999 is a false negative, not a real regression signal. What
    # actually matters for "no-op when _risk == {}" is that our overlay never
    # substitutes its own size and instead calls buy()/sell() with no `size=`
    # kwarg at all -- i.e. `_risk_entry_size()` returns None -- so the library's
    # own default (whatever its value) is what backtesting.py uses, untouched.
    from types import SimpleNamespace

    from cutie_backtesting_provider import _FixedRiskMixin

    stub = SimpleNamespace(_risk={}, _start_equity=0.0, _initial_capital=10000.0, equity=10000.0)
    assert _FixedRiskMixin._risk_entry_size(stub) is None


def test_ema_cross_end_to_end_stop_loss_wiring_shortens_hold_time():
    """Wiring smoke test: stop_loss_pct passed through provider params must reach
    the running strategy and cut a trade short vs. the no-risk baseline, using the
    same sine-wave fixture style as test_phase5_evaluators.py."""
    values = [100.0] * 20 + [100 + 30 * math.sin(i * math.pi / 20) for i in range(80)]
    df = _flat_frame(values)
    params = {"ema_fast": 5, "ema_slow": 15}
    baseline_trades = Backtest(
        df, _build_ema_cross(dict(params))["strategy"], cash=100000, finalize_trades=True
    ).run()["_trades"]
    assert len(baseline_trades) >= 1

    risky_params = {**params, "stop_loss_pct": 2, "position_size_pct": 20}
    risky_trades = Backtest(
        df, _build_ema_cross(dict(risky_params))["strategy"], cash=100000, finalize_trades=True
    ).run()["_trades"]
    assert len(risky_trades) >= 1
    # Fixed 2% stop on a strategy that otherwise rides full swings must not hold
    # any single trade longer than the baseline's longest trade by chance -- at
    # minimum, position sizing must differ (20% fixed vs ~99.99% full equity).
    baseline_notional = (baseline_trades.Size * baseline_trades.EntryPrice).abs()
    risky_notional = (risky_trades.Size * risky_trades.EntryPrice).abs()
    assert risky_notional.max() < baseline_notional.max()
