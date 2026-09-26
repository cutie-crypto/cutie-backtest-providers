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

import contextlib
import copy
import decimal
import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
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
    # content= 而不是 json=：信封里的 NaN 这类非法 JSON 数字也要能原样发出去。
    return TestClient(provider.app).post(
        "/cutie/backtest", content=json.dumps(request), headers={"Content-Type": "application/json"},
    ).json()


@contextlib.contextmanager
def _caller_decimal_prec(prec: int):
    """调用方全局 Decimal 精度：同时改本线程上下文与 DefaultContext——TestClient 在
    新线程里跑 handler，新线程的上下文从 DefaultContext 复制。"""
    previous_local = decimal.getcontext().prec
    previous_default = decimal.DefaultContext.prec
    decimal.getcontext().prec = prec
    decimal.DefaultContext.prec = prec
    try:
        yield
    finally:
        decimal.getcontext().prec = previous_local
        decimal.DefaultContext.prec = previous_default


def _spy_handler_decimal_prec(monkeypatch) -> list[int]:
    """记录 handler 进入组合分派前（run_backtest 的参数 schema 校验处）看到的精度。"""
    seen: list[int] = []
    original = provider._validate_params_against_schema

    def spy(params, properties):
        seen.append(decimal.getcontext().prec)
        return original(params, properties)

    monkeypatch.setattr(provider, "_validate_params_against_schema", spy)
    return seen


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
    start_at = case["envelope"]["start_at"]
    # fixture 的 K 线全在 start_at 之后，首轮前推 20 根后对齐预热 bar 仍为 0，
    # 于是再前推到 4 倍回看跨度（80 根）取一次；两轮都是两腿各一次。
    assert len(calls) == 4
    for index, (_exchange, _market, _symbol, _timeframe, start_sec, end_sec) in enumerate(calls):
        assert start_sec == start_at - (20 if index < 2 else 80) * step
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
    handler_precs = _spy_handler_decimal_prec(monkeypatch)
    with _caller_decimal_prec(10):
        low_prec_body = _post(_basket_request(case, run_id="prec-10"))
    assert low_prec_body["result_status"] == "success", low_prec_body
    assert handler_precs == [10]  # 调用方上下文确实到了 handler，本用例不是空转

    for key in ("metrics", "trades", "equity_curve", "data_manifests", "strategy_spec_hash"):
        assert low_prec_body[key] == default_body[key], key


# ---------------------------------------------------------------------------
# B2 验收返修（Codex review）
# ---------------------------------------------------------------------------


def _significant_digits(text: str) -> int:
    return len("".join(str(d) for d in Decimal(text).as_tuple().digits).strip("0"))


def _install_raw_fetch(monkeypatch, ohlcv_by_symbol: dict[str, list], *, central_used: bool = False):
    calls: list[tuple] = []

    def fetch(exchange_id, market, symbol, timeframe, start_sec, end_sec):
        calls.append((exchange_id, market, symbol, timeframe, start_sec, end_sec))
        source = "cutie_central_market_data" if central_used else provider.DATA_SOURCE
        return copy.deepcopy(ohlcv_by_symbol[symbol]), source, central_used, False

    monkeypatch.setattr(provider, "_fetch_ohlcv_raw", fetch)
    return calls


def _fixture_ohlcv(case: dict, convert) -> dict[str, list]:
    return {
        leg["symbol"]: [
            [row["open_time"] * 1000, *(convert(row[k]) for k in ("open", "high", "low", "close", "volume"))]
            for row in case["klines"][leg["leg_id"]]
        ]
        for leg in case["provider_params"]["legs"]
    }


# 1. ccxt 回退的 float 行情：repr 最短表示进内核；> 15 位有效数字整次按取数失败返回


def test_basket_ccxt_float_klines_with_short_repr_match_fixture(monkeypatch):
    case = CASES["sma_cross_basic"]
    _install_raw_fetch(monkeypatch, _fixture_ohlcv(case, float))
    body = _post(_basket_request(case, run_id="ccxt-float"))
    assert body["result_status"] == "success", body
    assert body["trades"] == case["expected_result"]["trades"]
    assert body["metrics"] == case["expected_result"]["metrics"]


