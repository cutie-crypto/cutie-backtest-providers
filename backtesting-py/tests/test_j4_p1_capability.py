"""JES-J4-P1-CAPABILITY：服务商内核放开周线 / 跨周期，新增 ATR 与标准差，
开放成交量字段。

八个用例由统领定稿（任务书逐条列出），每个都写明修前为什么红：

- ``test_prev_week_extreme_resample``：把 as-of 桶偏一格（用当前周，而不是上一
  个已收完的周），首日就会读到当周高点，红。
- ``test_coarse_1d_on_15m_and_4h_primary``：修前 15m/4h 主周期引用 1d 被硬拒，
  红。
- ``test_week_bucket_no_lookahead``：当周未收完时若能看到当周高低，红；修前
  ``1w`` 不是可 resample 目标，直接拒。
- ``test_atr_pct_filter``：TR 误用当根 c 而不是 c_{-1}，或暖机少一根，手算值
  不符，红。
- ``test_rolling_std_filter``：修前没有 ``rolling_std`` 原语，红。
- ``test_volume_field_rolling_mean``：修前 ``kline.primary.volume`` 不在字段表
  且 price-only 校验拒绝，红。
- ``test_volume_resample_sums``：修前 ``ohlcv_resample`` 不产出 volume，红。
- ``test_capability_declares_min_max_abs``：修前 capability operators 不含
  min/max/abs，红。

JES-J4-P1-CAPABILITY-R1（撤销 ``rolling_sum`` 的成交量声明）追加两条：

- ``test_rolling_sum_does_not_declare_volume``：修前 ``rolling_sum`` 的
  ``source_streams`` 含 ``kline.primary.volume``，红。
- ``test_rolling_sum_volume_spec_rejected_by_capability``：修前 ``window_bars=1``
  的 volume 规格能编译通过，红。

JES-J4-P1-CAPABILITY-R2（``atr_wilder`` 只属于 v2）追加三条：

- ``test_atr_wilder_rejected_by_v3_compile``：修前 ``atr_wilder`` 在 v3 白名单
  里，含它的 v3 spec 编译放行，运行时 ``params["window_bars"]`` 抛 KeyError，红。
- ``test_atr_wilder_still_compiles_on_v2``：移出 v3 白名单不能伤到 v2 路径。
- ``test_rolling_std_v3_evaluates_or_rejected``：核对 ``rolling_std`` 在 v3 的
  ``window_bars`` 求值路径可用，可用就留下并断言数值。
"""

from __future__ import annotations

import sys
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import test_strategy_kernel as tsk  # noqa: E402
import test_strategy_kernel_basket as tsb  # noqa: E402
import test_strategy_kernel_primitives as tkp  # noqa: E402
from canonical_json import canonical_decimal_str  # noqa: E402
from strategy_kernel import (  # noqa: E402
    ERR_SPEC_INVALID,
    ERR_SPEC_UNSUPPORTED,
    _KNOWN_PRIMITIVES,
    _KNOWN_PRIMITIVES_V2,
    StrategyContractError,
    StrategySpecV3Error,
    _DECIMAL_CONTEXT,
    _atr_wilder_series,
    _evaluate_windowed_primitive,
    _primitive_series_v3,
    _validate_capability,
    _validate_primitive_params,
    build_frames,
    build_frames_v3,
    capability_payload,
    compile_strategy,
    compile_strategy_v3,
    kline_primary_bucket_required_end,
    kline_primary_bucket_required_start,
    ohlcv_resample,
)

REVISION = tsk.REVISION
HOUR = 3600
DAY = 86_400
WEEK = 7 * DAY
# 2024-01-01 00:00 UTC is a Monday; Binance weekly bars open there.
MONDAY = 1_704_067_200
COVERAGE = {
    "summary": {"strict_eligible": True},
    "request_identity": {"symbol": "BTCUSDT"},
}
PRIMARY_STREAM = "binance.futures.kline.1h"


def _canonical(value: Decimal) -> str:
    with localcontext(_DECIMAL_CONTEXT):
        return canonical_decimal_str(value)


