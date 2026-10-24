"""B3B2-K3：v2 内核 equity_fraction 仓位模型与 risk_fraction basis。

两笔成交序列（入口条件恒真、一次一仓、cooldown=0）：
frame 0 触发 -> frame 1 开仓并止盈/止损 -> frame 1 再触发 -> frame 2 开仓 ->
finalize 按 frame 2 收盘平仓。入场价两笔都取 100，便于暴露权益口径的差异。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import test_strategy_kernel as tsk  # noqa: E402
from strategy_kernel import ERR_SPEC_INVALID, StrategyContractError  # noqa: E402

VALUE_PATH = "$.strategy_spec.risk.position_sizing.value"
BASIS_PATH = "$.strategy_spec.risk.position_sizing.basis"

# frame 1 止盈（long，5% 止盈 = 105），frame 2 开仓后无触发，finalize 收盘平仓。
WIN_FRAMES = [
    tsk.frame(0),
    tsk.frame(1, open_="100", high="106", low="99", close="100"),
    tsk.frame(2, open_="100", high="101", low="99", close="100"),
]
# frame 1 止损（long，5% 止损 = 95），frame 2 开仓后无触发，finalize 收盘平仓。
LOSS_FRAMES = [
    tsk.frame(0),
    tsk.frame(1, open_="100", high="101", low="94", close="100"),
    tsk.frame(2, open_="100", high="101", low="99", close="100"),
]


def _spec(*, model: str, value: str, basis: str | None = None) -> dict:
    spec = tsk.make_spec(sizing_model=model, sizing_value=value)
    if basis is not None:
        spec["risk"]["position_sizing"]["basis"] = basis
    return spec


def _run(spec: dict, frames: list):
    plan = tsk.compile_spec(spec)
    return tsk.simulate(
        plan, frames, tsk.initial_state(plan, tsk.execution_params())
    )


def test_equity_fraction_uses_current_equity_after_win():
    # t1: margin=10000*0.5=5000 -> qty=100，止盈 105，盈亏 469.25 -> equity=10469.25
    # t2: margin=10469.25*0.5=5234.625 -> raw=5234.625*2/100=104.6925 -> qty=104.692
    result = _run(_spec(model="equity_fraction", value="0.5"), WIN_FRAMES)
    assert [trade["qty"] for trade in result["trades"]] == ["100", "104.692"]


def test_equity_fraction_uses_current_equity_after_loss():
    # t1: qty=100，止损 95，盈亏 -529.25 -> equity=9470.75
    # t2: margin=9470.75*0.5=4735.375 -> raw=9470.75/100=94.7075 -> qty=94.707
    result = _run(_spec(model="equity_fraction", value="0.5"), LOSS_FRAMES)
    assert [trade["qty"] for trade in result["trades"]] == ["100", "94.707"]


def test_equity_fraction_value_one_is_full_equity_margin():
    # t1: margin=10000 -> qty=200，止盈 105，盈亏 938.5 -> equity=10938.5
    # t2: margin=10938.5 -> raw=10938.5*2/100=218.77 -> qty=218.77
    result = _run(_spec(model="equity_fraction", value="1"), WIN_FRAMES)
    assert [trade["qty"] for trade in result["trades"]] == ["200", "218.77"]


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


def test_risk_fraction_basis_initial_keeps_budget_after_win():
    # t1: budget=10000*0.1=1000 -> qty=200，止盈 105，盈亏 938.5 -> equity=10938.5
    # t2: budget 仍为 1000（不随权益变），止损距 5 -> qty=200
    result = _run(
        _spec(model="risk_fraction", value="0.1", basis="initial"), WIN_FRAMES
    )
    assert [trade["qty"] for trade in result["trades"]] == ["200", "200"]


def test_risk_fraction_basis_equity_grows_qty_after_win():
    # t2: budget=10938.5*0.1=1093.85 -> qty=218.77
    result = _run(
        _spec(model="risk_fraction", value="0.1", basis="equity"), WIN_FRAMES
    )
    assert [trade["qty"] for trade in result["trades"]] == ["200", "218.77"]


def test_risk_fraction_default_basis_matches_explicit_equity():
    # 缺省行为必须与改前的「按当前权益」逐笔一致：等价于显式 basis=equity。
    default = _run(_spec(model="risk_fraction", value="0.1"), WIN_FRAMES)
    explicit = _run(
        _spec(model="risk_fraction", value="0.1", basis="equity"), WIN_FRAMES
    )
    assert [t["qty"] for t in default["trades"]] == ["200", "218.77"]
    assert [t["qty"] for t in default["trades"]] == [
        t["qty"] for t in explicit["trades"]
    ]
    assert default["equity_curve"] == explicit["equity_curve"]


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
