"""canonical_json.v1 契约测试（Feature 62-1，SPEC §1.2，provider 侧唯一实现）。

golden fixtures 是跨语言/跨仓锚点：本文件与 TokenBeep 主仓
cutie-server/tests/fixtures/canonical_json/golden.json 逐字节一致（见本目录
fixtures/canonical_json/golden.json 的 source_of_truth 字段）；修改 fixtures 等于
修改跨语言契约，必须同步主仓与 Connector 侧并在 SPEC 记录。
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from canonical_json import (  # noqa: E402
    CanonicalJsonError,
    canonical_decimal_str,
    canonical_json,
    canonical_json_sha256,
)

GOLDEN_PATH = Path(__file__).parent / "fixtures" / "canonical_json" / "golden.json"


def _golden_cases() -> list[dict]:
    payload = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert payload["schema"] == "canonical_json.v1"
    return payload["cases"]


@pytest.mark.parametrize("case", _golden_cases(), ids=lambda c: c["name"])
def test_golden_fixture_canonical_and_hash(case: dict):
    assert canonical_json(case["input"]) == case["canonical"]
    assert canonical_json_sha256(case["input"]) == case["sha256"]


def test_utf16_code_unit_sort_beats_code_point_sort():
    """U+10000（UTF-16 首 unit D800）必须排在 U+FF01 前——code point 排序在这里相反。"""
    doc = {"！": 1, "\U00010000": 2}
    canonical = canonical_json(doc)
    assert canonical.index("\U00010000") < canonical.index("！")
    # 明确断言与 Python 默认 code-point sort_keys 不同（防止实现被"简化"回 json.dumps）
    code_point_sorted = json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    assert canonical != code_point_sorted


@pytest.mark.parametrize("bad", [1.5, Decimal("1.5"), float("nan"), object()])
def test_float_decimal_and_unknown_types_rejected(bad):
    with pytest.raises(CanonicalJsonError):
        canonical_json({"x": bad})


def test_non_string_key_rejected():
    with pytest.raises(CanonicalJsonError):
        canonical_json({1: "a"})


def test_bool_serialized_as_json_literal_not_int():
    assert canonical_json({"t": True, "f": False}) == '{"f":false,"t":true}'


def test_int_at_js_safe_integer_boundary_allowed():
    """review C-L7：|v| <= 2^53-1 仍原样输出 JSON integer（未超出 JS 安全整数范围）。"""
    assert canonical_json({"v": 2**53 - 1}) == '{"v":9007199254740991}'
    assert canonical_json({"v": -(2**53 - 1)}) == '{"v":-9007199254740991}'


def test_int_beyond_js_safe_integer_rejected():
    """review C-L7 跨语言一致性：超出 JS 安全整数范围的 int fail-closed 拒绝，
    不允许原样输出 integer——TS 实现会拒绝同样的值（JS number 表示不了），Python
    若放行会产生跨语言不一致的 canonical 串/hash。调用方需先经
    normalize_numbers_for_hash 转十进制字符串。
    """
    with pytest.raises(CanonicalJsonError):
        canonical_json({"v": 2**53})
    with pytest.raises(CanonicalJsonError):
        canonical_json({"v": -(2**53)})
    with pytest.raises(CanonicalJsonError):
        canonical_json({"v": 2**63})


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1.500", "1.5"),
        ("100", "100"),
        ("0", "0"),
        ("-0", "0"),
        ("1E+2", "100"),
        ("0.0381815", "0.0381815"),
        ("-2.50", "-2.5"),
        (Decimal("10381.815"), "10381.815"),
        (42, "42"),
    ],
)
def test_canonical_decimal_str(raw, expected):
    assert canonical_decimal_str(raw) == expected


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "abc", True])
def test_canonical_decimal_str_rejects_non_finite_and_garbage(bad):
    with pytest.raises(CanonicalJsonError):
        canonical_decimal_str(bad)


def test_control_char_escapes_lowercase_hex():
    assert canonical_json({"c": "\x01\x1f"}) == '{"c":"\\u0001\\u001f"}'
    assert canonical_json({"s": 'a"b\\c\nd\te'}) == '{"s":"a\\"b\\\\c\\nd\\te"}'