def _feature(
    key: str,
    primitive: str,
    params: dict,
    *,
    source_stream: str = "kline.primary.close",
    interval: str = "1h",
    value_kind: str = "price",
) -> dict:
    return {
        "key": key,
        "primitive": primitive,
        "primitive_version": "1",
        "source_stream": source_stream,
        "interval": interval,
        "value_kind": value_kind,
        "output_type": "decimal",
        "params": params,
        "required": True,
    }


def _primary_rows(opens: list[tuple[int, int, int, int, int, int]]) -> list[dict]:
    """(open_time, open, high, low, close, volume) -> kernel K-line rows."""
    return [
        {
            "open_time": open_time,
            "open": str(open_value),
            "high": str(high),
            "low": str(low),
            "close": str(close),
            "volume": str(volume),
        }
        for open_time, open_value, high, low, close, volume in opens
    ]


def _manifest_with_primary(
    spec: dict, *, requirement_interval: str, warmup_bars: int = 1
) -> dict:
    """``tkp._kline_primary_manifest`` hardcodes the 1h primary; retarget it to
    the spec's market timeframe (15m/4h/1d) and keep the manifest sorted."""
    manifest = tkp._kline_primary_manifest(
        spec, requirement_interval=requirement_interval, warmup_bars=warmup_bars
    )
    primary_timeframe = spec["market"]["timeframe"]
    for requirement in manifest["data_requirements"]:
        if requirement["execution_role"] == "primary_execution_kline":
            requirement["interval"] = primary_timeframe
            requirement["stream_id"] = f"binance.futures.kline.{primary_timeframe}"
    manifest["data_requirements"].sort(key=lambda item: item["stream_id"])
    manifest["spec_hash"] = tsk.canonical_json_sha256(spec)
    return manifest


def _primary_stream(manifest: dict) -> str:
    return next(
        item["stream_id"]
        for item in manifest["data_requirements"]
        if item["execution_role"] == "primary_execution_kline"
    )


def _daily_week_rows(weeks: int) -> list[dict]:
    """`weeks` complete Monday-based weeks of daily rows; week N high/low are
    110/90 + 10*N so a stale or look-ahead bucket is impossible to confuse."""
    rows = []
    for index in range(weeks * 7):
        week = index // 7
        base = 100 + 10 * week
        rows.append((MONDAY + index * DAY, base, base + 10, base - 10, base, 1))
    return _primary_rows(rows)


def test_prev_week_extreme_resample() -> None:
    """1d primary, 1w ``rolling_extreme(max)``: every frame reads the most
    recent *complete* week. Weeks 0/1/2 high = 110/120/130.

    - Mon..Sat of week 1 -> week 0 (110)
    - Sunday of week 1 closes exactly at week 2's open -> week 1 (120)
    - Mon..Sat of week 2 -> week 1 (120)
    - Sunday of week 2 -> week 2 (130)

    One-bucket-off mutation: anchoring on the current week makes Monday of
    week 1 read 120 (red); anchoring two weeks back loses week 0 (red).
    """
    fetch_rows = _daily_week_rows(weeks=3)
    weekly_buckets = ohlcv_resample(fetch_rows, DAY, "1w")
    assert [bucket["open_time"] for bucket in weekly_buckets] == [
        MONDAY,
        MONDAY + WEEK,
        MONDAY + 2 * WEEK,
    ]
    assert [bucket["high"] for bucket in weekly_buckets] == ["110", "120", "130"]

    feature = _feature(
        "prev_week_high",
        "rolling_extreme",
        {"window_bars": 1, "mode": "max"},
        interval="1w",
    )
    spec = tsk.make_spec(features=[feature])
    spec["market"]["timeframe"] = "1d"
    manifest = _manifest_with_primary(
        spec, requirement_interval="1w", warmup_bars=1
    )
    plan = compile_strategy(spec, manifest, capability_payload(REVISION))

    derived_rows = [
        {
            "ts": bucket["open_time"],
            "value": bucket["high"],
            "available_at": bucket["open_time"] + WEEK,
            "revision": "r1",
        }
        for bucket in weekly_buckets
    ]
    decision_rows = fetch_rows[7:]  # weeks 1 and 2: 14 decision frames
    frames = build_frames(
        {
            _primary_stream(manifest): decision_rows,
            "kline.primary.close.1w": derived_rows,
        },
        COVERAGE,
        plan,
    )
    values = [frame.values["prev_week_high"] for frame in frames]
    assert values == ["110"] * 6 + ["120"] + ["120"] * 6 + ["130"]


