"""PLOT-0: 0 笔窗口不得因 bt.plot() 崩溃变成 ENGINE_ERROR；plot 其它失败降级为占位报告。"""
import logging
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from backtesting import Backtest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p

TOOL = "local.backtesting_py.breakout"
VALUES = [100, 100, 100, 105, 110, 112, 108, 100, 95, 90, 92, 96, 103, 105, 100, 98, 98, 98]
PARAMS = dict(lookback=2, exit_lookback=1, direction="long")


def frame(values):
    return pd.DataFrame(dict(Open=values, High=[v + 1 for v in values], Low=[v - 1 for v in values],
        Close=values, Volume=[1] * len(values)), index=pd.date_range("2026-01-01", periods=len(values), freq="h"))


def post(data, plot_stub):
    body = dict(backtest=dict(run_id="plot0", provider_tool_id=TOOL, provider_params=PARAMS,
        symbol="BTCUSDT", market="spot", timeframe="1h",
        start_at=int(data.index[0].timestamp()), end_at=int(data.index[-1].timestamp()) + 3600,
        initial_capital="10000", fee_bps="0", slippage_bps="0"))
    with tempfile.TemporaryDirectory(prefix="plot0-") as reports, \
         patch.object(p, "AUTH_TOKEN", ""), patch.object(p, "REPORTS_DIR", Path(reports)), \
         patch.object(p, "_fetch_ohlcv", lambda *a: data.copy()), \
         patch.object(Backtest, "plot", plot_stub):
        result = TestClient(p.app).post("/cutie/backtest", json=body).json()
        report = Path(reports) / "plot0.html"
        text = report.read_text(encoding="utf-8") if report.exists() else None
    return result, text


def test_zero_trade_run_returns_success_without_plot():
    calls = []
    result, text = post(frame([100] * 18), lambda *a, **k: calls.append(1))
    assert result["result_status"] == "success", result
    assert result["metrics"]["trade_count"] == 0
    assert result["limitations"]["no_trades_executed"] is True
    assert result["report_url"] == "reports/plot0.html"
    assert result["report_url_scope"] == "local_machine_only"
    assert text is not None and "no trades executed" in text
    assert calls == []


def test_zero_trade_run_survives_real_plot_crash():
    def boom(*a, **k):
        raise ValueError("Encountered all NA values")
    result, text = post(frame([100] * 18), boom)
    assert result["result_status"] == "success", result
    assert result["metrics"]["trade_count"] == 0


def test_plot_failure_degrades_to_no_report(caplog):
    def boom(*a, **k):
        raise ValueError("plot exploded")
    with caplog.at_level(logging.WARNING, logger="cutie_backtesting_provider"):
        result, text = post(frame(VALUES), boom)
    assert result["result_status"] == "success", result
    assert result["metrics"]["trade_count"] == 2
    assert result["report_url"] == "reports/plot0.html"
    assert text is not None and "report unavailable" in text
    assert any("bt.plot failed" in r.getMessage() and "plot exploded" in r.getMessage() for r in caplog.records)


def test_trades_still_call_plot_once():
    calls = []
    result, text = post(frame(VALUES), lambda *a, **k: calls.append(1))
    assert result["result_status"] == "success", result
    assert result["metrics"]["trade_count"] == 2
    assert calls == [1]
