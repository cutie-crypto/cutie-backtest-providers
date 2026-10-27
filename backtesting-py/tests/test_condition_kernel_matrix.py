"""JES21-B3b-M1p：provider 仓 condition_kernel 矩阵。

对夹具 ``b3b_condition_matrix_specs.json`` 的 462 条 case（21 个可编译指标原子的
单项 多/空 + 两两 all/any）各跑一次 ``local.backtesting_py.condition_kernel``
回测，断言：状态 success（不得 fail closed）、结果顶层键与 kernel_v3 同键、
``raw_report.trade_exit_kinds`` 与成交逐笔对齐、做多/做空 case 的成交方向一致、
能力 hash 未变。

行情打桩照 ``test_condition_kernel_tool.py`` 的 ``market_data``：确定性合成 1h
K 线（100 根锯齿波，快涨 + 急跌，覆盖 rsi_wilder / ema / macd / rolling_zscore /
change_24h 的高低点位）与日线 metrics（6 日周期序列，含负值与极大正跳，覆盖
gt/lt/lte 阈值与算术比值；分母恒非零）。主回测窗口 500 根 1h，预热根数由
``data_requirements.warmup_bars`` 决定，打桩从请求起点生成（含预热）。

抽样规则：实测 462 条全跑约 20s（远低于 3 分钟），故不抽样、全量跑；若日后变慢，
按 ``_select_cases`` 的两两抽 stride 机制抽样（单项 42 条始终全跑）。
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cutie_backtesting_provider as provider  # noqa: E402
import test_basket_signal_http as tb  # noqa: E402
import test_strategy_kernel as tsk  # noqa: E402
from canonical_json import canonical_json_sha256  # noqa: E402

TOOL_ID = "local.backtesting_py.condition_kernel"
HOUR = 3600
DAY = 86400
FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "b3b_condition_matrix_specs.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
START_AT = FIXTURE["start_at"]
# 主回测窗口 500 根 1h；预热由 data_requirements.warmup_bars 决定，打桩按请求起点生成。
MAIN_BARS = 500
END_AT = START_AT + MAIN_BARS * HOUR
SYMBOL = FIXTURE["symbol"]
TIMEFRAME = FIXTURE["timeframe"]
FEE_BPS = FIXTURE["fee_bps"]
SLIPPAGE_BPS = FIXTURE["slippage_bps"]
INSTRUMENT_RULES = {
    "symbol": "BTCUSDT",
    "price_tick": "0.1",
    "qty_step": "0.001",
    "min_qty": "0.001",
    "min_notional": "5",
}
# 能力 hash：与 tests/fixtures/strategy_execution_capability_v1.json 的 sha256 一致
# （test_strategy_kernel.test_provider_capability_fixture_exact_payload_and_hash 钉死）。
CAPABILITY_HASH = "1349f70783dbaa6f743ce1d833f47cb315cbe6eda3c1dc0f980779142ccbb326"
# kernel_v3 信封顶层键（照 test_condition_kernel_tool.test_result_keys_match_kernel_v3_envelope：
# basket 的复数 manifests 换成单标的 data_manifest + result_hash）。
V3_KEYS = frozenset({
    "assumptions", "central_market_data_auth_mode", "central_market_data_used",
    "data_manifest", "data_source", "engine_name", "engine_version", "equity_curve",
    "initial_capital", "limitations", "market_data_cache_hit", "metrics", "provider_name",
    "provider_revision", "provider_run_id", "raw_report", "result_hash", "result_status",
    "schema", "schema_version", "strategy_spec_hash", "strategy_spec_json", "trades",
})

CASES = sorted(FIXTURE["cases"], key=lambda case: case["case_id"])


def _select_cases() -> list[dict]:
    """单项全跑；两两默认全跑（stride=1）。若全跑超时，调大 stride 稳定抽样。"""
    stride = 1
    singles = [case for case in CASES if case["case_id"].startswith("single:")]
    pairs = [case for case in CASES if case["case_id"].startswith("pair:")]
    return singles + [case for index, case in enumerate(pairs) if index % stride == 0]


SELECTED_CASES = _select_cases()


# --- 确定性行情 ------------------------------------------------------------------

# 100 根锯齿波：0..79 缓涨（100→179），80..99 急跌（180→104）。缓涨段给
# change_24h 正动量与 ema/macd 上穿，急跌段给 rsi<30 与 rolling_zscore<=-2。
def _close_at(ts: int) -> int:
    phase = (ts // HOUR) % 100
    # 缓涨段 phase 17（锯齿基值 117）叠加确定性上冲尖峰到 120：把该点 20 根
    # 滚动 zscore 推过 +2，触发 single:bollinger_touch:short 的 zscore_20>=2
    # 做空；缓涨段的轻微上冲几乎不改变 Wilder RSI，故其余 case（尤其
    # X+rsi:long:all）的既有成交覆盖保持，急跌段 zscore<=-2 的做多触发也不受影响。
    if phase == 17:
        return 120
    return 100 + phase if phase < 80 else 100 + (100 - phase) * 4


# 6 日周期日线值：有 lt 阈值下的负值、有 gt 阈值下的大正值、算术比值分母恒非零。
_METRIC_SERIES = ("-100", "300", "700", "1100", "1500", "1000000000")


def _metric_value(ts: int) -> str:
    return _METRIC_SERIES[(ts // DAY) % len(_METRIC_SERIES)]


@pytest.fixture
def market_data(monkeypatch):
    monkeypatch.setattr(provider, "PROVIDER_REVISION", tsk.REVISION)

    def fetch_klines(exchange, market, symbol, timeframe, start_ms, end_ms):
        rows = []
        for ts in range(start_ms // 1000, end_ms // 1000, HOUR):
            close = _close_at(ts)
            rows.append([ts * 1000, close, close + 1, close - 1, close, 1])
        return rows

    def fetch_metric_chunk(*, symbol, metric, interval, exchange, start_at, end_at):
        first = -(-start_at // DAY) * DAY
        return [{"ts": ts, "value": _metric_value(ts)} for ts in range(first, end_at + 1, DAY)]

    monkeypatch.setattr(provider, "_fetch_from_central", fetch_klines)
    monkeypatch.setattr(provider, "_fetch_artifact_metric_chunk", fetch_metric_chunk)


def _envelope(case: dict) -> dict:
    return {
        "backtest": {
            "run_id": f"ck-matrix-{case['case_id']}",
            "provider_tool_id": TOOL_ID,
            "provider_params": case["provider_params"],
            "symbol": SYMBOL,
            "market": "futures",
            "timeframe": TIMEFRAME,
            "start_at": START_AT,
            "end_at": END_AT,
            "initial_capital": "10000",
            "fee_bps": FEE_BPS,
            "slippage_bps": SLIPPAGE_BPS,
            "instrument_rules": dict(INSTRUMENT_RULES),
        },
        "provider": {},
    }


def _post(case: dict) -> dict:
    response = TestClient(provider.app).post("/cutie/backtest", json=_envelope(case))
    assert response.status_code == 200, response.text
    return response.json()


# --- 断言 ------------------------------------------------------------------------

def _assert_result_v2_shape(body: dict) -> None:
    assert body["schema_version"] == "cutie.backtest_result.v2"
    result_v2 = {
        key: body[key]
        for key in ("schema_version", "trades", "equity_curve", "metrics", "data_manifest")
    }
    assert body["result_hash"] == canonical_json_sha256(result_v2)
    assert set(body["metrics"]) == {"total_return", "max_drawdown", "trade_count"}
    assert body["metrics"]["trade_count"] == len(body["trades"])
    assert [trade["seq"] for trade in body["trades"]] == list(range(1, len(body["trades"]) + 1))
    manifest = body["data_manifest"]
    assert {
        key: manifest[key] for key in ("source", "symbol", "market", "timeframe", "start_at", "end_at")
    } == {
        "source": "binance_futures", "symbol": SYMBOL, "market": "futures",
        "timeframe": TIMEFRAME, "start_at": START_AT, "end_at": END_AT,
    }
    assert manifest["kline_count"] == (END_AT - START_AT) // HOUR


def _assert_exit_kinds_aligned(body: dict) -> None:
    trades = body["trades"]
    kinds = body["raw_report"].get("trade_exit_kinds")
    assert kinds is not None, "raw_report.trade_exit_kinds 缺失（P6 字段被省略）"
    assert len(kinds) == len(trades)
    assert [row["seq"] for row in kinds] == [trade["seq"] for trade in trades]
    assert all(row["exit_kind"] in provider.TRADE_EXIT_KINDS for row in kinds)


def _assert_trade_side(body: dict, side: str) -> None:
    trades = body["trades"]
    if trades:
        assert {trade["side"] for trade in trades} == {side}


# --- 矩阵 ------------------------------------------------------------------------

@pytest.mark.parametrize("case", SELECTED_CASES, ids=lambda case: case["case_id"])
def test_condition_matrix_case(market_data, case):
    body = _post(case)
    assert body["result_status"] == "success", (
        case["case_id"], body.get("error_type"), body.get("error_message")
    )
    assert set(body) == set(V3_KEYS)
    _assert_result_v2_shape(body)
    _assert_exit_kinds_aligned(body)
    _assert_trade_side(body, case["provider_params"]["strategy_spec"]["entry"]["side"])
    # 单项必须有成交：避免 trades 为空时方向断言空成立；pair:all 两条件同时
    # 成立在合成行情上本就少见，不要求成交。
    if case["case_id"].startswith("single:"):
        assert body["metrics"]["trade_count"] >= 1, (
            case["case_id"], "单项 case 零成交"
        )


def test_result_keys_match_kernel_v3_envelope(market_data, monkeypatch):
    """同一份单标的 condition_kernel 结果与 basket kernel_v3 结果顶层键集合同构。"""
    case = next(item for item in CASES if item["case_id"] == "single:rsi:long")
    body = _post(case)
    assert body["result_status"] == "success", body
    v3_case = tb.CASES[sorted(tb.CASES)[0]]
    tb._install_leg_fetch(monkeypatch, v3_case)
    v3_body = tb._post(tb._basket_request(v3_case))
    assert v3_body["result_status"] == "success", v3_body
    expected = (set(v3_body) - {"data_manifests", "data_manifests_hash"}) | {
        "data_manifest", "result_hash"
    }
    assert set(body) == expected


def test_capability_hash_unchanged(market_data):
    capability_pair = provider._artifact_capability_pair()
    assert capability_pair is not None
    assert capability_pair[1] == CAPABILITY_HASH


def test_mutation_flip_short_case_to_long_breaks_side_assertion(market_data):
    """③ 自证：把一个有成交的做空 case 在内存里改成做多，『全是空单』断言必红。"""
    case = next(item for item in CASES if item["case_id"] == "single:funding_rate:short")
    body = _post(case)
    assert body["result_status"] == "success", body
    assert body["trades"], "做空自证 case 必须有成交"
    _assert_trade_side(body, "short")

    mutated = copy.deepcopy(case)
    mutated["provider_params"]["strategy_spec"]["entry"]["side"] = "long"
    flipped = _post(mutated)
    assert flipped["result_status"] == "success", flipped
    assert flipped["trades"], "改做多后必须有成交"
    _assert_trade_side(flipped, "long")
    with pytest.raises(AssertionError):
        _assert_trade_side(flipped, "short")