def test_coarse_1d_on_15m_and_4h_primary() -> None:
    """15m and 4h primary timeframes may now reference a 1d coarse bucket."""
    feature = _feature(
        "close_1d_high",
        "rolling_extreme",
        {"window_bars": 1, "mode": "max"},
        interval="1d",
    )
    for timeframe in ("15m", "4h"):
        spec = tsk.make_spec(features=[feature])
        spec["market"]["timeframe"] = timeframe
        manifest = _manifest_with_primary(
            spec, requirement_interval="1d", warmup_bars=1
        )
        plan = compile_strategy(spec, manifest, capability_payload(REVISION))
        assert plan.strategy_spec["market"]["timeframe"] == timeframe
        assert any(
            requirement["interval"] == "1d"
            for requirement in manifest["data_requirements"]
        )


def test_week_bucket_no_lookahead() -> None:
    """The current, still-open week is never readable: ``ohlcv_resample`` only
    emits complete weeks, and the as-of anchor is the bucket before the frame's
    own week. In week 1 Mon..Sat the value must stay at week 0's high (110),
    never week 1's (120)."""
    partial_rows = _daily_week_rows(weeks=1)[: 7 + 3]  # week 0 + 3 days of week 1
    buckets = ohlcv_resample(partial_rows, DAY, "1w")
    assert len(buckets) == 1
    assert buckets[0]["open_time"] == MONDAY
    assert buckets[0]["high"] == "110"

    # Fetch/coverage bounds share the same Monday grid: a mid-week start with
    # one warmup bucket backs up to the previous Monday, and a mid-week end
    # floors to its own week's Monday.
    mid_week = MONDAY + 2 * DAY + HOUR
    assert (
        kline_primary_bucket_required_start(mid_week, 1, WEEK) == MONDAY - WEEK
    )
    assert kline_primary_bucket_required_end(mid_week, WEEK) == MONDAY

    feature = _feature(
        "prev_week_high",
        "rolling_extreme",
        {"window_bars": 1, "mode": "max"},
        interval="1w",
    )
    spec = tsk.make_spec(features=[feature])
    spec["market"]["timeframe"] = "1d"
    manifest = _manifest_with_primary(
        spec, requirement_interval="1w", warmup_bars=1
    )
    plan = compile_strategy(spec, manifest, capability_payload(REVISION))
    full_rows = _daily_week_rows(weeks=2)
    derived_rows = [
        {
            "ts": bucket["open_time"],
            "value": bucket["high"],
            "available_at": bucket["open_time"] + WEEK,
            "revision": "r1",
        }
        for bucket in ohlcv_resample(full_rows, DAY, "1w")
    ]
    frames = build_frames(
        {
            _primary_stream(manifest): full_rows[7:],  # week 1 weekdays
            "kline.primary.close.1w": derived_rows,
        },
        COVERAGE,
        plan,
    )
    # Week 1 Mon..Sat: the open week 1 bucket cannot be seen; week 0 is used.
    assert [frame.values["prev_week_high"] for frame in frames[:6]] == ["110"] * 6


def _atr_rows() -> list[tuple[int, int, int, int, int, int]]:
    return [
        (0 * HOUR, 100, 100, 99, 100, 1),
        (1 * HOUR, 100, 95.5, 95, 95.2, 1),  # gap down: TR_1 = 5
        (2 * HOUR, 95.2, 96, 94, 95, 1),  # TR_2 = 2
        (3 * HOUR, 95, 96.5, 95, 96, 1),  # TR_3 = 1.5
        (4 * HOUR, 96, 96.2, 95.8, 96, 1),  # TR_4 = 0.4
        (5 * HOUR, 96, 96.1, 95.9, 96, 1),  # TR_5 = 0.2
    ]


