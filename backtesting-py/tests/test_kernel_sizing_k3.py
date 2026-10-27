"""B3B2-K3/K3b：v2 内核仓位与既有模板逐值同口径。

口径（勿重议）：不写仓位 = equity_fraction 1；「每笔 N%」= equity_fraction N/100；
「每笔风险 N%」= risk_fraction + basis initial（模板 compound=False），
basis equity 对应模板 compound=True。杠杆照乘，做空同口径。

期望值一律现场跑模板路径得出（backtesting.py 引擎 + _FixedRiskMixin /
PositionSizingMixin），不手抄数字：同一组参数（杠杆、手续费、滑点、qty_step
全部取自内核 spec 与 execution_params）分别算模板与 v2 内核，断言数量与拒单原因逐值相等。
模板侧引擎一单位 = 内核 qty_step（与模板 sizing 模式「一单位即一 qty_step」同约定）。
"""

from __future__ import annotations

import sys
import warnings
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from backtesting import Backtest, Strategy

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cutie_backtesting_provider as provider  # noqa: E402
import test_strategy_kernel as tsk  # noqa: E402
from strategy_kernel import ERR_SPEC_INVALID, StrategyContractError  # noqa: E402

VALUE_PATH = "$.strategy_spec.risk.position_sizing.value"
BASIS_PATH = "$.strategy_spec.risk.position_sizing.basis"

# 入场 frame 1 开盘 100；高低价不触及 5% 止损 / 止盈，finalize 按收盘平仓。
FLAT_FRAMES = [
    tsk.frame(0),
    tsk.frame(1, open_="100", high="100.1", low="99.9", close="100"),
]
# frame 1 止盈（long 5% = 105）后再触发，frame 2 开第二笔。
WIN_FRAMES = [
    tsk.frame(0),
    tsk.frame(1, open_="100", high="106", low="99", close="100"),
    tsk.frame(2, open_="100", high="101", low="99", close="100"),
]


def _spec(*, model: str, value: str, side: str = "long", basis: str | None = None,
          fee: str | None = None, slip: str | None = None, leverage: str | None = None,
          time_exit_bars: int | None = None) -> dict:
    spec = tsk.make_spec(sizing_model=model, sizing_value=value, side=side,
                         time_exit_bars=time_exit_bars)
    if basis is not None:
        spec["risk"]["position_sizing"]["basis"] = basis
    if leverage is not None:
        spec["risk"]["leverage"] = leverage
    cost = spec["execution"]["cost_model"]
    if fee is not None:
        cost["fee_bps"] = fee
    if slip is not None:
        cost["slippage_bps"] = slip
    return spec


def _kernel(spec: dict, frames: list, *, initial: Decimal, equity: Decimal):
    plan = tsk.compile_spec(spec)
    params = tsk.execution_params(sizing_capital=str(initial))
    cost = spec["execution"]["cost_model"]
    params["fee_bps"], params["slippage_bps"] = cost["fee_bps"], cost["slippage_bps"]
    state = tsk.initial_state(plan, params)
    # 模拟此前已有盈亏：当前权益与初始资金不同，暴露 basis 口径差异。
    state.equity = equity
    return tsk.simulate(plan, frames, state)


def _kernel_entry(spec: dict, *, initial: Decimal, equity: Decimal):
    result = _kernel(spec, FLAT_FRAMES, initial=initial, equity=equity)
    qty = Decimal(result["trades"][0]["qty"]) if result["trades"] else None
    reasons = [d["reason"] for d in result["diagnostics"] if d["kind"] == "order_rejected"]
    return qty, reasons


def _template_entry(spec: dict, params: dict, *, initial: Decimal, equity: Decimal):
    """在模板引擎里以 next-open 开一笔，返回 (用户单位数量 | None, 拒单原因)。"""
    qtys, reasons = _template_run(spec, params, initial=initial, equity=equity)
    return (qtys[0] if qtys else None), reasons


