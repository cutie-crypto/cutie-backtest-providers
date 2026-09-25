"""123 组合策略 B2: provider `/cutie/backtest` HTTP wiring for the three basket
combo tools (SPEC_组合策略v3契约 §6.1/§6.2/§2.6.1/§3/§4).

Reuses the shared golden fixture (`tests/fixtures/strategy_kernel_conformance_v3.json`,
byte-identical to the TokenBeep main checkout copy per §8) so the HTTP-level
assertions pin against the exact same ``expected_result``/``expected_spec_hash``
the direct-kernel conformance tests (``test_strategy_kernel_conformance_v3.py``)
already check -- this file only proves the dispatch/fetch/response wiring
around that already-vetted core, not the kernel semantics themselves.
"""

from __future__ import annotations

import copy
import decimal
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

import cutie_backtesting_provider as provider  # noqa: E402
from canonical_json import canonical_json_sha256  # noqa: E402

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "strategy_kernel_conformance_v3.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
CASES = {case["case_id"]: case for case in FIXTURE["cases"]}

_FAMILY_TOOL_ID = {
    "basket_ratio_sma_cross": "local.backtesting_py.basket_ratio_sma_cross",
    "basket_ratio_roc": "local.backtesting_py.basket_ratio_roc",
    "basket_ratio_zscore": "local.backtesting_py.basket_ratio_zscore",
}


def _family_of(case: dict) -> str:
    """§8: strategy_family is inferred from the provider_params' own key set
    (the three families' extra keys never overlap)."""
    keys = set(case["provider_params"])
    if {"fast_window", "slow_window"} <= keys:
        return "basket_ratio_sma_cross"
    if {"roc_window", "entry_threshold", "exit_threshold"} <= keys:
        return "basket_ratio_roc"
    return "basket_ratio_zscore"


def _basket_request(case: dict, *, run_id: str = "basket-1", tool_id: str | None = None) -> dict:
    envelope = case["envelope"]
    legs = case["provider_params"]["legs"]
    tool_id = tool_id or _FAMILY_TOOL_ID[_family_of(case)]
    return {
        "backtest": {
            "run_id": run_id,
            "provider_tool_id": tool_id,
            "provider_params": case["provider_params"],
            "symbol": "~".join(leg["symbol"] for leg in legs),
            "market": "futures",
            "timeframe": envelope["timeframe"],
            "start_at": envelope["start_at"],
            "end_at": envelope["end_at"],
            "initial_capital": case["initial_capital"],
            "fee_bps": envelope["fee_bps"],
            "slippage_bps": envelope["slippage_bps"],
            "instrument_rules": case["instrument_rules"],
        }
    }


def _install_leg_fetch(monkeypatch, case: dict, *, central_used: bool = True):
    """Monkeypatch the shared central-then-ccxt raw fetch (``_fetch_ohlcv_raw``)
    with the fixture's per-leg klines, keyed by symbol; records every call so
    tests can assert the requested ``start_sec`` was pushed back for warmup."""
    legs = case["provider_params"]["legs"]
    klines_by_symbol = {leg["symbol"]: case["klines"][leg["leg_id"]] for leg in legs}
    calls: list[tuple] = []

    def fetch(exchange_id, market, symbol, timeframe, start_sec, end_sec):
        calls.append((exchange_id, market, symbol, timeframe, start_sec, end_sec))
        rows = klines_by_symbol[symbol]
        ohlcv = [
            [row["open_time"] * 1000, row["open"], row["high"], row["low"], row["close"], row["volume"]]
            for row in rows
        ]
        return ohlcv, "cutie_central_market_data", central_used, False

    monkeypatch.setattr(provider, "_fetch_ohlcv_raw", fetch)
    return calls


def _post(request: dict):
    return TestClient(provider.app).post("/cutie/backtest", json=request).json()


# ---------------------------------------------------------------------------
# 三 tool 各一次 HTTP 成功路径：与 fixture expected_result 逐字节一致
# ---------------------------------------------------------------------------


def _assert_matches_fixture(monkeypatch, case_id: str):
    case = CASES[case_id]
    _install_leg_fetch(monkeypatch, case)
    body = _post(_basket_request(case, run_id=case_id))
    assert body["result_status"] == "success", body
    assert body["schema_version"] == "cutie.backtest_result.v3"
    expected = case["expected_result"]
    assert body["metrics"] == expected["metrics"]
    assert body["trades"] == expected["trades"]
    assert body["equity_curve"] == expected["equity_curve"]
    assert body["data_manifests"] == expected["data_manifests"]
    # data_manifest（v2 单数）在组合 run 绝不出现（SPEC §4）。
    assert "data_manifest" not in body
    assert body["strategy_spec_hash"] == case["expected_spec_hash"]
    assert canonical_json_sha256(json.loads(body["strategy_spec_json"])) == case["expected_spec_hash"]
    assert body["data_manifests_hash"] == provider.data_manifests_hash(expected["data_manifests"])
    return body


def test_basket_sma_cross_http_matches_fixture(monkeypatch):
    _assert_matches_fixture(monkeypatch, "sma_cross_basic")


def test_basket_roc_http_matches_fixture(monkeypatch):
    _assert_matches_fixture(monkeypatch, "roc_basic")


