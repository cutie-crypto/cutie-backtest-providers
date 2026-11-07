"""PROV-ROLLING-MEAN-1107：内核补 ``rolling_mean``（SMA）算子。

TokenBeep 的「BTC 日线收盘价高于 SMA100 才做多」只能靠 EMA 近似，SMA 卡在
provider 没有这个算子（Owner 11-05 定 SMA / EMA 都要）。本文件钉死四件事：

1. 取值口径：``rolling_mean`` = 同窗 sum ÷ window_bars；窗口不满的前几根为缺失，
   与 ``rolling_sum`` / ``rolling_extreme`` 同口径（``rolling_quantile`` 的
   ``min_periods`` 宽松是唯一的例外，rolling_mean 不沿用）。
2. 参数口径：只收 ``window_bars``（整数），上下限照 ``ema``（2..10000）。
3. 可用性：``close gt rolling_mean(close, 3)`` 在确定性序列上的首根为真下标等于
   手算值；kline.primary 直通特征不再被 rolling_sum/rolling_extreme 的
   ``window_bars=1`` 限制卡住，而是由 primary ``warmup_bars`` 覆盖 window_bars。
4. 能力声明：``feature_primitives`` 登记 ``rolling_mean``，金样 hash 与重算一致。
"""
from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import test_strategy_kernel as tsk  # noqa: E402
import test_strategy_kernel_primitives as tkp  # noqa: E402
from strategy_kernel import (  # noqa: E402
    ERR_SPEC_INVALID,
    StrategyContractError,
    StrategyKernel,
    _evaluate_windowed_primitive,
    _validate_capability,
    _validate_primitive_params,
    build_frames,
    capability_hash,
    capability_payload,
    compile_strategy,
)

CAPABILITY_FIXTURE = (
    Path(__file__).parent / "fixtures" / "strategy_execution_capability_v1.json"
)
PRIMARY_STREAM = "binance.futures.kline.1h"
COVERAGE = {
    "summary": {"strict_eligible": True},
    "request_identity": {"symbol": "BTCUSDT"},
}
HOUR = 3600


def _feature(key: str, primitive: str, params: dict) -> dict:
    return {
        "key": key,
        "primitive": primitive,
        "primitive_version": "1",
        "source_stream": "kline.primary.close",
        "interval": "1h",
        "value_kind": "price",
        "output_type": "decimal",
        "params": params,
        "required": True,
    }


def _rows(closes: list[str]) -> list[dict]:
    return [
        {
            "open_time": index * HOUR,
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": "1",
        }
        for index, close in enumerate(closes)
    ]


def test_rolling_mean_values() -> None:
    """close = 1..10、window_bars=3：前 2 根缺失，第 3 根 = 2.0，第 10 根 = 9.0。"""
    closes = [str(value) for value in range(1, 11)]
    ordered = [index * HOUR for index in range(len(closes))]
    rows_by_ts = {
        index * HOUR: (Decimal(close), index * HOUR + HOUR, "r1")
        for index, close in enumerate(closes)
    }
    feature = {
        "key": "sma3",
        "primitive": "rolling_mean",
        "params": {"window_bars": 3},
        "interval": "1h",
    }
    series = []
    for ts in ordered:
        outcome = _evaluate_windowed_primitive(
            feature, rows_by_ts, ordered, ts, HOUR, "$.under_test"
        )
        series.append(None if outcome is None else outcome[0])

    # 预热口径同 rolling_sum / rolling_extreme：窗口不满 = 缺失，不做部分平均。
    assert series[:2] == [None, None]
    assert series[2] == Decimal("2.0")
    assert series[9] == Decimal("9.0")
    assert [str(value) for value in series[2:]] == ["2", "3", "4", "5", "6", "7", "8", "9"]


@pytest.mark.parametrize(
    "window_bars",
    [
        0,  # 下界之下
        -3,  # 负数
        2.5,  # 非整数
        "3",  # 非整数（字符串）
        10001,  # 上限（10000）之上
        1,  # rolling_sum 允许但 rolling_mean 不允许（下界照 ema = 2）
    ],
)
def test_rolling_mean_param_bounds(window_bars: object) -> None:
    with pytest.raises(StrategyContractError) as caught:
        _validate_primitive_params(
            "rolling_mean", {"window_bars": window_bars}, "$.under_test"
        )
    assert caught.value.code == ERR_SPEC_INVALID
    assert caught.value.path == "$.under_test.params.window_bars"


@pytest.mark.parametrize("window_bars", [0, -3, 10001, 1])
def test_rolling_mean_param_bounds_rejected_at_compile(window_bars: int) -> None:
    """同一拒绝在 compile 路径上成立（spec 层，不是只有内部校验器拦得住）。"""
    spec = tsk.make_spec(
        features=[_feature("sma", "rolling_mean", {"window_bars": window_bars})]
    )
    with pytest.raises(StrategyContractError) as caught_compile:
        compile_strategy(
            spec,
            tkp._kline_primary_manifest(
                spec, requirement_interval="1h", warmup_bars=3
            ),
            capability_payload(tsk.REVISION),
        )
    assert caught_compile.value.code == ERR_SPEC_INVALID
    assert (
        caught_compile.value.path
        == "$.strategy_spec.features[0].params.window_bars"
    )