def test_basket_ccxt_float_price_beyond_15_digits_is_data_fetch_failed(monkeypatch):
    case = CASES["sma_cross_basic"]
    ohlcv = _fixture_ohlcv(case, float)
    noisy = 3000.0000000000005
    assert _significant_digits(repr(noisy)) > 15
    ohlcv["ETHUSDT"][3][4] = noisy  # close of an in-window bar
    _install_raw_fetch(monkeypatch, ohlcv)

    def must_not_simulate(*args, **kwargs):
        raise AssertionError("untrusted float prices must never reach simulate_v3")

    monkeypatch.setattr(provider, "simulate_v3", must_not_simulate)
    body = _post(_basket_request(case, run_id="ccxt-noisy"))
    assert body["result_status"] == "failed", body
    assert body["error_type"] == "DATA_FETCH_FAILED"
    assert "precision" in body["error_message"]


def test_basket_float_price_trust_boundary_is_15_significant_digits():
    fifteen = 2994.12345678901
    sixteen = 2994.123456789012
    assert _significant_digits(repr(fifteen)) == 15
    assert _significant_digits(repr(sixteen)) == 16
    row = [1719000000 * 1000, fifteen, fifteen, fifteen, fifteen, 1.0]
    assert provider._basket_untrusted_float_price([row], 1719000000, 1719000001) is None
    row = [1719000000 * 1000, fifteen, fifteen, fifteen, sixteen, 1.0]
    assert provider._basket_untrusted_float_price([row], 1719000000, 1719000001) is not None
    # 中心行情是字符串，位数再多也不受这条限制；volume 不是价格字段。
    row = [1719000000 * 1000, "2994.123456789012345", "2994", "2994", "2994", 0.30000000000000004]
    assert provider._basket_untrusted_float_price([row], 1719000000, 1719000001) is None


# 2. K 线 canonical 化在 decimal128 里做：32 位有效数字的中心行情字符串原样保真


