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
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cutie_backtesting_provider as provider  # noqa: E402
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


def test_rolling_mean_coarse_kline(monkeypatch) -> None:
    """4h ``rolling_mean`` on a 1h decision clock: warmup_bars gate and as-of
    alignment must behave exactly like the same-scenario ``ema`` (both are
    window_bars primitives routed through the same coarse-warmup branch and
    the same as-of anchor), so the two runs are compared directly."""
    monkeypatch.setattr(provider, "PROVIDER_REVISION", tsk.REVISION)

    def fetch_klines(exchange, market, symbol, timeframe, start_ms, end_ms):
        return [[i * 3_600_000, 100, 200, 50, 100 + i, 1] for i in range(24)]

    monkeypatch.setattr(provider, "_fetch_from_central", fetch_klines)

    def build(primitive: str, warmup_bars: int) -> tuple[dict, dict]:
        feature = {
            "key": f"close_4h_{primitive}",
            "primitive": primitive,
            "primitive_version": "1",
            "source_stream": "kline.primary.close",
            "interval": "4h",
            "value_kind": "price",
            "output_type": "decimal",
            "params": {"window_bars": 2},
            "required": True,
        }
        condition = {
            "node": "compare",
            "op": "gt",
            "left": {"node": "feature", "key": feature["key"], "lag_bars": 0},
            "right": {"node": "literal", "value_type": "decimal", "value": "50"},
        }
        spec = tsk.make_spec(condition=condition, features=[feature])
        manifest = tkp._kline_primary_manifest(
            spec, requirement_interval="4h", warmup_bars=warmup_bars
        )
        return spec, manifest

    bodies: dict[str, dict] = {}
    for primitive in ("ema", "rolling_mean"):
        # lookback = window_bars = 2; one bucket short must reject identically.
        spec, manifest = build(primitive, warmup_bars=1)
        with pytest.raises(StrategyContractError) as caught:
            compile_strategy(spec, manifest, capability_payload(tsk.REVISION))
        assert caught.value.code == ERR_SPEC_INVALID
        assert caught.value.required == 2

        spec, manifest = build(primitive, warmup_bars=2)
        request = tkp._kline_primary_request(
            spec, manifest, start_at=57_600, end_at=86_400
        )
        response = TestClient(provider.app).post(
            "/cutie/backtest",
            json=request,
            headers={"X-Cutie-Connector-Version": "1.2.3"},
        )
        assert response.status_code == 200
        bodies[primitive] = response.json()
        assert bodies[primitive]["result_status"] == "success", bodies[primitive]

    derivations = []
    for primitive, body in bodies.items():
        coverage = body["coverage_manifest"]
        derived = next(
            stream
            for stream in coverage["streams"]
            if stream["stream_id"] == "kline.primary.close@4h"
        )
        derivations.append(
            (
                primitive,
                derived["point_count"],
                coverage["transforms"],
                derived["revision"],
            )
        )
    # as-of alignment + resample derivation are primitive-independent here.
    assert {item[1] for item in derivations} == {4}
    assert derivations[0][2] == derivations[1][2]
    assert derivations[0][3] == derivations[1][3]
    # Same visible bucket, same signal: the trades must line up bar for bar.
    assert bodies["ema"]["trades"] == bodies["rolling_mean"]["trades"]
    assert bodies["rolling_mean"]["trades"], "bucket values 100+ clear the 50 bar"


CVD_STREAM_ID = "coinglass.futures_cvd.1h"


def _cvd_sma_lag_request(warmup_bars: int) -> dict:
    """``coinglass.futures_cvd`` + rolling_mean(window_bars=20) referenced at
    lag_bars=1: SPEC §5.5's additive floor is 20 + 1 = 21, counted on the
    feature stream's own requirement (no primary passthrough row to carry it)."""
    feature = {
        "key": "cvd_sma20_1h",
        "primitive": "rolling_mean",
        "primitive_version": "1",
        "source_stream": "coinglass.futures_cvd",
        "interval": "1h",
        "value_kind": "flow",
        "output_type": "decimal",
        "params": {"window_bars": 20},
        "required": True,
    }
    condition = {
        "node": "compare",
        "op": "gt",
        "left": {"node": "feature", "key": "cvd_sma20_1h", "lag_bars": 1},
        "right": {"node": "literal", "value_type": "decimal", "value": "0.5"},
    }
    request = tsk.build_request(
        tsk.make_spec(condition=condition, features=[feature])
    )
    tsk.set_request_warmup(request, CVD_STREAM_ID, warmup_bars)
    request["execution_params"]["start_at"] = 21 * HOUR
    request["execution_params"]["end_at"] = 23 * HOUR
    return request


