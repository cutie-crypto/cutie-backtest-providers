"""62-1 result.v2（SPEC_验证复核契约.md §2 冻结结构）provider 侧输出测试。

覆盖：
1. trades 公式（long/short、费用/滑点/pnl）——与 TokenBeep 主仓
   cutie-server/tests/test_strategy_backtest_validation.py 的自洽金额数据集
   （entry=100/exit=110/qty=0.5/fee_bps=10/slippage_bps=5 →
   fee=0.105/slippage=0.0525/pnl=4.8425）逐分逐厘核对，跨仓交叉验证公式一致。
2. equity_curve 起点 + 逐笔累计 + 无交易保留初始点。
3. max_drawdown 峰谷比例。
4. K 线 checksum 输入的规范化 + 区间过滤 + 可独立复算。
5. data_manifest.source 命名（中心命中 binance_us/binance_futures，ccxt 回退老实标注）。
6. 端到端 /cutie/backtest：v2 结构完整性（恰好 N 键）+ 公式内部一致性 + checksum 复算。
7. /health.process_fingerprint 格式。
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.parse
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cutie_backtesting_provider as provider  # noqa: E402
from canonical_json import canonical_decimal_str, canonical_json_sha256  # noqa: E402


class _FakeCentralResponse:
    """最小 urlopen response 模拟：中心行情 klines 接口 JSON body。"""

    def __init__(self, body: dict):
        self._body = json.dumps(body).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

# ---------------------------------------------------------------------------
# trades 公式：与 cutie-server test_strategy_backtest_validation.py 的金额数据集交叉验证
# ---------------------------------------------------------------------------

START_AT = 1717002000
CLOSE_TS = START_AT + 3600


def _fake_trade_row(entry_price, exit_price, size, opened_at, closed_at):
    return {
        "Size": size,
        "EntryPrice": entry_price,
        "ExitPrice": exit_price,
        "EntryTime": pd.Timestamp(opened_at, unit="s"),
        "ExitTime": pd.Timestamp(closed_at, unit="s"),
    }


def test_trade_formula_matches_server_golden_case_long():
    """long：entry=100 exit=110 qty=0.5 fee_bps=10 slippage_bps=5 →
    fee=0.105 slippage=0.0525 gross=5 pnl=4.8425（server 侧同一组数字）。
    """
    trades_df = pd.DataFrame([_fake_trade_row(100.0, 110.0, 1, START_AT, CLOSE_TS)])
    trades = provider._build_result_v2_trades(
        trades_df, equity_scale_dec=Decimal("0.5"), fee_bps=Decimal("10"), slippage_bps=Decimal("5")
    )
    assert trades == [
        {
            "seq": 1,
            "opened_at": START_AT,
            "closed_at": CLOSE_TS,
            "side": "long",
            "qty": "0.5",
            "entry_price": "100",
            "exit_price": "110",
            "fee": "0.105",
            "slippage": "0.0525",
            "pnl": "4.8425",
        }
    ]


def test_trade_formula_short_side():
    """short：entry=110 exit=100 qty=0.5 fee_bps=10 slippage_bps=5 →
    gross=(entry-exit)*qty=5，费用/滑点公式与 side 无关（对称），pnl=4.8425。
    """
    trades_df = pd.DataFrame([_fake_trade_row(110.0, 100.0, -1, START_AT, CLOSE_TS)])
    trades = provider._build_result_v2_trades(
        trades_df, equity_scale_dec=Decimal("0.5"), fee_bps=Decimal("10"), slippage_bps=Decimal("5")
    )
    assert len(trades) == 1
    t = trades[0]
    assert t["side"] == "short"
    assert t["entry_price"] == "110"
    assert t["exit_price"] == "100"
    assert t["fee"] == "0.105"
    assert t["slippage"] == "0.0525"
    assert t["pnl"] == "4.8425"


def test_trade_formula_seq_assigned_by_closed_at_order():
    """两笔交易乱序传入，seq 必须按 closed_at 升序重排为 1,2。"""
    later = _fake_trade_row(100.0, 105.0, 1, START_AT + 7200, START_AT + 10800)
    earlier = _fake_trade_row(100.0, 102.0, 1, START_AT, CLOSE_TS)
    trades_df = pd.DataFrame([later, earlier])
    trades = provider._build_result_v2_trades(
        trades_df, equity_scale_dec=Decimal("1"), fee_bps=Decimal("0"), slippage_bps=Decimal("0")
    )
    assert [t["seq"] for t in trades] == [1, 2]
    assert trades[0]["closed_at"] == CLOSE_TS
    assert trades[1]["closed_at"] == START_AT + 10800


def test_trade_formula_zero_fee_slippage():
    trades_df = pd.DataFrame([_fake_trade_row(50.0, 60.0, 2, START_AT, CLOSE_TS)])
    trades = provider._build_result_v2_trades(
        trades_df, equity_scale_dec=Decimal("1"), fee_bps=Decimal("0"), slippage_bps=Decimal("0")
    )
    t = trades[0]
    assert t["qty"] == "2"
    assert t["fee"] == "0"
    assert t["slippage"] == "0"
    # gross = (60-50)*2 = 20
    assert t["pnl"] == "20"


def test_trade_formula_none_or_empty_returns_empty_list():
    assert provider._build_result_v2_trades(None, Decimal("1"), Decimal("1"), Decimal("1")) == []
    assert provider._build_result_v2_trades(pd.DataFrame(), Decimal("1"), Decimal("1"), Decimal("1")) == []


def test_trade_all_ten_keys_exactly():
    trades_df = pd.DataFrame([_fake_trade_row(100.0, 110.0, 1, START_AT, CLOSE_TS)])
    trades = provider._build_result_v2_trades(trades_df, Decimal("1"), Decimal("10"), Decimal("5"))
    assert set(trades[0].keys()) == {
        "seq", "opened_at", "closed_at", "side", "qty",
        "entry_price", "exit_price", "fee", "slippage", "pnl",
    }


# ---------------------------------------------------------------------------
# equity_curve / max_drawdown
# ---------------------------------------------------------------------------


def test_equity_curve_no_trades_keeps_initial_point():
    curve = provider._build_result_v2_equity_curve([], Decimal("10000"), START_AT)
    assert curve == [{"ts": START_AT, "equity": "10000"}]


def test_equity_curve_accumulates_pnl_in_seq_order():
    trades_v2 = [
        {"closed_at": CLOSE_TS, "pnl": "100"},
        {"closed_at": CLOSE_TS + 3600, "pnl": "-30"},
    ]
    curve = provider._build_result_v2_equity_curve(trades_v2, Decimal("10000"), START_AT)
    assert curve == [
        {"ts": START_AT, "equity": "10000"},
        {"ts": CLOSE_TS, "equity": "10100"},
        {"ts": CLOSE_TS + 3600, "equity": "10070"},
    ]


def test_max_drawdown_peak_to_trough():
    # 10000 -> 11000 (peak) -> 9900 (trough, dd = (11000-9900)/11000 = 0.1) -> 10500
    curve = [
        {"ts": 1, "equity": "10000"},
        {"ts": 2, "equity": "11000"},
        {"ts": 3, "equity": "9900"},
        {"ts": 4, "equity": "10500"},
    ]
    dd = provider._result_v2_max_drawdown(curve)
    # dd 本身就是比例：(11000-9900)/11000 = 0.1
    assert dd == Decimal("0.1")


def test_max_drawdown_zero_when_never_below_peak():
    curve = [{"ts": 1, "equity": "10000"}, {"ts": 2, "equity": "10500"}, {"ts": 3, "equity": "11000"}]
    assert provider._result_v2_max_drawdown(curve) == Decimal("0")


# ---------------------------------------------------------------------------
# K 线 checksum 输入
# ---------------------------------------------------------------------------


def _make_kline_df(start_at: int, count: int, step_sec: int = 3600) -> pd.DataFrame:
    rows = []
    idx = []
    price = 100.0
    for i in range(count):
        ts = start_at + i * step_sec
        idx.append(pd.to_datetime(ts * 1000, unit="ms"))
        rows.append([price + i, price + i + 5, price + i - 5, price + i + 1, 10.0 + i])
    df = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=pd.DatetimeIndex(idx))
    return df


def test_kline_rows_to_canonical_shape_and_order():
    df = _make_kline_df(START_AT, 3)
    rows = provider._kline_rows_to_canonical(df, START_AT, START_AT + 2 * 3600)
    assert len(rows) == 3
    assert [r["open_time"] for r in rows] == [START_AT, START_AT + 3600, START_AT + 7200]
    for r in rows:
        assert set(r.keys()) == {"open_time", "open", "high", "low", "close", "volume"}
        for key in ("open", "high", "low", "close", "volume"):
            assert isinstance(r[key], str)
            Decimal(r[key])  # 必须是可解析的规范 Decimal 字符串


def test_kline_rows_to_canonical_filters_outside_window():
    df = _make_kline_df(START_AT, 5)
    # 只声明前 3 根在窗口内
    rows = provider._kline_rows_to_canonical(df, START_AT, START_AT + 2 * 3600)
    assert len(rows) == 3


def test_kline_checksum_independently_recomputable():
    """checksum 必须等于 sha256(canonical_json(手工构造的期望 K 线数组))——独立复算路径。"""
    df = _make_kline_df(START_AT, 2)
    rows = provider._kline_rows_to_canonical(df, START_AT, START_AT + 3600)
    expected_rows = [
        {
            "open_time": START_AT,
            "open": canonical_decimal_str("100.0"),
            "high": canonical_decimal_str("105.0"),
            "low": canonical_decimal_str("95.0"),
            "close": canonical_decimal_str("101.0"),
            "volume": canonical_decimal_str("10.0"),
        },
        {
            "open_time": START_AT + 3600,
            "open": canonical_decimal_str("101.0"),
            "high": canonical_decimal_str("106.0"),
            "low": canonical_decimal_str("96.0"),
            "close": canonical_decimal_str("102.0"),
            "volume": canonical_decimal_str("11.0"),
        },
    ]
    assert rows == expected_rows
    assert canonical_json_sha256(rows) == canonical_json_sha256(expected_rows)


# ---------------------------------------------------------------------------
# data_manifest.source 命名
# ---------------------------------------------------------------------------


def test_data_manifest_source_central_hit_spot():
    df = pd.DataFrame()
    df.attrs["cutie_central_market_data_used"] = True
    assert provider._data_manifest_source("spot", "binance", df) == "binance_us"


def test_data_manifest_source_central_hit_futures():
    df = pd.DataFrame()
    df.attrs["cutie_central_market_data_used"] = True
    assert provider._data_manifest_source("futures", "binance", df) == "binance_futures"


@pytest.mark.parametrize("central_used", [False, None])
def test_data_manifest_source_ccxt_fallback_is_honest(central_used):
    df = pd.DataFrame()
    df.attrs["cutie_central_market_data_used"] = central_used
    assert provider._data_manifest_source("spot", "okx", df) == "ccxt:okx"


# ---------------------------------------------------------------------------
# 端到端 /cutie/backtest：v2 结构完整性 + 公式内部一致性 + checksum 复算
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    return TestClient(provider.app)


def test_backtest_result_v2_end_to_end_structure_and_checksum(client, monkeypatch, tmp_path):
    monkeypatch.setattr(provider, "REPORTS_DIR", tmp_path / "reports")
    from backtesting import Backtest
    monkeypatch.setattr(Backtest, "plot", lambda self, **kwargs: None)

    count = 6
    df = _make_kline_df(START_AT, count)
    # 模拟一段有涨有跌的序列，让 EMA(2)/EMA(3) 交叉能触发至少一笔交易。
    df["Close"] = [100.0, 103.0, 98.0, 106.0, 95.0, 108.0]
    df["Open"] = df["Close"]
    df["High"] = df["Close"] + 2
    df["Low"] = df["Close"] - 2
    df.attrs["cutie_data_source"] = "cutie_central_market_data"
    df.attrs["cutie_central_market_data_used"] = True
    df.attrs["cutie_market_data_cache_hit"] = False

    monkeypatch.setattr(provider, "_fetch_ohlcv", lambda *a, **kw: df)

    end_at = START_AT + (count - 1) * 3600
    fee_bps = "10"
    slippage_bps = "5"

    resp = client.post("/cutie/backtest", json={
        "backtest": {
            "run_id": "v2_struct_1",
            "provider_tool_id": "local.backtesting_py.ema_cross",
            "provider_params": {"ema_fast": 2, "ema_slow": 3, "exchange": "binance"},
            "symbol": "BTCUSDT",
            "market": "spot",
            "timeframe": "1h",
            "start_at": START_AT,
            "end_at": end_at,
            "initial_capital": "10000",
            "fee_bps": fee_bps,
            "slippage_bps": slippage_bps,
        },
    })

    assert resp.status_code == 200
    body = resp.json()
    assert body["result_status"] == "success", body
    assert body["schema_version"] == "cutie.backtest_result.v2"

    # metrics 恰好三键
    assert set(body["metrics"].keys()) == {"total_return", "max_drawdown", "trade_count"}
    assert body["metrics"]["trade_count"] == len(body["trades"])

    # trades 每笔恰好 10 键 + seq 从 1 连续 + 公式内部一致（不管 backtesting.py 具体成交价）
    fee_bps_dec = Decimal(fee_bps)
    slippage_bps_dec = Decimal(slippage_bps)
    for i, trade in enumerate(body["trades"], start=1):
        assert set(trade.keys()) == {
            "seq", "opened_at", "closed_at", "side", "qty",
            "entry_price", "exit_price", "fee", "slippage", "pnl",
        }
        assert trade["seq"] == i
        assert trade["side"] in ("long", "short")
        entry = Decimal(trade["entry_price"])
        exit_ = Decimal(trade["exit_price"])
        qty = Decimal(trade["qty"])
        expected_fee = (entry + exit_) * qty * fee_bps_dec / Decimal(10000)
        expected_slippage = (entry + exit_) * qty * slippage_bps_dec / Decimal(10000)
        assert Decimal(trade["fee"]) == expected_fee
        assert Decimal(trade["slippage"]) == expected_slippage
        if trade["side"] == "long":
            gross = (exit_ - entry) * qty
        else:
            gross = (entry - exit_) * qty
        assert Decimal(trade["pnl"]) == gross - expected_fee - expected_slippage

    # equity_curve：起点 = start_at/initial_capital，逐笔累计
    assert body["equity_curve"][0] == {"ts": START_AT, "equity": "10000"}
    running = Decimal("10000")
    for point, trade in zip(body["equity_curve"][1:], body["trades"]):
        running += Decimal(trade["pnl"])
        assert point["ts"] == trade["closed_at"]
        assert Decimal(point["equity"]) == running

    # metrics.total_return 与 equity_curve 尾点一致
    final_equity = Decimal(body["equity_curve"][-1]["equity"])
    expected_total_return = (final_equity - Decimal("10000")) / Decimal("10000")
    assert Decimal(body["metrics"]["total_return"]) == expected_total_return

    # data_manifest 恰好九键 + 命名 + checksum 独立复算
    manifest = body["data_manifest"]
    assert set(manifest.keys()) == {
        "source", "symbol", "market", "timeframe", "start_at", "end_at",
        "kline_count", "checksum_algo", "checksum",
    }
    assert manifest["source"] == "binance_us"  # central 命中 + spot
    assert manifest["symbol"] == "BTCUSDT"
    assert manifest["market"] == "spot"
    assert manifest["timeframe"] == "1h"
    assert manifest["start_at"] == START_AT
    assert manifest["end_at"] == end_at
    assert manifest["kline_count"] == count
    assert manifest["checksum_algo"] == "sha256"

    expected_rows = [
        {
            "open_time": START_AT + i * 3600,
            "open": canonical_decimal_str(str(df["Open"].iloc[i])),
            "high": canonical_decimal_str(str(df["High"].iloc[i])),
            "low": canonical_decimal_str(str(df["Low"].iloc[i])),
            "close": canonical_decimal_str(str(df["Close"].iloc[i])),
            "volume": canonical_decimal_str(str(df["Volume"].iloc[i])),
        }
        for i in range(count)
    ]
    assert manifest["checksum"] == canonical_json_sha256(expected_rows)

    # raw_report 保留旧展示性指标，顶层不再有
    assert "total_return_pct" not in body["metrics"]
    legacy = body["raw_report"]["legacy_metrics"]
    assert set(legacy.keys()) == {
        "total_return_pct", "win_rate_pct", "max_drawdown_pct", "trade_count", "buy_hold_return_pct",
    }


def test_backtest_result_v2_no_trades_still_has_initial_equity_point(client, monkeypatch, tmp_path):
    """全平走势（无交叉）→ trade_count=0，equity_curve 仍保留初始点。"""
    monkeypatch.setattr(provider, "REPORTS_DIR", tmp_path / "reports")
    from backtesting import Backtest
    monkeypatch.setattr(Backtest, "plot", lambda self, **kwargs: None)

    count = 6
    df = _make_kline_df(START_AT, count)
    df["Close"] = [100.0] * count  # 完全走平，EMA 快慢线永不交叉
    df["Open"] = df["Close"]
    df["High"] = df["Close"]
    df["Low"] = df["Close"]
    df.attrs["cutie_data_source"] = provider.DATA_SOURCE
    df.attrs["cutie_central_market_data_used"] = False
    df.attrs["cutie_market_data_cache_hit"] = False

    monkeypatch.setattr(provider, "_fetch_ohlcv", lambda *a, **kw: df)

    end_at = START_AT + (count - 1) * 3600
    resp = client.post("/cutie/backtest", json={
        "backtest": {
            "run_id": "v2_no_trade_1",
            "provider_tool_id": "local.backtesting_py.ema_cross",
            "provider_params": {"ema_fast": 2, "ema_slow": 3, "exchange": "okx"},
            "symbol": "ETHUSDT",
            "market": "spot",
            "timeframe": "1h",
            "start_at": START_AT,
            "end_at": end_at,
            "initial_capital": "5000",
        },
    })

    assert resp.status_code == 200
    body = resp.json()
    assert body["result_status"] == "success", body
    assert body["trades"] == []
    assert body["equity_curve"] == [{"ts": START_AT, "equity": "5000"}]
    assert body["metrics"] == {"total_return": "0", "max_drawdown": "0", "trade_count": 0}
    assert body["data_manifest"]["source"] == "ccxt:okx"


def test_backtest_futures_central_hit_end_to_end_data_manifest_source(client, monkeypatch, tmp_path):
    """62-1 F1 端到端：market=futures 走真实 central 命中路径（不走 _fetch_ohlcv 捷径），
    验证 _normalize_ohlcv_symbol 的 ":SETTLE" 后缀剥离修复 +
    data_manifest.source == "binance_futures"；ccxt 挂假失败探针确认真的没被调用。
    """
    monkeypatch.setattr(provider, "CENTRAL_MARKET_DATA_URL", "https://server.example.com/v1/internal/market-data")
    monkeypatch.setattr(provider, "CENTRAL_MARKET_DATA_TOKEN", "test-token")
    monkeypatch.setattr(provider, "CACHE_DIR", tmp_path / "cache" / "ohlcv")
    monkeypatch.setattr(provider, "REPORTS_DIR", tmp_path / "reports")
    from backtesting import Backtest
    monkeypatch.setattr(Backtest, "plot", lambda self, **kwargs: None)

    closes = [100.0, 103.0, 98.0, 106.0, 95.0, 108.0]

    def fake_urlopen(request, **_kw):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
        assert qs["market"][0] == "futures"
        assert qs["symbol"][0] == "BTC"  # 裸 symbol，不含 /:SETTLE
        items = [
            {
                "open_time": START_AT + i * 3600,
                "open": c, "high": c + 2, "low": c - 2, "close": c, "volume": 10.0,
            }
            for i, c in enumerate(closes)
        ]
        return _FakeCentralResponse({"err_code": 100, "data": {"available": True, "count": len(items), "items": items}})

    monkeypatch.setattr(provider._CENTRAL_HTTP_OPENER, "open", fake_urlopen)

    def _fail_if_called(*_a, **_kw):
        raise AssertionError("ccxt should not be called when futures central hits")

    fake_ccxt_module = type(sys)("ccxt")
    fake_ccxt_module.binance = _fail_if_called
    monkeypatch.setitem(sys.modules, "ccxt", fake_ccxt_module)

    end_at = START_AT + (len(closes) - 1) * 3600
    resp = client.post("/cutie/backtest", json={
        "backtest": {
            "run_id": "v2_futures_central_1",
            "provider_tool_id": "local.backtesting_py.ema_cross",
            "provider_params": {"ema_fast": 2, "ema_slow": 3, "exchange": "binance"},
            "symbol": "BTCUSDT",
            "market": "futures",
            "timeframe": "1h",
            "start_at": START_AT,
            "end_at": end_at,
            "initial_capital": "10000",
        },
    })

    assert resp.status_code == 200
    body = resp.json()
    assert body["result_status"] == "success", body
    assert body["data_manifest"]["source"] == "binance_futures"
    assert body["data_manifest"]["market"] == "futures"
    assert body["data_manifest"]["kline_count"] == len(closes)


# ---------------------------------------------------------------------------
# /health.process_fingerprint
# ---------------------------------------------------------------------------


def test_health_process_fingerprint_format():
    response = asyncio.run(provider.health())
    body = json.loads(response.body)
    fingerprint = body["process_fingerprint"]
    hostname, pid, started_at = fingerprint.rsplit(":", 2)
    assert hostname  # 非空
    assert pid.isdigit()
    assert started_at.isdigit()
    assert int(pid) > 0
    assert int(started_at) > 0


def test_health_process_fingerprint_stable_within_process():
    a = provider._process_fingerprint()
    b = provider._process_fingerprint()
    assert a == b