@pytest.mark.parametrize("caller_prec", [10, 28])
def test_basket_32_digit_central_price_survives_into_kernel_and_manifest(monkeypatch, caller_prec):
    case = copy.deepcopy(CASES["sma_cross_basic"])
    # 价格要落在 price_tick 网格上内核才收，tick 放到 1e-28 让 32 位报价合法。
    case["instrument_rules"]["a"]["price_tick"] = "0.0000000000000000000000000001"
    ohlcv = _fixture_ohlcv(case, str)
    bar = ohlcv["ETHUSDT"][3]
    with decimal.localcontext(decimal.Context(prec=50)):
        precise_high = str(Decimal(bar[4]) + Decimal("1e-28"))
    assert _significant_digits(precise_high) == 32
    bar[2] = precise_high
    _install_raw_fetch(monkeypatch, ohlcv, central_used=True)

    seen_klines: list[dict] = []
    original_simulate = provider.simulate_v3

    def spy(plan, leg_klines, *args, **kwargs):
        seen_klines.append(copy.deepcopy(leg_klines))
        return original_simulate(plan, leg_klines, *args, **kwargs)

    monkeypatch.setattr(provider, "simulate_v3", spy)
    handler_precs = _spy_handler_decimal_prec(monkeypatch)
    with _caller_decimal_prec(caller_prec):
        body = _post(_basket_request(case, run_id=f"prec32-{caller_prec}"))
    assert body["result_status"] == "success", body
    assert handler_precs == [caller_prec]
    kernel_row = next(r for r in seen_klines[0]["a"] if r["open_time"] == bar[0] // 1000)
    assert kernel_row["high"] == precise_high

    expected_rows = copy.deepcopy(case["klines"])
    next(r for r in expected_rows["a"] if r["open_time"] == bar[0] // 1000)["high"] = precise_high
    envelope = case["envelope"]
    expected_manifests = provider.build_data_manifests_v3(
        legs=case["provider_params"]["legs"],
        leg_klines=expected_rows,
        source=body["data_manifests"][0]["source"],
        market="futures",
        timeframe=envelope["timeframe"],
        start_at=envelope["start_at"],
        end_at=envelope["end_at"],
    )
    assert body["data_manifests"] == expected_manifests
    assert body["data_manifests"] != case["expected_result"]["data_manifests"]


# 3. 信封 fee/slippage canonical 化与 server 逐字一致。
# server：StrategyBacktestService._rebuild_run_strategy_spec_v3 在 decimal128 里对
# NUMERIC(10,4) 列值取 canonical_decimal_str（dispatch 信封里是 _decimal_to_str 的
# format(Decimal, "f")，如 "10.0000"）；下列期望值即 server 对同一数值产出的字符串。


@pytest.mark.parametrize(
    "raw,expected",
    [
        (10, "10"),
        (10.0, "10"),
        ("10", "10"),
        ("10.0000", "10"),
        (0.1, "0.1"),
        ("7.5", "7.5"),
        ("7.5000", "7.5"),
        ("0", "0"),
    ],
)
def test_basket_envelope_cost_bps_canonical_matches_server(monkeypatch, raw, expected):
    case = copy.deepcopy(CASES["sma_cross_basic"])
    case["envelope"]["fee_bps"] = raw
    case["envelope"]["slippage_bps"] = raw
    _install_leg_fetch(monkeypatch, case)
    body = _post(_basket_request(case, run_id="fee-canonical"))
    assert body["result_status"] == "success", body
    cost_model = json.loads(body["strategy_spec_json"])["execution"]["cost_model"]
    assert cost_model["fee_bps"] == expected
    assert cost_model["slippage_bps"] == expected


# 4. 非有限/非法费率：参数校验阶段 INVALID_PARAMS，不得 500


@pytest.mark.parametrize("field", ["fee_bps", "slippage_bps"])
@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity", "1E+1", "1e-05", "-1", "-0.5", float("nan"), 1e-05, True])
def test_basket_invalid_cost_bps_is_invalid_params(monkeypatch, field, raw):
    case = CASES["sma_cross_basic"]
    _install_leg_fetch(monkeypatch, case)
    request = _basket_request(case, run_id="fee-invalid")
    request["backtest"][field] = raw
    response = TestClient(provider.app).post(
        "/cutie/backtest", content=json.dumps(request), headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["result_status"] == "failed", body
    assert body["error_type"] == "INVALID_PARAMS"


# 5. 预热段腿 b 缺一根：再前推到 4 倍回看跨度，首根执行 bar 的慢均线有值、金叉照常触发


def test_basket_warmup_refetches_when_leg_gap_leaves_too_few_aligned_bars(monkeypatch):
    base = CASES["sma_cross_basic"]
    step = 4 * 3600
    start_at = base["envelope"]["start_at"]
    end_at = start_at + 10 * step
    params = copy.deepcopy(base["provider_params"])
    params.update({"fast_window": 2, "slow_window": 5, "cooldown_bars": 0})

    def row(open_time: int, price: str) -> list:
        return [open_time * 1000, price, price, price, price, "1"]

    # a 在预热段单调下行（快线 < 慢线），首根执行 bar 跳涨 -> 首根执行 bar 上金叉。
    a_rows = [row(start_at + k * step, str(3000 - 10 * k)) for k in range(-25, 0)]
    a_rows += [row(start_at + k * step, "3500") for k in range(0, 10)]
    b_rows = [row(start_at + k * step, "60000") for k in range(-25, 10) if k != -3]
    calls = _install_raw_fetch(monkeypatch, {"ETHUSDT": a_rows, "BTCUSDT": b_rows}, central_used=True)

    case = copy.deepcopy(base)
    case["provider_params"] = params
    case["envelope"]["end_at"] = end_at
    spec = provider.build_strategy_spec_v3(
        "basket_ratio_sma_cross", params, {"timeframe": "4h", "fee_bps": "10", "slippage_bps": "5"}
    )
    assert provider._basket_warmup_bars(spec) == 5

    body = _post(_basket_request(case, run_id="warmup-gap"))
    assert body["result_status"] == "success", body
    assert body["metrics"]["trade_count"] == 1
    assert body["trades"][0]["opened_at"] == start_at + step  # 首根执行 bar 金叉，t+1.open 成交
    assert [c[4] for c in calls] == [start_at - 5 * step] * 2 + [start_at - 20 * step] * 2
    # manifest 只计 start_at <= open_time < end_at
    for manifest in body["data_manifests"]:
        assert manifest["kline_count"] == 10


def test_basket_warmup_still_short_after_widening_runs_without_error(monkeypatch):
    """放宽到 4 倍仍不足（上市不久）：照常回测，特征缺值不触发，不报错。"""
    base = CASES["sma_cross_basic"]
    step = 4 * 3600
    start_at = base["envelope"]["start_at"]
    params = copy.deepcopy(base["provider_params"])
    params.update({"fast_window": 2, "slow_window": 5, "cooldown_bars": 0})
    rows = {
        "ETHUSDT": [[(start_at + k * step) * 1000, "3000", "3000", "3000", "3000", "1"] for k in range(-2, 10)],
        "BTCUSDT": [[(start_at + k * step) * 1000, "60000", "60000", "60000", "60000", "1"] for k in range(-2, 10)],
    }
    calls = _install_raw_fetch(monkeypatch, rows, central_used=True)
    case = copy.deepcopy(base)
    case["provider_params"] = params
    case["envelope"]["end_at"] = start_at + 10 * step
    body = _post(_basket_request(case, run_id="warmup-short"))
    assert body["result_status"] == "success", body
    assert len(calls) == 4
    assert body["metrics"]["trade_count"] == 0


# ---------------------------------------------------------------------------
# 123 B2b（D4）：信封 initial_capital 取自 NUMERIC 列（"10000.00000000"），进内核前
# 规范化成 canonical 串；结果与传 "10000" 逐字一致。非法值仍 INVALID_PARAMS，
# 内核自身的 canonical 校验不放宽。
# ---------------------------------------------------------------------------


def _body_without_run_identity(body: dict) -> dict:
    return {k: v for k, v in body.items() if k not in {"provider_run_id", "elapsed_ms"}}


@pytest.mark.parametrize("raw", ["10000.00000000", "10000.0", "010000", 10000, 10000.0])
def test_basket_numeric_column_capital_matches_canonical_capital(monkeypatch, raw):
    case = copy.deepcopy(CASES["sma_cross_basic"])
    _install_leg_fetch(monkeypatch, case)
    baseline = _post(_basket_request(case, run_id="capital"))
    assert baseline["result_status"] == "success", baseline

    request = _basket_request(case, run_id="capital")
    request["backtest"]["initial_capital"] = raw
    body = _post(request)
    assert body["result_status"] == "success", body
    assert json.dumps(_body_without_run_identity(body), sort_keys=True) == json.dumps(
        _body_without_run_identity(baseline), sort_keys=True
    )


@pytest.mark.parametrize(
    "raw", ["abc", "", "0", "0.000", "-1", "NaN", "Infinity", "1E+4", float("nan"), True, None, "1" + "0" * 40 + ".1"]
)
def test_basket_invalid_capital_is_still_invalid_params(monkeypatch, raw):
    case = CASES["sma_cross_basic"]
    _install_leg_fetch(monkeypatch, case)
    request = _basket_request(case, run_id="capital-invalid")
    request["backtest"]["initial_capital"] = raw
    response = TestClient(provider.app).post(
        "/cutie/backtest", content=json.dumps(request), headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["result_status"] == "failed", body
    assert body["error_type"] == "INVALID_PARAMS"


def test_kernel_still_rejects_non_canonical_capital():
    """规范化只发生在 provider 入口；内核 simulate_v3 对多余尾零照旧拒收。"""
    from strategy_kernel import StrategyContractError, compile_strategy_v3, simulate_v3
    from strategy_spec_v3_builder import build_strategy_spec_v3

    case = CASES["sma_cross_basic"]
    envelope = {k: case["envelope"][k] for k in ("timeframe", "fee_bps", "slippage_bps")}
    plan = compile_strategy_v3(build_strategy_spec_v3(_family_of(case), case["provider_params"], envelope))
    with pytest.raises(StrategyContractError) as excinfo:
        simulate_v3(
            plan, case["klines"], case["instrument_rules"], "10000.00000000",
            start_at=case["envelope"]["start_at"], end_at=case["envelope"]["end_at"],
        )
    assert excinfo.value.path == "$.initial_capital"
