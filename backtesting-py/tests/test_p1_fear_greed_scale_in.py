"""P1 恐贪分批模板 fear_greed_scale_in + 通用外部日频序列拉取 _fetch_metric_series。

中心 /metrics 全部用假数据（mock `_fetch_artifact_metric_chunk` 或假 opener），不连网。
"""
from __future__ import annotations

import io
import json
import sys
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as provider
from strategy_kernel import ERR_COVERAGE_INCOMPLETE, StrategyContractError

D = Decimal
TOOL = "local.backtesting_py.fear_greed_scale_in"
DAY = 86400
START = 1704067200  # 2024-01-01 00:00 UTC
LABELS = ("MARKET", "alternative_me", "fear_greed_index", "1d")
BASE = {"buy_notional": 100}


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(provider, "REPORTS_DIR", tmp_path / "reports")
    return TestClient(provider.app)


def _install(monkeypatch, *, days, fg, start=START, offset=0, closes=None, drop=(), calls=None):
    """K 线：start 前 1 根预热 + days 根日线（开盘价 = 收盘价 = 100）；恐贪：fg[i] 对应第 i 天，
    其余天默认 50，覆盖 start 前后各若干天，drop 里的天（相对 start 的序号）缺失。"""
    times = [start - DAY + offset + i * DAY for i in range(days + 1)]
    prices = [100.0] * (days + 1) if closes is None else [100.0, *closes]
    frame = pd.DataFrame(
        dict(Open=prices, Close=prices, High=[p + 1 for p in prices], Low=[p - 1 for p in prices],
             Volume=[10] * len(times)),
        index=pd.to_datetime(times, unit="s"),
    )

    def fetch_ohlcv(exchange, market, symbol, timeframe, start_at, end_at):
        return frame.loc[(frame.index >= pd.to_datetime(start_at, unit="s"))
                         & (frame.index < pd.to_datetime(end_at, unit="s"))].copy()

    store = {start + i * DAY: fg.get(i, 50) for i in range(-10, days + 10) if i not in drop}

    def fetch_chunk(*, symbol, metric, interval, exchange, start_at, end_at):
        if calls is not None:
            calls.append(dict(symbol=symbol, metric=metric, interval=interval, exchange=exchange,
                              start_at=start_at, end_at=end_at))
        if (symbol, exchange, metric, interval) != LABELS:  # 中心表按原样标签匹配，大小写不同就是没数据
            return []
        return [{"ts": ts, "value": str(v)} for ts, v in sorted(store.items()) if start_at <= ts <= end_at]

    monkeypatch.setattr(provider, "_fetch_ohlcv", fetch_ohlcv)
    monkeypatch.setattr(provider, "_fetch_artifact_metric_chunk", fetch_chunk)
    return times[1:]


def _post(client, *, days, params=None, start=START, market="spot", timeframe="1d", extra=None):
    payload = dict(run_id="fg_test", provider_tool_id=TOOL,
                   provider_params={"exchange": "binance", **(BASE if params is None else params)},
                   symbol="BTCUSDT", market=market, timeframe=timeframe, start_at=start,
                   end_at=start + days * DAY, initial_capital="10000", fee_bps="0", slippage_bps="0",
                   **(extra or {}))
    response = client.post("/cutie/backtest", json={"backtest": payload})
    assert response.status_code == 200
    return response.json()


# ---------------------------------------------------------------- 阈值买入 / 分批上限 / 阈值卖出

