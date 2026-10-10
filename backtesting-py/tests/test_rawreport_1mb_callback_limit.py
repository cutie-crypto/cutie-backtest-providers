"""raw_report 回调上限提到 1MB（与服务端 raw_report_json 回调字段上限同步），其它字段上限不变。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cutie_backtesting_provider as provider  # noqa: E402

RAW_REPORT_LIMIT = 1048576
OTHER_LIMITS = {"metrics": 262144, "equity_curve": 262144, "trades": 262144,
                "assumptions": 262144, "limitations": 262144, "data_manifest": 8192}


def _body_of(response) -> dict:
    return json.loads(bytes(response.body).decode("utf-8"))


def _json_string_of_bytes(size: int) -> str:
    # 紧凑 JSON 下字符串两侧引号各占 1 字节。
    return "x" * (size - 2)


def test_raw_report_300kb_passes_unchanged():
    body = {"result_status": "success", "raw_report": {"payload": "x" * 300 * 1024}}
    assert _body_of(provider._bounded_template_response("raw300k", body)) == body


def test_raw_report_exactly_1mb_passes():
    body = {"raw_report": _json_string_of_bytes(RAW_REPORT_LIMIT)}
    assert _body_of(provider._bounded_template_response("raw1m", body)) == body


def test_raw_report_over_1mb_rejected_with_limit_in_message():
    body = {"result_status": "success", "raw_report": _json_string_of_bytes(RAW_REPORT_LIMIT + 1)}
    failed = _body_of(provider._bounded_template_response("raw1m1", body))
    assert failed["result_status"] == "failed"
    assert failed["error_type"] == "INVALID_PARAMS"
    assert "raw_report" in failed["error_message"]
    assert "1048576" in failed["error_message"]


@pytest.mark.parametrize("field,limit", sorted(OTHER_LIMITS.items()))
def test_other_field_limits_unchanged(field, limit):
    at_limit = {field: _json_string_of_bytes(limit)}
    assert _body_of(provider._bounded_template_response("at", at_limit)) == at_limit
    failed = _body_of(provider._bounded_template_response("over", {field: _json_string_of_bytes(limit + 1)}))
    assert failed["result_status"] == "failed"
    assert failed["error_type"] == "INVALID_PARAMS"
    assert field in failed["error_message"] and str(limit) in failed["error_message"]