def test_rolling_mean_only_accepts_window_bars() -> None:
    """``rolling_mean`` 只收 ``window_bars``：多了键（如 quantile/mode）直接拒。"""
    with pytest.raises(StrategyContractError) as caught:
        _validate_primitive_params(
            "rolling_mean", {"window_bars": 3, "min_periods": 1}, "$.under_test"
        )
    assert caught.value.code == ERR_SPEC_INVALID
    assert caught.value.path == "$.under_test.params"


def test_rolling_mean_compare_close() -> None:
    """``close gt rolling_mean(close, 3)`` 的首根为真下标 = 手算值。

    序列 10,9,8,7,8,9,10,11,12,13（先降后升），SMA3：
      i=2 (10+9+8)/3 = 9.0   -> 8 > 9.0   否
      i=3 (9+8+7)/3  = 8.0   -> 7 > 8.0   否
      i=4 (8+7+8)/3  = 7.666…-> 8 > 7.667 是  <-- 首根为真
    前 2 根 SMA 缺失（预热），因此判定从 i=2 开始。
    """
    closes = ["10", "9", "8", "7", "8", "9", "10", "11", "12", "13"]
    condition = {
        "node": "compare",
        "op": "gt",
        "left": {"node": "feature", "key": "close_now", "lag_bars": 0},
        "right": {"node": "feature", "key": "sma3", "lag_bars": 0},
    }
    spec = tsk.make_spec(
        condition=condition,
        features=[
            # 直通 close 本身：v2 condition 只有 feature 节点（没有裸 close 引用），
            # rolling_extreme(close, 1, max) 是能力声明里允许的 window_bars=1
            # 恒等形态（单值 max 就是该 close）。
            _feature(
                "close_now", "rolling_extreme", {"window_bars": 1, "mode": "max"}
            ),
            # PROV-ROLLING-MEAN-1107：直通 rolling_mean 可以是宽窗口，不受
            # rolling_sum/rolling_extreme 的 window_bars=1 限制。
            _feature("sma3", "rolling_mean", {"window_bars": 3}),
        ],
    )
    manifest = tkp._kline_primary_manifest(spec, requirement_interval="1h")
    # 直通特征不产生自己的 data requirement；宽窗口的预热由 primary 的
    # warmup_bars 承担（compile 硬性要求它 >= window_bars）。
    next(
        item
        for item in manifest["data_requirements"]
        if item["execution_role"] == "primary_execution_kline"
    )["warmup_bars"] = 3
    plan = compile_strategy(spec, manifest, capability_payload(tsk.REVISION))
    frames = build_frames({PRIMARY_STREAM: _rows(closes)}, COVERAGE, plan)

    # 预热行：primary warmup_bars=3 内 rolling_mean 可以缺失，第 4 行起必须有值。
    assert "sma3" not in frames[0].values
    assert all("sma3" in frame.values for frame in frames[3:])
    assert all("close_now" in frame.values for frame in frames)

    kernel = StrategyKernel.__new__(StrategyKernel)
    kernel.plan = SimpleNamespace(
        feature_types=plan.feature_types, parameter_types={}
    )
    kernel.parameters = {}
    state = SimpleNamespace(frames=frames)
    # 决策帧从 warmup_bars 行开始（前 3 行是预热帧，永不判定），与真内核一致：
    # 预热行 SMA 缺失不会被 _condition 当作 gap fail closed。
    hits = {
        index: kernel._condition(condition, state, index)
        for index in range(3, len(frames))
    }
    assert hits == {
        3: False,
        4: True,
        5: True,
        6: True,
        7: True,
        8: True,
        9: True,
    }
    assert min(index for index, hit in hits.items() if hit) == 4


def test_capability_declares_rolling_mean() -> None:
    capability = capability_payload(tsk.REVISION)
    _validate_capability(capability)
    item = next(
        entry
        for entry in capability["feature_primitives"]
        if entry["primitive"] == "rolling_mean"
    )
    assert item["version"] == "1"
    # 客户用例是 close 序列 -> SMA，能力声明的 source_streams 必须含 close。
    assert "kline.primary.close" in item["source_streams"]
    assert set(item["intervals"]) == {"1d", "1h", "4h"}
    assert item["output_type"] == "decimal"

    fixture = json.loads(CAPABILITY_FIXTURE.read_text(encoding="utf-8"))
    assert capability_hash(fixture["capability"]) == fixture["sha256"]
    assert fixture["sha256"] == capability_hash(capability)
    assert any(
        entry["primitive"] == "rolling_mean"
        for entry in fixture["capability"]["feature_primitives"]
    )