def test_atr_pct_filter() -> None:
    """period=3 Wilder ATR over a gap-down then calm sequence.

    TR uses c_{-1}: TR_1 = 5 (not 0.5). Seed at bar 3 = (5+2+1.5)/3, so
    ATR% = 2.83% (bar 3) and 2.11% (bar 4), then 1.47% (bar 5) after the
    0.2 TR. ATR% < 2% first becomes true at bar 5. Wrong c_{-1} flips bar 3
    below 2%; a 3-bar warmup would expose a value at bar 2.
    """
    rows = _primary_rows(_atr_rows())
    ordered = [row["open_time"] for row in rows]
    rows_by_ts = {row["open_time"]: row for row in rows}
    period = 3
    series = _atr_wilder_series(rows_by_ts, ordered, HOUR, period, "$.under_test")
    # 暖机：TR_1 needs c_0, first ATR needs period TRs -> bar index `period`.
    assert series[2 * HOUR][0] is None
    with localcontext(_DECIMAL_CONTEXT):
        seed = (Decimal(5) + Decimal(2) + Decimal("1.5")) / Decimal(period)
        atr3 = +seed
        atr4 = +((seed * Decimal(period - 1) + Decimal("0.4")) / Decimal(period))
        atr5 = +((atr4 * Decimal(period - 1) + Decimal("0.2")) / Decimal(period))
    assert series[3 * HOUR][0] == atr3
    assert series[4 * HOUR][0] == atr4
    assert series[5 * HOUR][0] == atr5

    features = [
        _feature("atr3", "atr_wilder", {"period": period}),
        _feature(
            "close_now",
            "rolling_extreme",
            {"window_bars": 1, "mode": "max"},
        ),
    ]
    spec = tsk.make_spec(
        condition={
            "node": "compare",
            "op": "lt",
            "left": {"node": "feature", "key": "atr3", "lag_bars": 0},
            "right": {"node": "feature", "key": "close_now", "lag_bars": 0},
        },
        features=features,
    )
    manifest = tkp._kline_primary_manifest(spec, requirement_interval="1h")
    primary_requirement = next(
        item
        for item in manifest["data_requirements"]
        if item["execution_role"] == "primary_execution_kline"
    )
    primary_requirement["warmup_bars"] = 4
    plan = compile_strategy(spec, manifest, capability_payload(REVISION))
    frames = build_frames({PRIMARY_STREAM: rows}, COVERAGE, plan)
    # atr / close percentage, evaluated only once ATR has warmed up.
    assert frames[2].values.get("atr3") is None
    assert float(frames[3].values["atr3"]) / 96 > 0.02
    assert float(frames[4].values["atr3"]) / 96 > 0.02
    assert float(frames[5].values["atr3"]) / 96 < 0.02

    # 暖机不足（3 根 < period+1=4）必须在 compile 层红。
    primary_requirement["warmup_bars"] = period
    with pytest.raises(StrategyContractError) as caught:
        compile_strategy(spec, manifest, capability_payload(REVISION))
    assert caught.value.code == ERR_SPEC_INVALID
    assert caught.value.required == period + 1


def test_rolling_std_filter() -> None:
    """``rolling_std`` = population stdev over ``window_bars`` (2..10000)."""
    ordered = [index * HOUR for index in range(4)]
    rows_by_ts = {
        ts: (Decimal(value), ts + HOUR, "r1")
        for ts, value in zip(ordered, ("1", "2", "3", "4"))
    }
    feature = {
        "key": "std3",
        "primitive": "rolling_std",
        "params": {"window_bars": 3},
        "interval": "1h",
    }
    outcome = _evaluate_windowed_primitive(
        feature, rows_by_ts, ordered, ordered[3], HOUR, "$.under_test"
    )
    assert outcome is not None
    with localcontext(_DECIMAL_CONTEXT):
        expected = +(Decimal(2) / Decimal(3)).sqrt()
    assert outcome[0] == expected
    # Filter atom: std(2,3,4)=0.816 > 0.8 true, > 0.9 false.
    assert outcome[0] > Decimal("0.8")
    assert not outcome[0] > Decimal("0.9")

    constant = {
        ts: (Decimal(5), ts + HOUR, "r1") for ts in ordered[:3]
    }
    flat = _evaluate_windowed_primitive(
        feature, constant, ordered[:3], ordered[2], HOUR, "$.under_test"
    )
    assert flat is not None and flat[0] == Decimal(0)

    for bad in (1, 10001):
        with pytest.raises(StrategyContractError) as caught:
            _validate_primitive_params(
                "rolling_std", {"window_bars": bad}, "$.under_test"
            )
        assert caught.value.code == ERR_SPEC_INVALID