def _template_run(spec: dict, params: dict, *, initial: Decimal, equity: Decimal,
                  round_trips: int = 1):
    """模板引擎独立跑：bar 1、5 发开仓信号（next-open 成交），bar 3 平仓；价格恒定。
    返回 (各笔用户单位数量, 拒单原因)。"""
    cost = spec["execution"]["cost_model"]
    fee, slip = Decimal(cost["fee_bps"]), Decimal(cost["slippage_bps"])
    leverage = int(spec["risk"]["leverage"])
    step = Decimal(tsk.execution_params()["provider_params"]["instrument_rules"]["qty_step"])
    side = spec["entry"]["side"]
    params = {**params, "leverage": leverage}
    if "position_size_risk_pct" in params:
        params["position_size_qty_step"] = float(step)
        params["stop_loss_pct"] = float(Decimal(spec["exit"]["stop_loss"]["value"]) * 100)
        params["risk_layer_enabled"] = True

    class Template(provider._FixedRiskMixin, Strategy):
        _risk = provider._parse_fixed_risk_params(params)
        _initial_capital = initial

        def init(self):
            self._risk_init()

        def next(self):
            bar = len(self.data) - 1
            if self.position and round_trips > 1 and bar == 3:
                self.position.close()
            elif not self.position and bar in (1, 5)[:round_trips]:
                self._risk_buy() if side == "long" else self._risk_sell()

    Template._sizing_scale = Template._isolated_equity_scale = step
    Template._isolated_initial_capital = initial
    Template._isolated_fee_bps = fee
    Template._isolated_slippage_bps = slip
    price = float(FLAT_FRAMES[1].values["open"])
    data = pd.DataFrame(
        dict(Open=[price] * 8, Close=[price] * 8, High=[price] * 8, Low=[price] * 8, Volume=1),
        index=pd.date_range("2026-01-01", periods=8, freq="h"),
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = Backtest(
            data, Template, cash=float(equity / step), commission=float(fee / 10000),
            exclusive_orders=True, finalize_trades=True,
            **provider._leverage_backtest_kwargs(leverage),
        ).run()
    qtys = [abs(Decimal(str(size))) * step for size in result["_trades"].Size]
    report = getattr(result["_strategy"], "_sizing_report", None)
    reasons = [r["reason"] for r in report["rejections"]] if report else []
    return qtys, reasons


# (名称, 内核 sizing, 模板参数)。模板参数里 position_size_pct 是百分数，内核是比例。
CASES = [
    ("full_equity", ("equity_fraction", "1", None), {}),
    ("half_equity", ("equity_fraction", "0.5", None), {"position_size_pct": 50}),
    ("risk_initial_ok", ("risk_fraction", "0.05", "initial"),
     {"position_size_risk_pct": 5, "compound": False}),
    ("risk_initial_short_of_fees", ("risk_fraction", "0.1", "initial"),
     {"position_size_risk_pct": 10, "compound": False}),
    ("risk_equity_ok", ("risk_fraction", "0.05", "equity"),
     {"position_size_risk_pct": 5, "compound": True}),
    ("risk_equity_short_of_fees", ("risk_fraction", "0.1", "equity"),
     {"position_size_risk_pct": 10, "compound": True}),
]
# (initial, equity)：未盈亏 / 已有盈利（当前权益高于初始资金）。
CAPITALS = [(Decimal("10000"), Decimal("10000")), (Decimal("8000"), Decimal("12000"))]


@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize("capital", CAPITALS, ids=["flat", "after_gain"])
@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_kernel_entry_matches_template(case, capital, side):
    _, (model, value, basis), params = case
    initial, equity = capital
    spec = _spec(model=model, value=value, side=side, basis=basis)
    template = _template_entry(spec, params, initial=initial, equity=equity)
    kernel = _kernel_entry(spec, initial=initial, equity=equity)
    assert kernel == template


@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize("name", ["risk_initial_short_of_fees", "risk_equity_short_of_fees"])
def test_short_of_fees_is_rejected_with_template_reason(name, side):
    # 钉住：上面逐值相等不是「两边都 None」之外的巧合——此组参数模板确实拒单。
    _, (model, value, basis), params = next(c for c in CASES if c[0] == name)
    spec = _spec(model=model, value=value, side=side, basis=basis)
    capital = dict(initial=Decimal("10000"), equity=Decimal("10000"))
    template = _template_entry(spec, params, **capital)
    assert template == (None, ["insufficient_funds_after_fees"])
    assert _kernel_entry(spec, **capital) == template


@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize("name", ["full_equity", "half_equity"])
def test_percentage_entry_reserves_commission(name, side):
    # 模板按 价格+佣金 折算数量，严格小于不计佣金的 权益*比例*杠杆/价格。
    _, (model, value, basis), params = next(c for c in CASES if c[0] == name)
    spec = _spec(model=model, value=value, side=side, basis=basis)
    qty, _ = _kernel_entry(spec, initial=Decimal("10000"), equity=Decimal("10000"))
    gross = Decimal("10000") * Decimal(value) * Decimal(spec["risk"]["leverage"]) / Decimal(
        FLAT_FRAMES[1].values["open"]
    )
    assert qty is not None and qty < gross


# 内核两笔平价往返：frame 1 开仓、time_exit 按 frame 1 收盘平仓，frame 2 再开、finalize 平仓。
ROUND_TRIP_FRAMES = [
    tsk.frame(0),
    tsk.frame(1, open_="100", high="100.1", low="99.9", close="100"),
    tsk.frame(2, open_="100", high="100.1", low="99.9", close="100"),
]
HALF = ("equity_fraction", "0.5", None, {"position_size_pct": 50})
RISK_INITIAL = ("risk_fraction", "0.02", "initial", {"position_size_risk_pct": 2, "compound": False})
RISK_EQUITY = ("risk_fraction", "0.02", "equity", {"position_size_risk_pct": 2, "compound": True})


def _both_runs(sizing, *, side="long", fee="10", slip="5", leverage="2"):
    """两条路径从同一起点（权益 = 初始资金 10000）各自独立跑完两笔，互不借用对方权益。"""
    model, value, basis, params = sizing
    spec = _spec(model=model, value=value, side=side, basis=basis, fee=fee, slip=slip,
                 leverage=leverage, time_exit_bars=1)
    initial = Decimal("10000")
    kernel = _kernel(spec, ROUND_TRIP_FRAMES, initial=initial, equity=initial)
    template, _ = _template_run(spec, params, initial=initial, equity=initial, round_trips=2)
    return [Decimal(t["qty"]) for t in kernel["trades"]], template


@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize("leverage", ["1", "2"])
@pytest.mark.parametrize("sizing", [HALF, RISK_INITIAL, RISK_EQUITY], ids=["half", "risk_initial", "risk_equity"])
def test_two_round_trips_match_template_without_slippage(sizing, leverage, side):
    kernel, template = _both_runs(sizing, side=side, slip="0", leverage=leverage)
    assert len(kernel) == 2
    assert kernel == template


@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize("sizing", [HALF, RISK_INITIAL, RISK_EQUITY], ids=["half", "risk_initial", "risk_equity"])
def test_two_round_trips_match_template_with_slippage_and_leverage(sizing, side):
    # 杠杆 > 1 时模板按逐仓结算滑点并计入后续定量，两路一致（半仓 99.900 -> 99.600）。
    kernel, template = _both_runs(sizing, side=side, slip="5", leverage="2")
    assert len(kernel) == 2
    assert kernel == template


@pytest.mark.parametrize("side", ["long", "short"])
def test_known_gap_slippage_not_settled_by_template_at_leverage_one(side):
    # 已知差异（模板怪癖，已登记待 Owner 定，内核不照抄）：杠杆 1 + 比例仓位时，
    # 模板的滑点只进报表、不扣现金，第二笔按未扣滑点的权益定量；内核扣了滑点。
    # 模板哪天修了，这条会红，届时改成逐值相等。
    kernel, template = _both_runs(HALF, side=side, slip="5", leverage="1")
    assert kernel[0] == template[0] == Decimal("49.950")
    assert template[1] == Decimal("49.900")
    assert kernel[1] == Decimal("49.875")


@pytest.mark.parametrize("side", ["long", "short"])
def test_known_gap_full_equity_sentinel(side):
    # 已知差异（模板怪癖，已登记待 Owner 定，内核不照抄）：模板不写仓位时用
    # backtesting.py 的 _FULL_EQUITY = 1 - ε 哨兵，零费用、整除时少一个 qty_step；
    # 内核按口径取比例 1。模板哪天修了，这条会红，届时改成逐值相等。
    spec = _spec(model="equity_fraction", value="1", side=side, fee="0", slip="0")
    capital = dict(initial=Decimal("10000"), equity=Decimal("10000"))
    assert _template_entry(spec, {}, **capital) == (Decimal("199.999"), [])
    assert _kernel_entry(spec, **capital) == (Decimal("200"), [])


def test_risk_fraction_default_basis_matches_explicit_equity():
    # 缺省行为必须与显式 basis=equity 逐笔一致。
    default = _kernel(_spec(model="risk_fraction", value="0.05"), WIN_FRAMES,
                      initial=Decimal("10000"), equity=Decimal("10000"))
    explicit = _kernel(_spec(model="risk_fraction", value="0.05", basis="equity"), WIN_FRAMES,
                       initial=Decimal("10000"), equity=Decimal("10000"))
    assert len(default["trades"]) == 2
    assert [t["qty"] for t in default["trades"]] == [t["qty"] for t in explicit["trades"]]
    assert default["equity_curve"] == explicit["equity_curve"]


def test_equity_fraction_rejects_value_above_one():
    with pytest.raises(StrategyContractError) as caught:
        tsk.compile_spec(_spec(model="equity_fraction", value="1.01"))
    assert caught.value.code == ERR_SPEC_INVALID
    assert caught.value.path == VALUE_PATH


def test_equity_fraction_rejects_nonpositive_value():
    with pytest.raises(StrategyContractError) as caught:
        tsk.compile_spec(_spec(model="equity_fraction", value="0"))
    assert caught.value.code == ERR_SPEC_INVALID
    assert caught.value.path == VALUE_PATH


def test_risk_fraction_rejects_unknown_basis():
    with pytest.raises(StrategyContractError) as caught:
        tsk.compile_spec(
            _spec(model="risk_fraction", value="0.1", basis="initial_capital")
        )
    assert caught.value.code == ERR_SPEC_INVALID
    assert caught.value.path == BASIS_PATH


def test_basis_is_rejected_for_other_models():
    with pytest.raises(StrategyContractError) as caught:
        tsk.compile_spec(
            _spec(model="equity_fraction", value="0.5", basis="initial")
        )
    assert caught.value.code == ERR_SPEC_INVALID
    assert caught.value.path == BASIS_PATH
