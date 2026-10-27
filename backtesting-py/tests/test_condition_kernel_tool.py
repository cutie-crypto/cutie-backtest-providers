"""JES21-B3b 二期 K2：provider 新工具 ``local.backtesting_py.condition_kernel``。

runner="kernel_v2_conditions"，provider_params = {strategy_spec, data_requirements}：
provider 用自身 v2 校验器 fail closed，按 data_requirements 取数（中心 K 线 +
metrics.*），在 v2 内核上执行，结果映射到 run 结果形（result.v2 单标的键 +
kernel_v3 的 strategy_spec_json / strategy_spec_hash），assumptions 带实际执行参数。
"""
from __future__ import annotations

import copy
import hashlib
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
START_AT = 10 * DAY
END_AT = 13 * DAY
INSTRUMENT_RULES = {
    "symbol": "BTCUSDT",
    "price_tick": "0.1",
    "qty_step": "0.001",
    "min_qty": "0.001",
    "min_notional": "5",
}
METRIC_STREAM = "metrics.market.fear_greed_index"


# --- spec / requirement builders ------------------------------------------------

def _kline_feature(key: str, primitive: str, params: dict) -> dict:
    return {
        "key": key,
        "primitive": primitive,
        "primitive_version": "1",
        "source_stream": "kline.primary.close",
        "interval": "1h",
        "value_kind": "price",
        "output_type": "decimal",
        "params": params,
        "required": True,
    }


def _metric_feature() -> dict:
    return {
        "key": "fg",
        "primitive": "rolling_extreme",
        "primitive_version": "1",
        "source_stream": METRIC_STREAM,
        "interval": "1d",
        "value_kind": "level",
        "output_type": "decimal",
        "params": {"window_bars": 1, "mode": "max"},
        "required": True,
    }


def _compare(key: str, op: str, value: str) -> dict:
    return {
        "node": "compare",
        "op": op,
        "left": {"node": "feature", "key": key, "lag_bars": 0},
        "right": tsk._literal(value),
    }


def _primary_requirement(warmup_bars: int) -> dict:
    requirement = copy.deepcopy(tsk.make_manifest(tsk.make_spec())["data_requirements"][0])
    requirement["warmup_bars"] = warmup_bars
    return requirement


def _metric_requirement(warmup_bars: int = 1) -> dict:
    return {
        "stream_id": f"{METRIC_STREAM}.1d",
        "kind": "feature",
        "execution_role": "feature_input",
        "provider": "cutie",
        "storage_source": "market_metrics_history",
        "result_source": None,
        "exchange": "auto",
        "market": "futures",
        "symbols": ["BTCUSDT"],
        "interval": "1d",
        "warmup_bars": warmup_bars,
        "max_freshness_seconds": 108000,
        "gap_policy": "none",
        "allowed_transforms": [],
    }


def _rsi_spec(**kwargs) -> dict:
    return tsk.make_spec(
        features=[_kline_feature("rsi", "rsi_wilder", {"period": 2})],
        condition=_compare("rsi", "lt", "30"),
        **kwargs,
    )


def _ema_short_spec() -> dict:
    condition = {
        "node": "cross",
        "op": "crosses_below",
        "left": {"node": "feature", "key": "ema_fast", "lag_bars": 0},
        "right": {"node": "feature", "key": "ema_slow", "lag_bars": 0},
    }
    return tsk.make_spec(
        side="short",
        features=[
            _kline_feature("ema_fast", "ema", {"window_bars": 2}),
            _kline_feature("ema_slow", "ema", {"window_bars": 5}),
        ],
        condition=condition,
    )


def _envelope(spec: dict, requirements: list, **overrides) -> dict:
    backtest = {
        "run_id": "ck-1",
        "provider_tool_id": TOOL_ID,
        "provider_params": {"strategy_spec": spec, "data_requirements": requirements},
        "symbol": "BTCUSDT",
        "market": "futures",
        "timeframe": "1h",
        "start_at": START_AT,
        "end_at": END_AT,
        "initial_capital": "10000",
        "fee_bps": "10",
        "slippage_bps": "5",
        "instrument_rules": dict(INSTRUMENT_RULES),
    }
    backtest.update(overrides)
    return {"backtest": backtest, "provider": {}}