def test_threshold_buy_lot_cap_and_sell_all(client, monkeypatch):
    times = _install(monkeypatch, days=10, fg={1: 10, 2: 20, 3: 5, 5: 80, 7: 15})
    body = _post(client, days=10, params={**BASE, "max_lots": 2})
    assert body["result_status"] == "success", body
    a = body["assumptions"]
    # 第 1、2 天信号 → 第 2、3 天开盘买；第 3 天到上限跳过；第 5 天 = 80 → 第 6 天开盘全卖；
    # 第 7 天再买 → 第 8 天开盘，期末强平。
    assert [(t["opened_at"], t["closed_at"]) for t in body["trades"]] == [
        (times[2], times[6]), (times[3], times[6]), (times[8], times[-1] + DAY)]
    assert (a["buy_fills"], a["sell_fills"], a["lot_cap_skips"]) == (3, 1, 1)
    assert (a["buy_signals"], a["sell_signals"], a["sell_mode"]) == (3, 1, "sell_all")
    assert a["position_mode"] == "fear_greed_scale_in" and a["max_lots"] == 2
    assert all(D(t["entry_price"]) * D(t["qty"]) <= D(100) for t in body["trades"])


def test_thresholds_are_inclusive_and_neutral_values_hold(client, monkeypatch):
    _install(monkeypatch, days=6, fg={1: 21, 2: 79})
    body = _post(client, days=6)
    assert body["result_status"] == "success", body
    assert body["trades"] == [] and body["assumptions"]["buy_fills"] == 0
    _install(monkeypatch, days=6, fg={1: 20, 3: 80})
    body = _post(client, days=6)
    assert body["assumptions"]["buy_fills"] == 1 and body["assumptions"]["sell_fills"] == 1


# ---------------------------------------------------------------- D+1 防偷看

def test_day_d_value_trades_no_earlier_than_d_plus_one_open(client, monkeypatch):
    times = _install(monkeypatch, days=8, fg={4: 3})
    body = _post(client, days=8)
    assert body["result_status"] == "success", body
    opened = [t["opened_at"] for t in body["trades"]]
    assert opened == [times[5]]  # D = 第 4 天，成交在 D+1 开盘
    assert times[4] not in opened and min(opened) >= times[4] + DAY


def test_decision_reads_only_rows_available_at_bar_close():
    built = provider._build_fear_greed_scale_in(BASE)
    bars = [provider.LedgerBar(open_time=START + i * DAY, close_time=START + (i + 1) * DAY,
                               open=D(100), close=D(100)) for i in range(4)]
    series = [{"ts": START + i * DAY, "value": "5" if i == 1 else "50",
               "available_at": START + (i + 1) * DAY, "revision": "r"} for i in range(4)]
    signal, _ = built["scale_in_out"]["signal_factory"](bars, series=series)
    assert [signal(i) for i in range(3)] == ["hold", ("buy", D(100)), "hold"]


def test_non_utc_aligned_daily_bars_use_previous_complete_day(client, monkeypatch):
    # 交易所日线 16:00 UTC 开盘（UTC+8 对齐）：收盘在 D 16:00 的那根只能读 D-1 的值。
    times = _install(monkeypatch, days=6, fg={2: 4}, offset=-8 * 3600)
    body = _post(client, days=6)
    assert body["result_status"] == "success", body
    # 第 2 天的值 available_at = 第 3 天 00:00，首个收盘 >= 它的 K 线收盘在第 3 天 16:00，成交在其后一根开盘。
    assert [t["opened_at"] for t in body["trades"]] == [times[4]]
    assert times[4] >= START + 3 * DAY


# ---------------------------------------------------------------- 缺口 fail-closed

@pytest.mark.parametrize("drop", [(3,), (0,), (5,)], ids=["middle", "first_day", "last_day"])
def test_series_gap_fails_whole_run(client, monkeypatch, drop):
    _install(monkeypatch, days=6, fg={1: 5}, drop=drop)
    body = _post(client, days=6)
    assert (body["result_status"], body["error_type"]) == ("failed", "TIME_DATA_GAP"), body
    assert body["limitations"]["reason"] == "fear_greed_data_gap"
    assert body["limitations"]["required_first_date"] == "2024-01-01"
    assert body["limitations"]["required_last_date"] == "2024-01-06"
    assert "fear_greed_index" in body["error_message"]