def test_basket_zscore_http_matches_fixture(monkeypatch):
    _assert_matches_fixture(monkeypatch, "zscore_basic")


# ---------------------------------------------------------------------------
# 预热区间正确：取数请求的 start 按编译后 plan 最大回看前推（§2.6.1）
# ---------------------------------------------------------------------------


def test_basket_warmup_pushes_fetch_start_back_by_slow_window(monkeypatch):
    """sma_cross_basic: fast_window=5, slow_window=20 -> the cross entry needs
    the slow SMA at t and t-1, so warmup_bars == slow_window (20), not
    slow_window - 1 (see _basket_warmup_bars docstring)."""
    case = CASES["sma_cross_basic"]
    calls = _install_leg_fetch(monkeypatch, case)
    body = _post(_basket_request(case, run_id="warmup-check"))
    assert body["result_status"] == "success", body
    step = provider._timeframe_milliseconds(case["envelope"]["timeframe"]) // 1000
    expected_start = case["envelope"]["start_at"] - 20 * step
    assert len(calls) == 2
    for _exchange, _market, _symbol, _timeframe, start_sec, end_sec in calls:
        assert start_sec == expected_start
        assert end_sec == case["envelope"]["end_at"]


# ---------------------------------------------------------------------------
# catalog：三个 tool 进入方式与既有模板一致，但不含 v3 schema 字符串、不填
# supported_symbols
# ---------------------------------------------------------------------------


def test_basket_catalog_entries_have_no_v3_strings_and_empty_supported_symbols():
    body = TestClient(provider.app).get("/catalog").json()
    catalog_text = json.dumps(body)
    assert "cutie.strategy_spec.v3" not in catalog_text
    assert "cutie.backtest_result.v3" not in catalog_text
    tools_by_id = {tool["tool_id"]: tool for tool in body["tools"]}
    for tool_id in _FAMILY_TOOL_ID.values():
        tool = tools_by_id[tool_id]
        assert tool["supported_symbols"] == []
        assert "legs" in tool["param_schema"]["properties"]


def test_capability_payload_never_mentions_v3():
    payload = provider.capability_payload("a" * 40)
    payload_text = json.dumps(payload)
    assert "cutie.strategy_spec.v3" not in payload_text
    assert "cutie.backtest_result.v3" not in payload_text
    assert "rolling_zscore" not in payload_text


# ---------------------------------------------------------------------------
# 失败契约：参数非法（legs 非法 / 越界）、取数失败各一个失败码用例
# ---------------------------------------------------------------------------


def test_basket_duplicate_leg_symbol_is_invalid_params(monkeypatch):
    case = copy.deepcopy(CASES["sma_cross_basic"])
    case["provider_params"]["legs"][1]["symbol"] = case["provider_params"]["legs"][0]["symbol"]
    _install_leg_fetch(monkeypatch, CASES["sma_cross_basic"])
    body = _post(_basket_request(case, run_id="dup-symbol"))
    assert body["result_status"] == "failed", body
    assert body["error_type"] == "INVALID_PARAMS"


def test_basket_fast_window_out_of_range_is_invalid_params(monkeypatch):
    case = copy.deepcopy(CASES["sma_cross_basic"])
    case["provider_params"]["fast_window"] = 51  # schema max is 50 (§6.1)
    _install_leg_fetch(monkeypatch, CASES["sma_cross_basic"])
    body = _post(_basket_request(case, run_id="fast-window-oob"))
    assert body["result_status"] == "failed", body
    assert body["error_type"] == "INVALID_PARAMS"


def test_basket_leg_fetch_failure_is_no_data(monkeypatch):
    case = CASES["sma_cross_basic"]

    def fetch(exchange_id, market, symbol, timeframe, start_sec, end_sec):
        raise provider.MarketDataFetchError(
            "NO_DATA", "No OHLCV data is available in the requested range",
            central_failure_reason=None, central_attempted=False,
        )

    monkeypatch.setattr(provider, "_fetch_ohlcv_raw", fetch)
    body = _post(_basket_request(case, run_id="fetch-fail"))
    assert body["result_status"] == "failed", body
    assert body["error_type"] == "NO_DATA"


# ---------------------------------------------------------------------------
# 精度：调用方全局 Decimal 上下文不得影响结果（§2.6.1 精度上下文）
# ---------------------------------------------------------------------------


def test_basket_result_is_independent_of_caller_decimal_context(monkeypatch):
    case = CASES["sums_exact"]
    _install_leg_fetch(monkeypatch, case)
    default_body = _post(_basket_request(case, run_id="prec-default"))
    assert default_body["result_status"] == "success", default_body

    _install_leg_fetch(monkeypatch, case)
    previous_prec = decimal.getcontext().prec
    decimal.getcontext().prec = 10
    try:
        low_prec_body = _post(_basket_request(case, run_id="prec-10"))
    finally:
        decimal.getcontext().prec = previous_prec
    assert low_prec_body["result_status"] == "success", low_prec_body

    for key in ("metrics", "trades", "equity_curve", "data_manifests", "strategy_spec_hash"):
        assert low_prec_body[key] == default_body[key], key
