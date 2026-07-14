"""canonical_json.v1 — Feature 62-1 跨语言规范 JSON 序列化（provider 侧唯一实现）。

Source of truth（本文件必须与其逐字节规则一致；上游修订规则必须同步本文件与
tests/fixtures/canonical_json/golden.json）：
  - SPEC: TokenBeep 主仓 docs/features/62_KOL策略市场/SPEC_验证复核契约.md §1.2
  - Python 参照实现（规则唯一权威）：TokenBeep 主仓 cutie-server/utils/canonical_json.py

Server(Python) 与 Connector/Provider(TypeScript/各实现语言) 各自只允许保留一个
canonical_json.v1 实现，共享同一组 golden fixtures 做字节级与 hash 一致性测试
（规则见 SPEC §1.2，RFC 8785/JCS 子集，本项目 payload 值域收紧后的形态）：

- 对象成员递归按 UTF-16 code-unit 顺序排列（不是 Unicode code point 顺序，
  BMP 外字符按 surrogate pair 参与排序）；
- 数组保持原始顺序；
- 输出 UTF-8，无任何多余空白；
- 金额/价格/比例必须在 payload 构造时就是无指数的规范 Decimal 字符串
  （见 canonical_decimal_str），整数时间戳/计数保持 JSON integer；
- float / Decimal 值直接拒绝（fail-closed）：出现即说明 payload 构造违约，
  跨语言浮点序列化差异会让 hash 永远对不上，宁可在源头炸掉；
- 禁止 NaN / Infinity / 负零。
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Any

CANONICAL_JSON_SCHEMA_VERSION = "canonical_json.v1"

# JCS（RFC 8785）字符串转义表：控制字符里有短形式的用短形式，其余 \u00xx 小写。
_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


class CanonicalJsonError(ValueError):
    """payload 含 canonical_json.v1 不允许的值（float/Decimal/NaN/非法 key 等）。"""


def _escape_string(value: str) -> str:
    out: list[str] = ['"']
    for ch in value:
        mapped = _ESCAPES.get(ch)
        if mapped is not None:
            out.append(mapped)
        elif ch < "\x20":
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _utf16_sort_key(key: str) -> bytes:
    # UTF-16BE 字节序 == UTF-16 code unit 序（JCS 排序要求）
    return key.encode("utf-16-be")


def _serialize(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, str):
        out.append(_escape_string(value))
    elif isinstance(value, int):
        # bool 已在上方分支排除（Python bool 是 int 子类）
        # review C-L7：超出 JS 安全整数范围的 int 在 TS 实现被拒绝（JS number 无法
        # 精确表示），Python 原样输出 integer 会产生跨语言不一致的 hash——同样
        # fail-closed，要求先经 normalize_numbers_for_hash 转十进制字符串。与
        # cutie-server/utils/canonical_json.py 同步修（跨仓 canonical_json.v1 唯一权威）。
        if abs(value) > _JS_MAX_SAFE_INTEGER:
            raise CanonicalJsonError(
                "canonical_json.v1 forbids integers outside the JS safe range (|v| > 2^53-1); "
                "convert to a canonical decimal string first (normalize_numbers_for_hash)"
            )
        out.append(str(value))
    elif isinstance(value, (float, Decimal)):
        raise CanonicalJsonError(
            "canonical_json.v1 forbids float/Decimal values; "
            "convert amounts to canonical decimal strings and timestamps to int first"
        )
    elif isinstance(value, dict):
        for key in value.keys():
            if not isinstance(key, str):
                raise CanonicalJsonError(f"object key must be str, got {type(key).__name__}")
        out.append("{")
        first = True
        for key in sorted(value.keys(), key=_utf16_sort_key):
            if not first:
                out.append(",")
            first = False
            out.append(_escape_string(key))
            out.append(":")
            _serialize(value[key], out)
        out.append("}")
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for index, item in enumerate(value):
            if index:
                out.append(",")
            _serialize(item, out)
        out.append("]")
    else:
        raise CanonicalJsonError(f"unsupported type for canonical_json.v1: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """把 payload 序列化为 canonical_json.v1 字符串。"""
    out: list[str] = []
    _serialize(value, out)
    return "".join(out)


def canonical_json_sha256(value: Any) -> str:
    """sha256(canonical_json(value)) 的 hex digest——本功能所有权威 hash 的唯一算法。"""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


# JS Number 安全整数范围：|int| 超出后 JSON.parse 会丢精度，整数值也必须走字符串
_JS_MAX_SAFE_INTEGER = 2**53 - 1


def normalize_numbers_for_hash(value: Any) -> Any:
    """execution_params.v1 数值归一（跨语言，Connector 侧同规则；SPEC §0 追加冻结）：

    - 数值为整数值（无论 int/float 类型；JSON "5.0" 在 Python 是 float、在 JS 是
      integer）且 |值| ≤ 2^53-1 → JSON integer；超出 JS 安全整数范围 → 十进制字符串
      （JS 侧 JSON.parse 会丢精度，必须走字符串）；
    - 非整数值 → **无指数定点十进制字符串**（canonical_decimal_str）。禁用最短往返
      repr/toString：Python `repr(1e-07)='1e-07'` 与 JS `'1e-7'`、Python
      `repr(1e-06)='1e-06'` 与 JS `'0.000001'` 的指数/定点切换阈值不同，会分叉；
      定点无指数形态两侧可确定性复现；
    - NaN/Infinity 拒绝；容器递归；其余类型原样。

    归一后的对象才允许进 canonical_json（原始 float 会被 fail-closed 拒绝）。
    """
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        if abs(value) > _JS_MAX_SAFE_INTEGER:
            return canonical_decimal_str(value)
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise CanonicalJsonError("NaN/Infinity are forbidden in canonical payloads")
        if value.is_integer():
            int_value = int(value)
            if abs(int_value) > _JS_MAX_SAFE_INTEGER:
                return canonical_decimal_str(int_value)
            return int_value
        # str(float) 是最短往返表示（值精确），Decimal 化后输出定点无指数
        return canonical_decimal_str(str(value))
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise CanonicalJsonError("NaN/Infinity are forbidden in canonical payloads")
        if value == value.to_integral_value():
            int_value = int(value)
            if abs(int_value) > _JS_MAX_SAFE_INTEGER:
                return canonical_decimal_str(int_value)
            return int_value
        return canonical_decimal_str(value)
    if isinstance(value, dict):
        return {k: normalize_numbers_for_hash(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize_numbers_for_hash(item) for item in value]
    return value


def canonical_decimal_str(value: Decimal | int | str) -> str:
    """把数值转成 SPEC §1.2 规范 Decimal 字符串：无指数、无多余尾零、无负零。

    int / 数字字符串也接受（统一入口），NaN/Infinity 拒绝。
    """
    if isinstance(value, bool):
        raise CanonicalJsonError("bool is not a decimal value")
    try:
        dec = value if isinstance(value, Decimal) else Decimal(str(value))
    except ArithmeticError as exc:
        raise CanonicalJsonError(f"not a decimal value: {value!r}") from exc
    if not dec.is_finite():
        raise CanonicalJsonError("NaN/Infinity are forbidden in canonical payloads")
    if dec == 0:
        return "0"
    normalized = dec.normalize()
    # format 'f' 禁科学计数法：Decimal('1E+2') → '100'，Decimal('1.500') → '1.5'
    return format(normalized, "f")