# ---------------------------------------------------------------- 最早日期

def test_window_before_earliest_archive_is_rejected_before_fetch(client, monkeypatch):
    calls = []
    start = provider.FEAR_GREED_EARLIEST_TS - 3 * DAY
    _install(monkeypatch, days=10, fg={}, start=start, calls=calls)
    fetched = []
    monkeypatch.setattr(provider, "_fetch_ohlcv", lambda *a: fetched.append(a))
    body = _post(client, days=10, start=start)
    assert (body["result_status"], body["error_type"]) == ("failed", "INSUFFICIENT_DATA")
    assert body["limitations"]["reason"] == "fear_greed_history_unavailable"
    assert body["limitations"]["earliest_available_date"] == "2018-02-01"
    assert "2018-02-01" in body["error_message"]
    assert fetched == [] and calls == []


def test_window_starting_on_earliest_archive_day_runs(client, monkeypatch):
    start = provider.FEAR_GREED_EARLIEST_TS
    _install(monkeypatch, days=5, fg={1: 5}, start=start)
    body = _post(client, days=5, start=start)
    assert body["result_status"] == "success", body
    assert body["assumptions"]["fear_greed_series"]["first_date"] == "2018-02-01"


def test_runner_backstops_earliest_date_when_bars_start_before_it(monkeypatch):
    # 预检之后的兜底：K 线（非 UTC 对齐）让决策要读的首日早于存档首日 → 同一拒因。
    monkeypatch.setattr(provider, "_fetch_artifact_metric_chunk", lambda **k: pytest.fail("fetched"))
    built = provider._build_fear_greed_scale_in(BASE)
    df = pd.DataFrame(dict(Open=[100.0] * 3, Close=[100.0] * 3, High=[101.0] * 3, Low=[99.0] * 3,
                           Volume=[1] * 3),
                      index=pd.to_datetime([provider.FEAR_GREED_EARLIEST_TS - 8 * 3600 + i * DAY
                                            for i in range(3)], unit="s"))
    monkeypatch.setattr(provider, "_fetch_template_warmup", lambda *a: df.iloc[:1])
    response = provider._run_scale_in_out_backtest(
        body={}, run_id="fg_backstop", built=built, df=df, symbol="BTCUSDT", market="spot", timeframe="1d",
        start_at=provider.FEAR_GREED_EARLIEST_TS - 8 * 3600, end_at=provider.FEAR_GREED_EARLIEST_TS + 3 * DAY,
        initial_capital=D(10000), fee_bps=D(0), slippage_bps=D(0), exchange_id="binance")
    body = json.loads(response.body)
    assert (body["error_type"], body["limitations"]["reason"]) == (
        "INSUFFICIENT_DATA", "fear_greed_history_unavailable")


# ---------------------------------------------------------------- 参数校验 / 市场 / 周期

def test_builder_rejects_sell_not_above_buy():
    for buy, sell in ((40, 40), (45, 40)):
        with pytest.raises(ValueError, match="sell_threshold > buy_threshold"):
            provider._build_fear_greed_scale_in({**BASE, "buy_threshold": buy, "sell_threshold": sell})


@pytest.mark.parametrize("bad", [
    {"buy_notional": None}, {"buy_notional": 0}, {"buy_notional": True},
    {"buy_threshold": 0}, {"buy_threshold": 50}, {"sell_threshold": 50}, {"sell_threshold": 100},
    {"buy_threshold": 49, "sell_threshold": 49},
    {"max_lots": 0}, {"max_lots": 21}, {"max_lots": 1.5}, {"unknown_key": 1},
    {"time_layer_enabled": True}, {"max_holding_bars": 3},
])
def test_invalid_params_over_http(client, monkeypatch, bad):
    _install(monkeypatch, days=5, fg={})
    params = {k: v for k, v in {**BASE, **bad}.items() if v is not None}
    body = _post(client, days=5, params=params)
    assert (body["result_status"], body["error_type"]) == ("failed", "INVALID_PARAMS"), body