def test_volume_field_rolling_mean() -> None:
    """``kline.primary.volume`` is a first-class field (flow value_kind) and a
    3-bar rolling mean over volumes 10/20/30/40/50 is 20/30/40."""
    volumes = ["10", "20", "30", "40", "50"]
    rows = _primary_rows(
        [(index * HOUR, 100, 101, 99, 100, int(volume)) for index, volume in enumerate(volumes)]
    )
    feature = _feature(
        "vol_ma3",
        "rolling_mean",
        {"window_bars": 3},
        source_stream="kline.primary.volume",
        value_kind="flow",
    )
    spec = tsk.make_spec(features=[feature])
    manifest = tkp._kline_primary_manifest(spec, requirement_interval="1h")
    next(
        item
        for item in manifest["data_requirements"]
        if item["execution_role"] == "primary_execution_kline"
    )["warmup_bars"] = 3
    plan = compile_strategy(spec, manifest, capability_payload(REVISION))
    frames = build_frames({PRIMARY_STREAM: rows}, COVERAGE, plan)
    assert "vol_ma3" not in frames[0].values
    assert [frame.values["vol_ma3"] for frame in frames[2:]] == ["20", "30", "40"]


def test_volume_resample_sums() -> None:
    """``ohlcv_resample`` sums volume within each bucket (1..8 -> 10, 26)."""
    rows = _primary_rows(
        [
            (index * HOUR, 100 + index, 101 + index, 99 + index, 100 + index, index + 1)
            for index in range(8)
        ]
    )
    buckets = ohlcv_resample(rows, HOUR, "4h")
    assert [bucket["open_time"] for bucket in buckets] == [0, 4 * HOUR]
    assert [bucket["volume"] for bucket in buckets] == ["10", "26"]
    assert buckets[0]["open"] == "100"
    assert buckets[0]["high"] == "104"
    assert buckets[0]["low"] == "99"
    assert buckets[0]["close"] == "103"


def test_capability_declares_min_max_abs() -> None:
    """min/max/abs are already evaluated by ``_ctx_op``; the declaration now
    advertises them (abs unary, min/max binary)."""
    capability = capability_payload(REVISION)
    _validate_capability(capability)
    by_op = {
        item["op"]: item
        for item in capability["operators"]
        if item["node"] == "arithmetic"
    }
    assert set(by_op) >= {"abs", "min", "max"}
    assert [sig["arg_types"] for sig in by_op["abs"]["signatures"]] == [
        ["decimal"],
        ["integer"],
    ]
    for op in ("min", "max"):
        assert [sig["arg_types"] for sig in by_op[op]["signatures"]] == [
            ["decimal", "decimal"],
            ["integer", "integer"],
        ]


def _feature_primitive(capability: dict, primitive: str) -> dict:
    return next(
        item
        for item in capability["feature_primitives"]
        if item["primitive"] == primitive
    )


def test_rolling_sum_does_not_declare_volume() -> None:
    """``rolling_sum`` must not advertise ``kline.primary.volume``.

    修前为什么红：J4-P1 让 ``rolling_sum`` 的 ``source_streams`` 带上了
    ``kline.primary.volume``，可 1h 直通路径上 ``window_bars > 1`` 仍被
    ``ERR_STRATEGY_SPEC_UNSUPPORTED`` 拒绝，等于声明了用不了的能力。量能确认
    只走 ``rolling_mean``，所以这里断言 volume 只剩 ``rolling_mean`` 声明。
    """
    capability = capability_payload(REVISION)
    _validate_capability(capability)
    assert "kline.primary.volume" not in _feature_primitive(
        capability, "rolling_sum"
    )["source_streams"]
    assert "kline.primary.volume" in _feature_primitive(
        capability, "rolling_mean"
    )["source_streams"]
    # 度量流不受影响：rolling_sum 仍覆盖 cvd 与 metrics。
    assert "coinglass.futures_cvd" in _feature_primitive(
        capability, "rolling_sum"
    )["source_streams"]


