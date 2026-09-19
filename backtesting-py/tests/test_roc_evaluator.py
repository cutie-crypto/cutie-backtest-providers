"""P2b（0919 夜间无人值守追加项）：strategy_entry_evaluators 里的 ROC 判定器。

统领采纳的追加项：provider 仓库自己这份 `strategy_entry_evaluators.py`（Feature 50
三期 3b 手写移植，供 `strategy_intent_replay.py`/`strategy_sl_tp_kernel.py`/
`strategy_signal_report.py` 消费）此前只覆盖 7 个模板，ROC 模板落地后必须补齐注册，
否则 `is_supported_strategy_type("roc")` 一直 False，布防判定半支持。

用 `tests/fixtures/roc_golden.json`（P2 主任务的 golden，与 `_build_roc` 同源公式）
对账：判定器在这份 40 根 fixture 上走一遍逐 bar 状态机，得到的入场/出场 bar 索引必须
分别等于 fixture 的 `expected_entries`/`expected_exits`。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strategy_entry_evaluators import (
    Bar,
    evaluate_entry,
    evaluate_exit,
    is_supported_strategy_type,
    required_warmup_bars,
    roc_settings,
)

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "roc_golden.json"


def _load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text())


def _bars(closes: list[float]) -> list[Bar]:
    return [Bar(i * 3600, (i + 1) * 3600, c, c, c, c) for i, c in enumerate(closes)]


def _walk(params: dict, bars: list[Bar]) -> tuple[list[int], list[int]]:
    """逐 bar 状态机：从 required_warmup_bars 开始，仿照 test_phase5_evaluators.py 的
    「evaluate_entry/evaluate_exit 只用已收盘 bars 前缀」用法，只是这里直接对齐 fixture
    的信号 bar 序号（fixture 的 expected_entries/exits 就是判定器自己认定的信号 bar，
    不像 provider 回测引擎那样因 trade_on_close=False 而延后一根执行）。"""
    warmup = required_warmup_bars("roc", params)
    held = False
    entries, exits = [], []
    for i in range(warmup - 1, len(bars)):
        slice_bars = bars[: i + 1]
        if not held:
            if evaluate_entry("roc", params, slice_bars, "long") is not None:
                entries.append(i)
                held = True
        else:
            if evaluate_exit("roc", params, slice_bars, "long") is not None:
                exits.append(i)
                held = False
    return entries, exits


def test_roc_is_registered_and_supported():
    assert is_supported_strategy_type("roc") is True


def test_roc_evaluator_matches_golden_fixture_entries_and_exits():
    fixture = _load_fixture()
    params = dict(
        roc_period=fixture["roc_period"],
        entry_threshold=fixture["entry_threshold"],
        exit_threshold=fixture["exit_threshold"],
    )
    bars = _bars(fixture["closes"])
    entries, exits = _walk(params, bars)
    assert entries == fixture["expected_entries"]
    assert exits == fixture["expected_exits"]


def test_roc_evaluator_short_direction_never_fires():
    fixture = _load_fixture()
    params = dict(
        roc_period=fixture["roc_period"],
        entry_threshold=fixture["entry_threshold"],
        exit_threshold=fixture["exit_threshold"],
    )
    bars = _bars(fixture["closes"])
    for i in range(fixture["roc_period"], len(bars)):
        slice_bars = bars[: i + 1]
        assert evaluate_entry("roc", params, slice_bars, "short") is None
        assert evaluate_exit("roc", params, slice_bars, "short") is None


def test_required_warmup_bars_matches_provider_min_bars():
    assert required_warmup_bars("roc", dict(roc_period=12)) == 13
    assert required_warmup_bars("roc", dict(roc_period=30)) == 31
    # 非法参数（周期 < 2）解析不出 settings -> None，不是崩溃。
    assert required_warmup_bars("roc", dict(roc_period=1)) is None


def test_below_min_bars_produces_no_signal():
    """不足 min_bars（roc_period + 1）时判定器必须返回 None，不产出信号。"""
    fixture = _load_fixture()
    roc_period = fixture["roc_period"]
    params = dict(roc_period=roc_period, entry_threshold=fixture["entry_threshold"], exit_threshold=fixture["exit_threshold"])
    # 只喂 roc_period 根（= min_bars - 1），无论涨跌都不该有信号。
    short_bars = _bars(fixture["closes"][:roc_period])
    assert len(short_bars) == roc_period
    assert evaluate_entry("roc", params, short_bars, "long") is None
    assert evaluate_exit("roc", params, short_bars, "long") is None


def test_roc_settings_rejects_exit_greater_than_entry():
    assert roc_settings(dict(entry_threshold=5, exit_threshold=10)) is None
    assert roc_settings(dict(entry_threshold=5, exit_threshold=5)) == (12, 5, 5)
    assert roc_settings(dict(roc_period=1)) is None