def test_defaults_and_edge_values_accepted():
    built = provider._build_fear_greed_scale_in(BASE)
    assert built["min_bars"] == 1 and built["scale_in_out"]["external_series"] is provider.FEAR_GREED_SERIES
    provider._build_fear_greed_scale_in({**BASE, "buy_threshold": 49, "sell_threshold": 51, "max_lots": 20})
    provider._build_fear_greed_scale_in({**BASE, "buy_threshold": 1, "sell_threshold": 99, "max_lots": 1})


@pytest.mark.parametrize("market,timeframe,params,error_type,needle", [
    ("futures", "1d", BASE, "INVALID_PARAMS", "spot market only"),
    ("spot", "4h", BASE, "TIMEFRAME_UNSUPPORTED", "['1d']"),
    ("spot", "1d", {**BASE, "stop_loss_pct": 5}, "INVALID_PARAMS", "stop_loss_pct"),
    ("spot", "1d", {**BASE, "position_size_notional": 500}, "INVALID_PARAMS", "position_size_notional"),
    ("spot", "1d", {**BASE, "position_size_risk_pct": 1}, "INVALID_PARAMS", "buy_notional"),
])
def test_spot_1d_only_and_no_fixed_risk(client, monkeypatch, market, timeframe, params, error_type, needle):
    _install(monkeypatch, days=5, fg={})
    body = _post(client, days=5, params=params, market=market, timeframe=timeframe)
    assert (body["result_status"], body["error_type"]) == ("failed", error_type), body
    assert needle in body["error_message"]


# ---------------------------------------------------------------- assumptions 数据区间

def test_assumptions_report_series_window_and_available_at_rule(client, monkeypatch):
    calls = []
    _install(monkeypatch, days=7, fg={2: 5}, calls=calls)
    body = _post(client, days=7)
    series = body["assumptions"]["fear_greed_series"]
    assert {k: series[k] for k in ("symbol", "exchange", "metric", "interval")} == dict(
        zip(("symbol", "exchange", "metric", "interval"), LABELS))
    assert (series["first_date"], series["last_date"], series["count"]) == ("2024-01-01", "2024-01-07", 7)
    assert (series["first_ts"], series["last_ts"]) == (START, START + 6 * DAY)
    assert series["earliest_available_date"] == "2018-02-01" and series["gap_policy"] == "fail_closed"
    assert "available_at = ts + 86400s" in series["available_at_rule"] and "D+1 open" in series["available_at_rule"]
    assert series["revision"].startswith("sha256:") or len(series["revision"]) >= 32
    # 只按决策需要的天数取，标签原样（不 .title()、不去 USDT）
    assert calls == [dict(symbol="MARKET", metric="fear_greed_index", interval="1d", exchange="alternative_me",
                          start_at=START, end_at=START + 7 * DAY - 1)]


def test_catalog_entry():
    spec = provider.TOOL_SPECS[TOOL]
    entry = provider._catalog_tool(TOOL, spec, ["BTCUSDT"])
    assert spec["runner"] == provider.SCALE_IN_OUT_RUNNER
    assert entry["markets"] == ["spot"] and entry["timeframes"] == ["1d"]
    props = entry["param_schema"]["properties"]
    assert props == {
        "buy_threshold": {"type": "number", "default": 20, "minimum": 1, "maximum": 49},
        "sell_threshold": {"type": "number", "default": 80, "minimum": 51, "maximum": 99},
        "buy_notional": {"type": "number", "minimum": 0},
        "max_lots": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
        "exchange": {"type": "string", "default": provider.DEFAULT_EXCHANGE},
    }
    assert not set(props) & (set(provider._FIXED_RISK_PARAM_SCHEMA_PROPERTIES) - {"max_holding_bars"})
    assert TOOL in provider.POSITION_SIZING_UNWIRED_TOOLS and provider.RUNNER_SIZING_ALLOWED_KEYS[TOOL] == frozenset()


