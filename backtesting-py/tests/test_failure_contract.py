"""2026-07-19 修复批回归钉子。

1. `_validation_failure` 的 failed 响应必须携带 assumptions/limitations（P0 必填
   object）——缺失时 connector ≥3.9.5 会把整个响应判为 PROVIDER_CONTRACT_VIOLATION,
   真实 error_message 被「Provider response field "assumptions" must be an object」
   吞掉（Pre 2026-07-19 实测）。
2. `is_strategy_execution_intent` 不得把 expected_provider_revision 当 artifact
   意图信号——legacy dispatch envelope 自 62-1 起始终携带它作完整性证据
   （connector 3.9.13 同款修复的 provider sibling）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cutie_backtesting_provider as provider  # noqa: E402
from strategy_execution import is_strategy_execution_intent  # noqa: E402


def _body_of(response) -> dict:
    return json.loads(bytes(response.body).decode("utf-8"))


def test_validation_failure_carries_required_failed_contract_objects():
    response = provider._validation_failure("INVALID_PARAMS", "symbol is required")
    body = _body_of(response)
    assert body["result_status"] == "failed"
    assert body["error_message"] == "symbol is required"
    assert isinstance(body["assumptions"], dict)
    assert isinstance(body["limitations"], dict)
    assert isinstance(body["raw_report"], dict)


def test_validation_failure_matches_business_failure_required_keys():
    validation = _body_of(provider._validation_failure("INVALID_PARAMS", "x"))
    business = _body_of(provider._business_failure("run-1", "ENGINE_ERROR", "y"))
    required = {"result_status", "error_type", "error_message", "assumptions", "limitations", "raw_report"}
    assert required <= set(validation.keys())
    assert required <= set(business.keys())


def test_intent_probe_ignores_legacy_integrity_fields():
    legacy_envelope = {
        "schema": "cutie.backtest_dispatch.v1",
        "run_id": "337108449168982016",
        "strategy": {"name": "legacy"},
        "symbol": "BTCUSDT",
        "dispatch_nonce": "nonce-1",
        "expected_provider_revision": "14851f528c7bed4ae92931adbbbe32100bf421d3",
        "result_callback": {"method": "POST"},
    }
    assert is_strategy_execution_intent(legacy_envelope) is False
    assert is_strategy_execution_intent({**legacy_envelope, "expected_provider_revision": None}) is False


def test_intent_probe_still_detects_bound_requests():
    assert is_strategy_execution_intent({"schema": "cutie.strategy_execution_request.v1"}) is True
    for key in ("artifact", "strategy_spec", "artifact_manifest", "expected_capability_hash", "result_contract"):
        assert is_strategy_execution_intent({key: {}}) is True
