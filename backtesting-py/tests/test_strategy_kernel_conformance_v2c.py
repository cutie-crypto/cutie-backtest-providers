"""v2c conformance golden (JES21-B3b 二期 K1): ``ema`` / ``macd`` /
``rolling_zscore`` in StrategySpec v2.

The fixture is the single cross-repo source the server (S1) replays; its
expected values come from ``_v2c_reference`` (no kernel import) and are
cross-checked against pandas here, independently of the kernel tests.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import _v2c_reference as ref  # noqa: E402
from canonical_json import canonical_json_sha256  # noqa: E402

FIXTURE = json.loads(ref.FIXTURE.read_text(encoding="utf-8"))
CASES = {case["name"]: case for case in FIXTURE["cases"]}


def test_fixture_schema_and_sha256_cover_the_cases() -> None:
    assert FIXTURE["schema"] == "cutie.strategy_kernel_conformance.v2c"
    assert set(FIXTURE) == {"schema", "source_of_truth", "note", "sha256", "cases"}
    assert FIXTURE["sha256"] == canonical_json_sha256(FIXTURE["cases"])
    assert {case["primitive"] for case in FIXTURE["cases"]} == {
        "ema",
        "macd",
        "rolling_zscore",
    }


def test_fixture_is_exactly_what_the_reference_builds() -> None:
    assert FIXTURE == ref.build_fixture()


@pytest.mark.parametrize("name", sorted(CASES))
def test_golden_matches_pandas_float_reference(name: str) -> None:
    case = CASES[name]
    expected = [row["value"] for row in case["expected"]]
    floats = ref.pandas_reference(case)
    assert [value is None for value in expected] == [value is None for value in floats]
    for golden, approx in zip(expected, floats):
        if golden is not None:
            assert float(golden) == pytest.approx(approx, rel=1e-12, abs=1e-12)


def test_boundary_coverage_is_present() -> None:
    """warmup 不足 / 缺口 / 常数序列 (stdev 0) each have a case per primitive."""
    for primitive in ("ema", "macd", "rolling_zscore"):
        names = [n for n, c in CASES.items() if c["primitive"] == primitive]
        assert any("gap" in n for n in names), primitive
        assert any("constant" in n for n in names), primitive
    for name in ("ema_warmup_never_reached", "macd_signal_warmup_not_reached"):
        assert all(row["value"] is None for row in CASES[name]["expected"])