def test_rolling_sum_volume_spec_rejected_by_capability() -> None:
    """A ``rolling_sum`` over ``kline.primary.volume`` is rejected because the
    capability no longer declares it.

    修前为什么红：声明里带着 volume，``window_bars=1`` 的规格能一路编译通过，
    ``compile_strategy`` 不抛错；撤销声明后同一份规格被
    ``capability_requirements.features[0]`` 拒掉。``window_bars=5`` 则先被
    直通门禁拒绝——这正是撤销声明的理由。
    """
    feature = _feature(
        "vol_sum5",
        "rolling_sum",
        {"window_bars": 5},
        source_stream="kline.primary.volume",
        value_kind="flow",
    )
    spec = tsk.make_spec(features=[feature])
    manifest = tkp._kline_primary_manifest(spec, requirement_interval="1h")
    next(
        item
        for item in manifest["data_requirements"]
        if item["execution_role"] == "primary_execution_kline"
    )["warmup_bars"] = 5
    with pytest.raises(StrategyContractError) as caught:
        compile_strategy(spec, manifest, capability_payload(REVISION))
    assert caught.value.code == ERR_SPEC_UNSUPPORTED
    assert caught.value.path.endswith("features[0].params.window_bars")

    narrow = _feature(
        "vol_sum1",
        "rolling_sum",
        {"window_bars": 1},
        source_stream="kline.primary.volume",
        value_kind="flow",
    )
    narrow_spec = tsk.make_spec(features=[narrow])
    narrow_manifest = tkp._kline_primary_manifest(
        narrow_spec, requirement_interval="1h"
    )
    with pytest.raises(StrategyContractError) as caught:
        compile_strategy(
            narrow_spec, narrow_manifest, capability_payload(REVISION)
        )
    assert caught.value.code == ERR_SPEC_UNSUPPORTED
    assert caught.value.path.endswith("capability_requirements.features[0]")


# ------------------------------------------ R2: ``atr_wilder`` 只属于 v2

# ``atr_wilder`` 需要 high/low/close 三个字段一起算 TR，而 v3 的特征源是一维
# 标量序列（单 leg 字段或 derived 特征）。v3 的求值器没有 ATR 分支，
# ``_primitive_series_v3`` 会把它当滑动窗口原语读 ``params["window_bars"]``，
# 而 ATR 的参数是 ``{"period": N}`` —— 编译放行就等于换一个 KeyError 崩溃。


def _v3_atr_feature(period: int = 3, key: str = "atr3") -> dict:
    return {
        "key": key,
        "primitive": "atr_wilder",
        "primitive_version": "1",
        "source_stream": "kline.leg.a.close",
        "interval": "4h",
        "value_kind": "price",
        "output_type": "decimal",
        "params": {"period": period},
        "required": True,
    }


def _v3_spec_with(feature: dict) -> dict:
    """§2.7 ``sma_cross_basic`` spec plus one primitive feature.

    该原语直接进 entry 条件（大于 0.5），规格里不留悬空特征。
    """
    spec = tsb.example_spec()
    spec["features"] = [feature, *spec["features"]]
    spec["features"].sort(key=lambda item: item["key"].encode("utf-16-be"))
    spec["entry"]["condition"] = {
        "node": "compare",
        "op": "gt",
        "left": tsb._feature(feature["key"]),
        "right": tsb._dec("0.5"),
    }
    return spec


def test_atr_wilder_rejected_by_v3_compile() -> None:
    """``atr_wilder`` 不得留在 v3 原语白名单里。

    修前为什么红：``atr_wilder`` 挂在 ``_KNOWN_PRIMITIVES`` 上，下面这份 v3
    规格 ``compile_strategy_v3`` 直接放行，直到 ``build_frames_v3`` 才在
    ``_primitive_series_v3`` 里读 ``params["window_bars"]`` 抛未捕获的
    ``KeyError``。变异点：把 ``atr_wilder`` 放回 ``_KNOWN_PRIMITIVES``，本用例
    的编译断言立即红。
    """
    assert "atr_wilder" not in _KNOWN_PRIMITIVES
    assert "atr_wilder" in _KNOWN_PRIMITIVES_V2

    spec = _v3_spec_with(_v3_atr_feature())
    with pytest.raises(StrategySpecV3Error) as caught:
        compile_strategy_v3(spec)
    assert caught.value.code == ERR_SPEC_INVALID
    assert caught.value.path.endswith(".primitive")
    assert caught.value.detail()["actual"] == "atr_wilder"

    # 白名单放行的后果留证：v3 求值器对 ATR 只有 KeyError 一条路。
    with pytest.raises(KeyError) as key_error:
        _primitive_series_v3(
            {"key": "atr3", "primitive": "atr_wilder", "params": {"period": 3}},
            [(Decimal(1), None), (Decimal(2), None)],
        )
    assert key_error.value.args == ("window_bars",)


