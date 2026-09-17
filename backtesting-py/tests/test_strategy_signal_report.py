from decimal import Decimal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import pandas as pd
from fastapi.testclient import TestClient
from backtesting import Backtest
import cutie_backtesting_provider as provider
from canonical_json import canonical_json_sha256


@pytest.mark.parametrize("outcome", ["unfilled", "sl", "tp", "rule_exit"])
@pytest.mark.parametrize("missing_observation", [False, True])
def test_http_signal_report_uses_independent_observation_replay(
    monkeypatch, tmp_path, missing_observation, outcome
):
    closes = [100, 101, 100, 99, 110, 112, 114, 90] + [90] * 42
    df = pd.DataFrame(
        {
            "Open": closes,
            "High": [v + 1 for v in closes],
            "Low": [v - 1 for v in closes],
            "Close": closes,
            "Volume": [1] * len(closes),
        },
        index=pd.to_datetime([i * 300 for i in range(len(closes))], unit="s", utc=True),
    )
    if outcome != "unfilled":
        df.iloc[6, df.columns.get_loc("Low")] = 109  # entry after creation bar
    if outcome == "tp":
        df.iloc[7, df.columns.get_loc("High")] = 125  # existing TP preference for ambiguous bar
    calls = []

    def fetch(exchange, market, symbol, timeframe, start, end):
        calls.append((timeframe, start, end))
        result = df.loc[
            (df.index >= pd.to_datetime(start, unit="s", utc=True))
            & (df.index < pd.to_datetime(end, unit="s", utc=True))
        ].copy()
        return result.iloc[:-1] if missing_observation and len(calls) == 3 else result

    monkeypatch.setattr(provider, "_fetch_ohlcv", fetch)
    monkeypatch.setattr(provider, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(Backtest, "plot", lambda *args, **kwargs: None)
    request = {
        "backtest": {
            "run_id": "signal_replay",
            "provider_tool_id": "local.backtesting_py.ema_cross",
            "provider_params": {"ema_fast": 2, "ema_slow": 3, "exchange": "okx"},
            "symbol": "BTCUSDT",
            "market": "spot",
            "timeframe": "5m",
            "start_at": 1200,
            "end_at": 15000,
            "initial_capital": "10000",
            "fee_bps": "10",
            "slippage_bps": "5",
            "risk_policy": {
                "schema": "cutie.strategy_risk_policy.v1",
                "direction": "long",
                "leverage": 1,
            },
            "signal_execution": {
                "execution_policy": {
                    "schema": "cutie.strategy_signal_execution.v1",
                    "reference_price": "signal_bar_close",
                    "entry_mode": "limit_only",
                    "observation_timeframe": "5m",
                    "evaluation_lag_seconds": 30,
                    "indicator_history_bars": 4,
                    "sl_tp_rule": {
                        "stop_loss": {"type": "fixed_pct", "pct": "5"},
                        "take_profit": {"type": "fixed_pct", "pct": "10"},
                    },
                },
                "cycle_limits": {
                    "daily_limit": 10,
                    "author_daily_limit": 10,
                    "cooldown_seconds": 0,
                    "day_offset_seconds": 0,
                },
            },
        }
    }
    if outcome == "rule_exit":
        request["backtest"]["signal_execution"]["execution_policy"]["sl_tp_rule"] = {
            "stop_loss": {"type": "fixed_pct", "pct": "50"},
            "take_profit": {"type": "fixed_pct", "pct": "200"},
        }
    response = TestClient(provider.app).post("/cutie/backtest", json=request)
    body = response.json()
    if missing_observation:
        assert body["result_status"] == "failed", body
        assert "observation evidence does not cover" in body["error_message"]
        return
    assert body["result_status"] == "success", body
    report = body["raw_report"]["strategy_signal_result"]
    assert "strategy_risk_result" not in body["raw_report"]
    assert report["sha256"] == canonical_json_sha256(
        {k: v for k, v in report.items() if k != "sha256"}
    )
    assert calls == [("5m", 1200, 15000), ("5m", 0, 15000), ("5m", 1200, 15000)]
    assert report["intents"][0]["at"] == 1530
    settlements = report["replay"]["settlements"]
    if outcome == "unfilled":
        assert settlements == []  # reference was never an actual entry
    else:
        assert len(settlements) == 1
        assert (Decimal(settlements[0]["net_return"]) > 0) == (outcome == "tp")
        assert settlements[0]["closed_at"] == (2430 if outcome == "rule_exit" else 2399)
        assert report["replay"]["open_signal"] is None
    assert report["verification"] == "provider_reported"