# ---------------------------------------------------------------- 通用拉取 + artifact 路径回归

class _FakeOpener:
    def __init__(self, items):
        self.urls, self.items = [], items

    def open(self, request, timeout=None):
        self.urls.append(request.full_url)
        return io.BytesIO(json.dumps({"err_code": 100, "data": {"items": self.items}}).encode())


def _fake_central(monkeypatch, items):
    opener = _FakeOpener(items)
    monkeypatch.setattr(provider, "CENTRAL_MARKET_DATA_URL", "https://central.test/v1/internal/market-data")
    monkeypatch.setattr(provider, "CENTRAL_MARKET_DATA_TOKEN", "t")
    monkeypatch.setattr(provider, "_CENTRAL_HTTP_OPENER", opener)
    return opener


def test_generic_series_sends_labels_verbatim(monkeypatch):
    opener = _fake_central(monkeypatch, [{"ts": START, "value": 12}, {"ts": START + DAY, "value": "34"}])
    rows = provider._fetch_metric_series(symbol="MARKET", exchange="alternative_me", metric="fear_greed_index",
                                         interval="1d", start_at=START, end_at=START + 2 * DAY)
    assert opener.urls == [
        "https://central.test/v1/internal/market-data/metrics?symbol=MARKET&metric=fear_greed_index"
        f"&interval=1d&exchange=alternative_me&start_ts={START}&end_ts={START + 2 * DAY - 1}&limit=5000"]
    assert [(r["ts"], r["value"], r["available_at"]) for r in rows] == [
        (START, "12", START + DAY), (START + DAY, "34", START + 2 * DAY)]
    assert len({r["revision"] for r in rows}) == 1


def test_generic_series_gap_and_custom_path(monkeypatch):
    _fake_central(monkeypatch, [{"ts": START, "value": 1}, {"ts": START + 2 * DAY, "value": 2}])
    with pytest.raises(StrategyContractError) as caught:
        provider._fetch_metric_series(symbol="MARKET", exchange="alternative_me", metric="fear_greed_index",
                                      interval="1d", start_at=START, end_at=START + 3 * DAY, path="$.x")
    assert (caught.value.code, caught.value.path) == (ERR_COVERAGE_INCOMPLETE, "$.x")


@pytest.mark.parametrize("exchange,expected_exchange", [("binance", "Binance"), ("all", "AGGREGATED")])
def test_artifact_feature_fetch_request_and_rows_unchanged(monkeypatch, exchange, expected_exchange):
    """回归：artifact 路径的请求 URL 与产出行和 P1 之前逐字节一致（BTCUSDT→BTC、exchange .title()）。"""
    opener = _fake_central(monkeypatch, [{"ts": 0, "value": "1.50"}, {"ts": DAY, "value": 2}])
    rows = provider._fetch_artifact_features(
        {"stream_id": "coinglass.futures_cvd.1d", "exchange": exchange, "interval": "1d"},
        {"features": [{"source_stream": "coinglass.futures_cvd", "interval": "1d"}]},
        "btcusdt", 0, 2 * DAY,
    )
    assert opener.urls == [
        "https://central.test/v1/internal/market-data/metrics?symbol=BTC&metric=futures_cvd&interval=1d"
        f"&exchange={expected_exchange}&start_ts=0&end_ts={2 * DAY - 1}&limit=5000"]
    revision = provider.canonical_json_sha256([{"ts": 0, "value": "1.5"}, {"ts": DAY, "value": "2"}])
    assert rows == [
        {"ts": 0, "value": "1.5", "available_at": DAY, "revision": revision},
        {"ts": DAY, "value": "2", "available_at": 2 * DAY, "revision": revision},
    ]
