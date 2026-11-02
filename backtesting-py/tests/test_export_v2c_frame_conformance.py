"""K0 (四期 B3B-P4-K0): v2c 逐帧金标。

``tests/fixtures/strategy_kernel_conformance_v2c_frames.json`` 由
``scripts/export_v2c_frame_conformance.py`` 从服务端条件矩阵夹具导出：每条规格
跑 provider 自己的 ``compile_strategy`` + ``build_frames`` + 条件循环，逐帧记
``{frame_close_at, entry_condition, exit_condition, feature_values}``（列式存储，
四个字段的每一列按帧下标对齐）。

本文件钉三件事：

1. 金标字节可从夹具内嵌的 ``provider_params`` + 合成行情完整重算（重算路径同样
   逐帧用 ``build_frames`` 交叉验证特征值），所以金标不能手改；
2. 每帧特征值是 ``cutie.decimal128.v1`` 规范字符串，与 provider 自身结果逐位相等；
3. 变异靶：把 metric 的 as-of 锚点换成「收盘时最新一行」，交叉验证必须报红
   （``FrameConformanceError``），而不是悄悄导出错的金标。

矩阵规格全部是 ``signal_exit == null`` + ``time_exit_bars``，所以
``exit_condition`` 逐帧为 ``false``；它钉的是「无信号出场」这一条，不是出场语义全集。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

BACKTESTING_PY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKTESTING_PY))

from canonical_json import canonical_decimal_str, canonical_json_sha256  # noqa: E402

SCRIPT_PATH = BACKTESTING_PY / "scripts" / "export_v2c_frame_conformance.py"
FIXTURE_PATH = (
    BACKTESTING_PY
    / "tests"
    / "fixtures"
    / "strategy_kernel_conformance_v2c_frames.json"
)


def _load_exporter():
    spec = importlib.util.spec_from_file_location(
        "export_v2c_frame_conformance", SCRIPT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


exporter = _load_exporter()
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
CASES = {case["case_id"]: case for case in FIXTURE["cases"]}
FRAME_FIELDS = {"frame_close_at", "entry_condition", "exit_condition", "feature_values"}
SINGLE_ATOMS = {
    "ahr999",
    "altcoin_season",
    "bollinger_touch",
    "bubble_index",
    "change_24h",
    "ema_cross",
    "fear_greed",
    "funding_rate",
    "golden_ratio",
    "liquidation_usd",
    "long_short_ratio",
    "lth_sth_mvrv",
    "ma_200w",
    "macd_cross",
    "nupl",
    "open_interest_change",
    "pi_cycle",
    "puell",
    "rainbow_band",
    "rsi",
    "stablecoin_mcap_change",
}


def test_fixture_schema_and_sha256() -> None:
    assert FIXTURE["schema"] == "cutie.strategy_kernel_conformance.v2c_frames"
    assert FIXTURE["fixture_version"] == "1"
    assert set(FIXTURE) == {
        "schema",
        "fixture_version",
        "source_of_truth",
        "note",
        "matrix",
        "generator",
        "market_data",
        "cases",
        "sha256",
    }
    payload = {"market_data": FIXTURE["market_data"], "cases": FIXTURE["cases"]}
    assert FIXTURE["sha256"] == canonical_json_sha256(payload)
    assert FIXTURE["source_of_truth"].startswith(
        "backtesting-py/scripts/export_v2c_frame_conformance.py"
    )


def test_matrix_metadata_matches_the_cases_and_market_data() -> None:
    meta = FIXTURE["matrix"]
    assert meta["case_count"] == len(FIXTURE["cases"]) == 462
    assert meta["symbol"] == FIXTURE["market_data"]["symbol"] == "BTCUSDT"
    assert meta["timeframe"] == FIXTURE["market_data"]["timeframe"] == "1h"
    assert meta["start_at"] == FIXTURE["market_data"]["start_at"]
    assert FIXTURE["market_data"]["end_at"] == (
        meta["start_at"]
        + FIXTURE["generator"]["decision_frames"]
        * FIXTURE["market_data"]["primary_step_seconds"]
    )
    # 462 = 21 原子 × 2 方向 + 两两组合 all/any。
    case_ids = [case["case_id"] for case in FIXTURE["cases"]]
    assert case_ids == sorted(case_ids)
    assert len(set(case_ids)) == len(case_ids)
    assert sum(1 for case_id in case_ids if case_id.startswith("single:")) == 42
    assert {
        case["case_id"].split(":")[1]
        for case in CASES.values()
        if case["case_id"].startswith("single:")
    } == SINGLE_ATOMS


def test_fixture_is_exactly_what_the_exporter_rebuilds() -> None:
    """夹具里每条规格都重新跑一遍 provider 内核，逐帧值必须与提交字节一致。"""
    cases = FIXTURE["cases"]
    market = exporter.build_market_data(cases, FIXTURE["matrix"])
    assert market == FIXTURE["market_data"], "合成行情生成器漂移"
    rebuilt = exporter.build_fixture(cases, market, FIXTURE["matrix"])
    assert rebuilt == FIXTURE


def test_frames_are_columnar_aligned_and_pin_the_decision_window() -> None:
    step = FIXTURE["market_data"]["primary_step_seconds"]
    start_at = FIXTURE["market_data"]["start_at"]
    decision_frames = FIXTURE["generator"]["decision_frames"]
    for case in FIXTURE["cases"]:
        frames = case["frames"]
        assert set(frames) == FRAME_FIELDS
        lengths = {
            len(frames["frame_close_at"]),
            len(frames["entry_condition"]),
            len(frames["exit_condition"]),
            *(len(values) for values in frames["feature_values"].values()),
        }
        assert lengths == {decision_frames}
        assert frames["frame_close_at"][0] == start_at + step
        assert frames["frame_close_at"][-1] == start_at + decision_frames * step
        assert all(
            later - earlier == step
            for earlier, later in zip(
                frames["frame_close_at"], frames["frame_close_at"][1:]
            )
        )
        # 金标的条件真值是逐帧原始条件；矩阵规格无 signal_exit。
        assert all(isinstance(flag, bool) for flag in frames["entry_condition"])
        assert all(flag is False for flag in frames["exit_condition"])
        spec = case["provider_params"]["strategy_spec"]
        assert set(frames["feature_values"]) == {
            feature["key"] for feature in spec["features"]
        }
        assert spec["exit"]["signal_exit"] is None


def test_spec_hash_pins_the_embedded_provider_params() -> None:
    for case in FIXTURE["cases"]:
        spec = case["provider_params"]["strategy_spec"]
        assert case["spec_hash"] == canonical_json_sha256(spec)
        assert set(case["provider_params"]) == {"strategy_spec", "data_requirements"}


def test_feature_values_are_canonical_decimal_strings() -> None:
    # build_frames canonicalizes under cutie.decimal128.v1 (34 significant
    # digits), so re-canonicalizing must happen in that context, not the
    # interpreter default (prec=28).
    with localcontext(exporter._DECIMAL_CONTEXT):
        for case in FIXTURE["cases"]:
            for key, values in case["frames"]["feature_values"].items():
                for value in values:
                    assert isinstance(value, str), (case["case_id"], key)
                    assert canonical_decimal_str(Decimal(value)) == value, (
                        case["case_id"],
                        key,
                        value,
                    )


def test_every_atom_gets_both_entry_branches() -> None:
    """21 个可编译原子的单项 long/short 合并后，entry 真/假都出现过。"""
    branches: dict[str, set] = {}
    for case in FIXTURE["cases"]:
        if not case["case_id"].startswith("single:"):
            continue
        atom = case["case_id"].split(":")[1]
        branches.setdefault(atom, set()).update(case["frames"]["entry_condition"])
    assert set(branches) == SINGLE_ATOMS
    assert {
        atom: flags for atom, flags in branches.items() if flags != {True, False}
    } == {}


def _latest_row_anchor(feature, frame, timeframe):
    """变异：metric 锚点取「收盘时最新一行」，而不是 §3.4 的上一已收盘点。"""
    step = exporter._interval_seconds(feature["interval"])
    if exporter.metrics_stream_parts(feature["source_stream"]) is not None:
        return (frame.bar_close_at // step) * step
    return exporter._anchor_ts(feature, frame, timeframe)


@pytest.mark.parametrize(
    "case_id",
    [
        "single:fear_greed:long",  # 1d metrics as-of
        "single:funding_rate:long",  # 1h stitched metrics as-of
        "single:liquidation_usd:long",  # 1h stitched metrics as-of
    ],
)
def test_anchor_mutation_to_latest_row_breaks_build_frames_crosscheck(
    case_id: str, monkeypatch
) -> None:
    case = CASES[case_id]
    market = FIXTURE["market_data"]
    meta = FIXTURE["matrix"]

    # 未变异时可以正常导出，且锚点确实不等于「最新一行」。
    exporter.build_case_frames(case, market, meta)
    feature = case["provider_params"]["strategy_spec"]["features"][0]
    frame = type(
        "Frame",
        (),
        {
            "bar_open_at": market["start_at"],
            "bar_close_at": market["start_at"] + 24 * 3600,
        },
    )()
    assert exporter._anchor_ts(feature, frame, meta["timeframe"]) != (
        _latest_row_anchor(feature, frame, meta["timeframe"])
    )

    monkeypatch.setattr(exporter, "_anchor_ts", _latest_row_anchor)
    with pytest.raises(exporter.FrameConformanceError, match="build_frames"):
        exporter.build_case_frames(case, market, meta)


def test_seeded_jitter_is_deterministic_and_bounded() -> None:
    jitter = exporter.PRICE_JITTER
    assert jitter == exporter._lcg_jitter()
    assert len(jitter) == exporter.PRICE_JITTER_COUNT > 1
    assert set(jitter) <= {-1, 0, 1}
    # 同一输入必然同一收盘价（不含任何时钟/随机依赖）。
    assert exporter._close_at(1767225600, 1767225600) == exporter._close_at(
        1767225600, 1767225600
    )