# --- data ------------------------------------------------------------------------

def _close_at(ts: int) -> int:
    """Triangle wave, period 20 bars: RSI and EMAs cross repeatedly."""
    phase = (ts // HOUR) % 20
    return 100 + (phase if phase < 10 else 20 - phase)


@pytest.fixture
def market_data(monkeypatch):
    monkeypatch.setattr(provider, "PROVIDER_REVISION", tsk.REVISION)
    metric_calls: list[dict] = []
    state = {"metric_value": lambda ts: "50"}

    def fetch_klines(exchange, market, symbol, timeframe, start_ms, end_ms):
        rows = []
        for ts in range(start_ms // 1000, end_ms // 1000, HOUR):
            close = _close_at(ts)
            rows.append([ts * 1000, close, close + 1, close - 1, close, 1])
        return rows

    def fetch_metric_chunk(*, symbol, metric, interval, exchange, start_at, end_at):
        metric_calls.append({"symbol": symbol, "metric": metric, "exchange": exchange})
        first = -(-start_at // DAY) * DAY
        return [{"ts": ts, "value": state["metric_value"](ts)} for ts in range(first, end_at + 1, DAY)]

    monkeypatch.setattr(provider, "_fetch_from_central", fetch_klines)
    monkeypatch.setattr(provider, "_fetch_artifact_metric_chunk", fetch_metric_chunk)
    return {"metric_calls": metric_calls, "state": state}


def _post(envelope: dict) -> dict:
    response = TestClient(provider.app).post("/cutie/backtest", json=envelope)
    assert response.status_code == 200, response.text
    return response.json()


def _assert_result_v2_shape(body: dict) -> None:
    assert body["schema_version"] == "cutie.backtest_result.v2"
    result_v2 = {key: body[key] for key in ("schema_version", "trades", "equity_curve", "metrics", "data_manifest")}
    assert body["result_hash"] == canonical_json_sha256(result_v2)
    assert set(body["metrics"]) == {"total_return", "max_drawdown", "trade_count"}
    assert body["metrics"]["trade_count"] == len(body["trades"])
    assert [trade["seq"] for trade in body["trades"]] == list(range(1, len(body["trades"]) + 1))
    manifest = body["data_manifest"]
    assert {key: manifest[key] for key in ("source", "symbol", "market", "timeframe", "start_at", "end_at")} == {
        "source": "binance_futures", "symbol": "BTCUSDT", "market": "futures",
        "timeframe": "1h", "start_at": START_AT, "end_at": END_AT,
    }
    assert manifest["kline_count"] == (END_AT - START_AT) // HOUR


# --- registration ---------------------------------------------------------------

def test_tool_is_registered_with_runner_and_catalog_entry(monkeypatch):
    spec = provider.TOOL_SPECS[TOOL_ID]
    assert spec["runner"] == "kernel_v2_conditions"
    assert set(spec["param_schema_properties"]) == {"strategy_spec", "data_requirements"}
    assert "build" not in spec
    assert TOOL_ID in provider.POSITION_SIZING_UNWIRED_TOOLS
    monkeypatch.setattr(provider, "PROVIDER_REVISION", tsk.REVISION)
    catalog = TestClient(provider.app).get("/catalog").json()
    entry = next(tool for tool in catalog["tools"] if tool["tool_id"] == TOOL_ID)
    assert entry["markets"] == ["futures"]
    assert set(entry["param_schema"]["properties"]) == {"strategy_spec", "data_requirements"}


# --- execution --------------------------------------------------------------------

def test_rsi_single_long_runs_on_v2_kernel_and_reports_spec_hash(market_data):
    spec = _rsi_spec()
    body = _post(_envelope(spec, [_primary_requirement(20)]))
    assert body["result_status"] == "success", body
    _assert_result_v2_shape(body)
    assert body["trades"]
    assert {trade["side"] for trade in body["trades"]} == {"long"}
    expected_hash = canonical_json_sha256(spec)
    assert body["strategy_spec_hash"] == expected_hash
    assert hashlib.sha256(body["strategy_spec_json"].encode("utf-8")).hexdigest() == expected_hash
    assumptions = body["assumptions"]
    assert assumptions["strategy_spec_hash"] == expected_hash
    executed = assumptions["executed_parameters"]
    assert executed["engine"] == "strategy_kernel_v2"
    assert (executed["side"], executed["leverage"], executed["timeframe"]) == ("long", "2", "1h")
    assert executed["features"] == [{
        "key": "rsi", "primitive": "rsi_wilder", "params": {"period": 2},
        "source": "kline", "source_stream": "kline.primary.close", "scope": "self",
        "metric": "close", "interval": "1h", "granularity": "primary",
        "stream_id": "binance.futures.kline.1h.BTCUSDT", "data_start_at": START_AT,
    }]
    raw = body["raw_report"]
    assert raw["coverage_manifest_hash"] == canonical_json_sha256(raw["coverage_manifest"])


def test_spec_hash_follows_the_spec(market_data):
    first = _post(_envelope(_rsi_spec(), [_primary_requirement(20)]))
    other = _rsi_spec()
    other["entry"]["condition"]["right"]["value"] = "25"
    second = _post(_envelope(other, [_primary_requirement(20)]))
    assert first["result_status"] == second["result_status"] == "success"
    assert first["strategy_spec_hash"] != second["strategy_spec_hash"]
    assert second["strategy_spec_hash"] == canonical_json_sha256(other)


def test_ema_cross_short_trades_are_all_short(market_data):
    body = _post(_envelope(_ema_short_spec(), [_primary_requirement(5)]))
    assert body["result_status"] == "success", body
    _assert_result_v2_shape(body)
    assert body["trades"]
    assert {trade["side"] for trade in body["trades"]} == {"short"}
    assert body["assumptions"]["executed_parameters"]["side"] == "short"


def test_null_stop_take_exit_by_holding_bars(market_data):
    spec = _rsi_spec(time_exit_bars=3)
    spec["exit"]["stop_loss"] = None
    spec["exit"]["take_profit"] = None
    body = _post(_envelope(spec, [_primary_requirement(20)]))
    assert body["result_status"] == "success", body
    _assert_result_v2_shape(body)
    assert body["trades"]
    held = {(trade["closed_at"] - trade["opened_at"]) // HOUR for trade in body["trades"][:-1]}
    assert held == {3}
    executed = body["assumptions"]["executed_parameters"]
    assert (executed["stop_loss"], executed["take_profit"], executed["time_exit_bars"]) == (None, None, 3)


def test_metrics_daily_asof_condition_reads_only_closed_days(market_data):
    """IMPL §5 验收 3：第 i 根决策只用 ts + 86400 ≤ 收盘时刻的点。"""
    switch_day = 11 * DAY
    market_data["state"]["metric_value"] = lambda ts: "90" if ts >= switch_day else "10"
    spec = tsk.make_spec(features=[_metric_feature()], condition=_compare("fg", "gt", "50"))
    requirements = sorted(
        [_primary_requirement(0), _metric_requirement(1)], key=lambda item: item["stream_id"]
    )
    body = _post(_envelope(spec, requirements))
    assert body["result_status"] == "success", body
    _assert_result_v2_shape(body)
    # The 11-day point becomes known only once that day has closed (bar closing
    # at switch_day + 1d); the fill is the next bar's open.
    assert body["trades"][0]["opened_at"] == switch_day + DAY
    assert {call["metric"] for call in market_data["metric_calls"]} == {"fear_greed_index"}
    feature = body["assumptions"]["executed_parameters"]["features"][0]
    assert {key: feature[key] for key in ("source", "scope", "metric", "interval", "granularity")} == {
        "source": "metric", "scope": "market", "metric": "fear_greed_index",
        "interval": "1d", "granularity": "daily_asof",
    }
    assert feature["stream_id"].startswith(f"{METRIC_STREAM}.1d")
    assert isinstance(feature["data_start_at"], int) and feature["data_start_at"] <= START_AT


# --- fail closed --------------------------------------------------------------------

def _with_unknown_spec_key() -> dict:
    spec = _rsi_spec()
    spec["surprise"] = True
    return spec


def _with_float_threshold() -> dict:
    spec = _rsi_spec()
    spec["risk"]["leverage"] = 2.5
    return spec


@pytest.mark.parametrize(
    "provider_params",
    [
        pytest.param({"strategy_spec": _with_unknown_spec_key(), "data_requirements": [_primary_requirement(20)]},
                     id="unknown-spec-key"),
        pytest.param({"strategy_spec": _with_float_threshold(), "data_requirements": [_primary_requirement(20)]},
                     id="float-in-spec"),
        pytest.param({"strategy_spec": _rsi_spec(), "data_requirements": [_primary_requirement(2)]},
                     id="rsi-warmup-too-short"),
        pytest.param({"strategy_spec": _rsi_spec(), "data_requirements": []}, id="no-primary"),
        pytest.param({"strategy_spec": _rsi_spec()}, id="missing-data-requirements"),
        pytest.param({"strategy_spec": "x", "data_requirements": [_primary_requirement(20)]}, id="spec-not-object"),
        pytest.param({"strategy_spec": {}, "data_requirements": [{}]}, id="empty-shapes"),
        pytest.param({"strategy_spec": _rsi_spec(), "data_requirements": [_primary_requirement(20)], "x": 1},
                     id="extra-param"),
    ],
)
def test_invalid_spec_fails_closed_as_invalid_params(market_data, provider_params):
    envelope = _envelope(_rsi_spec(), [])
    envelope["backtest"]["provider_params"] = provider_params
    body = _post(envelope)
    assert body["result_status"] == "failed"
    assert body["error_type"] == "INVALID_PARAMS", body


def test_symbol_outside_spec_fails_closed(market_data):
    body = _post(_envelope(_rsi_spec(), [_primary_requirement(20)], symbol="ETHUSDT"))
    assert body["result_status"] == "failed"
    assert body["error_type"] == "INVALID_PARAMS"
    assert "ERR_STRATEGY_EXECUTION_BINDING_MISMATCH" in body["error_message"]


def test_missing_instrument_rules_fails_closed(market_data):
    envelope = _envelope(_rsi_spec(), [_primary_requirement(20)])
    del envelope["backtest"]["instrument_rules"]
    body = _post(envelope)
    assert (body["result_status"], body["error_type"]) == ("failed", "INVALID_PARAMS")


@pytest.mark.parametrize("where", ["envelope", "spec"])
def test_spot_is_rejected_explicitly(market_data, where):
    spec = _rsi_spec()
    overrides = {}
    if where == "envelope":
        overrides["market"] = "spot"
    else:
        spec["market"]["market_type"] = "spot"
    body = _post(_envelope(spec, [_primary_requirement(20)], **overrides))
    assert body["result_status"] == "failed"
    assert body["error_type"] == "MARKET_UNSUPPORTED"
    assert "spot" in body["error_message"]


# --- result shape vs kernel_v3 -----------------------------------------------------------

def test_result_keys_match_kernel_v3_envelope(market_data, monkeypatch):
    body = _post(_envelope(_rsi_spec(), [_primary_requirement(20)]))
    assert body["result_status"] == "success", body
    case = tb.CASES[sorted(tb.CASES)[0]]
    tb._install_leg_fetch(monkeypatch, case)
    v3_body = tb._post(tb._basket_request(case))
    assert v3_body["result_status"] == "success", v3_body
    # Single-symbol v2 evidence replaces the basket's plural manifests; every
    # other kernel_v3 key (strategy_spec_json / strategy_spec_hash included) is shared.
    expected = (set(v3_body) - {"data_manifests", "data_manifests_hash"}) | {"data_manifest", "result_hash"}
    assert set(body) == expected