def test_atr_wilder_still_compiles_on_v2() -> None:
    """从 v3 白名单移出不能伤到 v2：ATR 仍能编译并算出值。

    v2 里 ATR 读 ``kline.primary`` 的 high/low/close，走
    ``_atr_wilder_series`` 预计算路径；这里断言编译通过且第 3 根起的 ATR 等于
    手算的 Wilder 种子值。
    """
    rows = _primary_rows(_atr_rows())
    period = 3
    features = [
        _feature("atr3", "atr_wilder", {"period": period}),
        _feature(
            "close_now",
            "rolling_extreme",
            {"window_bars": 1, "mode": "max"},
        ),
    ]
    spec = tsk.make_spec(
        condition={
            "node": "compare",
            "op": "lt",
            "left": {"node": "feature", "key": "atr3", "lag_bars": 0},
            "right": {"node": "feature", "key": "close_now", "lag_bars": 0},
        },
        features=features,
    )
    manifest = tkp._kline_primary_manifest(spec, requirement_interval="1h")
    next(
        item
        for item in manifest["data_requirements"]
        if item["execution_role"] == "primary_execution_kline"
    )["warmup_bars"] = 4
    plan = compile_strategy(spec, manifest, capability_payload(REVISION))
    frames = build_frames({PRIMARY_STREAM: rows}, COVERAGE, plan)
    with localcontext(_DECIMAL_CONTEXT):
        seed = +((Decimal(5) + Decimal(2) + Decimal("1.5")) / Decimal(period))
    # build_frames 的暖机帧（前 3 根）不出值，第 4 根即 source bar 3 起。
    atr_values = [frame.values.get("atr3") for frame in frames]
    assert atr_values[:3] == [None] * 3
    assert Decimal(atr_values[3]) == seed


def test_rolling_std_v3_evaluates_or_rejected() -> None:
    """``rolling_std`` 在 v3 是可用的，所以留在白名单里并断言数值。

    它和 ``ema``/``rolling_zscore`` 一样吃 ``{"window_bars": N}``，
    ``_primitive_series_v3`` 的滑动窗口分支能直接求值。修前若把 ``atr_wilder``
    移出而顺手误伤 ``rolling_std``，编译或求值会直接红。
    """
    assert "rolling_std" in _KNOWN_PRIMITIVES
    spec = _v3_spec_with(
        {
            "key": "ratio_std3",
            "primitive": "rolling_std",
            "primitive_version": "1",
            "source_stream": "feature:ratio",
            "interval": "4h",
            "value_kind": "level",
            "output_type": "decimal",
            "params": {"window_bars": 3},
            "required": True,
        }
    )
    plan = compile_strategy_v3(spec)
    closes = ["2", "4", "4", "4", "5", "5", "7", "9", "1"]
    frames = build_frames_v3(
        {"a": tsb._rows(closes), "b": tsb._rows(["1"] * len(closes))}, plan
    )
    values = [frame.values["ratio_std3"] for frame in frames.frames]
    # 3 根窗口前不出值。
    assert values[:2] == [None, None]
    window = [Decimal(close) for close in closes[:3]]
    with localcontext(_DECIMAL_CONTEXT) as ctx:
        count = Decimal(3)
        mean = ctx.divide(sum(window, Decimal(0)), count)
        variance = ctx.divide(
            sum(
                (
                    ctx.multiply(ctx.subtract(v, mean), ctx.subtract(v, mean))
                    for v in window
                ),
                Decimal(0),
            ),
            count,
        )
        expected = +ctx.sqrt(variance)
    assert Decimal(values[2]) == expected
    # 常数窗口给 0，而不是缺口。
    flat = build_frames_v3(
        {"a": tsb._rows(["5"] * 4), "b": tsb._rows(["1"] * 4)}, plan
    )
    assert flat.frames[-1].values["ratio_std3"] == "0"
