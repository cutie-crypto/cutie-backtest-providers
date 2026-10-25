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


def _spec(*, model: str, value: str, side: str = "long", basis: str | None = None) -> dict:
    spec = tsk.make_spec(sizing_model=model, sizing_value=value, side=side)
    if basis is not None:
        spec["risk"]["position_sizing"]["basis"] = basis
    return spec


def _kernel(spec: dict, frames: list, *, initial: Decimal, equity: Decimal):
    plan = tsk.compile_spec(spec)
    state = tsk.initial_state(plan, tsk.execution_params(sizing_capital=str(initial)))
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
            if not self.position and len(self.data) - 1 == 1:
                self._risk_buy() if side == "long" else self._risk_sell()

    Template._sizing_scale = Template._isolated_equity_scale = step
    Template._isolated_initial_capital = initial
    Template._isolated_fee_bps = fee
    Template._isolated_slippage_bps = slip
    price = float(FLAT_FRAMES[1].values["open"])
    data = pd.DataFrame(
        dict(Open=[price] * 6, Close=[price] * 6, High=[price] * 6, Low=[price] * 6, Volume=1),
        index=pd.date_range("2026-01-01", periods=6, freq="h"),
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = Backtest(
            data, Template, cash=float(equity / step), commission=float(fee / 10000),
            exclusive_orders=True, finalize_trades=True,
            **provider._leverage_backtest_kwargs(leverage),
        ).run()
    trades = result["_trades"]
    qty = abs(Decimal(str(trades.iloc[0].Size))) * step if len(trades) else None
    report = getattr(result["_strategy"], "_sizing_report", None)
    reasons = [r["reason"] for r in report["rejections"]] if report else []
    return qty, reasons


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


@pytest.mark.parametrize("model,value,basis,params", [
    ("equity_fraction", "0.5", None, {"position_size_pct": 50}),
    ("risk_fraction", "0.05", "equity", {"position_size_risk_pct": 5, "compound": True}),
    ("risk_fraction", "0.05", "initial", {"position_size_risk_pct": 5, "compound": False}),
])
def test_second_trade_sized_on_kernel_equity_matches_template(model, value, basis, params):
    # 第二笔按第一笔平仓后的内核权益重新折算，期望值同样由模板现场算出。
    spec = _spec(model=model, value=value, basis=basis)
    initial = Decimal("10000")
    result = _kernel(spec, WIN_FRAMES, initial=initial, equity=initial)
    first, second = result["trades"][:2]
    equity_after = initial + Decimal(first["pnl"])
    assert (Decimal(first["qty"]), []) == _template_entry(spec, params, initial=initial, equity=initial)
    expected, _ = _template_entry(spec, params, initial=initial, equity=equity_after)
    assert Decimal(second["qty"]) == expected


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