def test_rolling_mean_feature_stream_lag_warmup_rejected(monkeypatch) -> None:
    """agy review HIGH: the execution preflight's per-primitive warmup floor
    missed ``rolling_mean``, so a non-passthrough stream (cvd) declaring
    warmup_bars=5 for rolling_mean(window_bars=20)+lag_bars=1 passed preflight
    and then died at runtime with ERR_COVERAGE_INCOMPLETE. It must reject as
    ERR_SPEC_INVALID before any data access."""
    monkeypatch.setattr(provider, "PROVIDER_REVISION", tsk.REVISION)

    def fail_if_fetched(*args, **kwargs):
        raise AssertionError(
            "rolling_mean's additive lag floor must be checked before data access"
        )

    monkeypatch.setattr(provider, "_fetch_from_central", fail_if_fetched)
    monkeypatch.setattr(provider, "_fetch_artifact_metric_chunk", fail_if_fetched)

    request = _cvd_sma_lag_request(warmup_bars=5)
    response = TestClient(provider.app).post(
        "/cutie/backtest",
        json=request,
        headers={"X-Cutie-Connector-Version": "1.2.3"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["error_type"] == ERR_SPEC_INVALID
    assert "warmup_bars" in body["error_detail"]["path"]
    assert body["error_detail"]["required"] == {
        "primitive_floor": 20,
        "lag_need": 1,
        "total": 21,
    }


def test_rolling_mean_feature_stream_lag_warmup_ok(monkeypatch) -> None:
    """Companion acceptance case: the same cvd SMA20 window at lag_bars=1
    executes once warmup_bars covers primitive_floor + lag (21)."""
    monkeypatch.setattr(provider, "PROVIDER_REVISION", tsk.REVISION)

    def fetch_klines(exchange, market, symbol, timeframe, start_ms, end_ms):
        return [
            [ts * 1000, 100, 101, 99, 100, 1]
            for ts in range(start_ms // 1000, end_ms // 1000, HOUR)
        ]

    def fetch_metric_chunk(*, symbol, metric, interval, exchange, start_at, end_at):
        return [
            {"ts": ts, "value": "1"} for ts in range(start_at, end_at + 1, HOUR)
        ]

    monkeypatch.setattr(provider, "_fetch_from_central", fetch_klines)
    monkeypatch.setattr(provider, "_fetch_artifact_metric_chunk", fetch_metric_chunk)

    request = _cvd_sma_lag_request(warmup_bars=21)
    response = TestClient(provider.app).post(
        "/cutie/backtest",
        json=request,
        headers={"X-Cutie-Connector-Version": "1.2.3"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["result_status"] == "success", body.get("error_message")


def test_rolling_mean_primary_lag_compile_gate() -> None:
    """kline.primary passthrough rolling_mean(window_bars=3) referenced at
    lag_bars=2 first resolves at row lookback-1, so the primary warmup must
    cover lookback - 1 + lag = 4 (K1d); 3 (the bare window floor) rejects."""
    feature = _feature("sma3", "rolling_mean", {"window_bars": 3})
    condition = {
        "node": "compare",
        "op": "gt",
        "left": {"node": "feature", "key": "sma3", "lag_bars": 2},
        "right": {"node": "literal", "value_type": "decimal", "value": "0"},
    }
    spec = tsk.make_spec(condition=condition, features=[feature])
    manifest = tkp._kline_primary_manifest(spec, requirement_interval="1h")
    primary = next(
        item
        for item in manifest["data_requirements"]
        if item["execution_role"] == "primary_execution_kline"
    )
    primary["warmup_bars"] = 3
    with pytest.raises(StrategyContractError) as caught:
        compile_strategy(spec, manifest, capability_payload(tsk.REVISION))
    assert caught.value.code == ERR_SPEC_INVALID
    assert "warmup_bars" in caught.value.path
    assert caught.value.required == 4

    primary["warmup_bars"] = 4
    plan = compile_strategy(spec, manifest, capability_payload(tsk.REVISION))
    assert plan.feature_types["sma3"] == "decimal"


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
