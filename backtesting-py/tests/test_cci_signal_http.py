import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from decimal import Decimal as D
import pandas as pd
import pytest
from backtesting import Backtest
from fastapi.testclient import TestClient
import cutie_backtesting_provider as provider
import funding_history


def run_cci_short_http(monkeypatch, tmp_path, outcome):
    values = [100,101,99,101,99,100,100,100,100,100] + [90,80,70,60,60,70,80,81,82,110,120,130,140,140,130,120,110,100] + [100] * 32
    df = pd.DataFrame(
        dict(
            Open=values,
            High=[v + 1 for v in values],
            Low=[v - 1 for v in values],
            Close=values,
            Volume=[1] * len(values),
        ),
        index=pd.to_datetime([i * 300 for i in range(len(values))], unit="s", utc=True),
    )

    def fetch(exchange, market, symbol, timeframe, start, end):
        assert (exchange, market, symbol, timeframe) == ("binance", "futures", "BTCUSDT", "5m")
        return df.loc[
            (df.index >= pd.to_datetime(start, unit="s", utc=True))
            & (df.index < pd.to_datetime(end, unit="s", utc=True))
        ].copy()

    funding_calls = []

    def funding(params):
        funding_calls.append(params)
        return [
            {
                "symbol": "BTCUSDT",
                "fundingTime": 5700000,
                "fundingRate": "0.001",
                "markPrice": "100",
            }
        ]

    monkeypatch.setattr(provider, "_fetch_ohlcv", fetch)
    monkeypatch.setattr(funding_history, "_request", funding)
    monkeypatch.setattr(provider, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(Backtest, "plot", lambda *args, **kwargs: None)
    request = {
        "backtest": {
            "run_id": "cci_short",
            "provider_tool_id": "local.backtesting_py.cci_rsi",
            "provider_params": {
                "cci_period": 3,
                "rsi_period": 3,
                "cci_oversold": -80,
                "cci_overbought": 80,
                "rsi_oversold": 30,
                "rsi_overbought": 65,
                "direction": "short",
                "exchange": "binance",
            },
            "symbol": "BTCUSDT",
            "market": "futures",
            "timeframe": "5m",
            "start_at": 3000,
            "end_at": 18000,
            "initial_capital": "10000",
            "fee_bps": "10",
            "slippage_bps": "5",
            "risk_policy": {
                "schema": "cutie.strategy_risk_policy.v1",
                "direction": "short",
                "leverage": 3,
            },
            "signal_execution": {
                "execution_policy": {
                    "schema": "cutie.strategy_signal_execution.v1",
                    "reference_price": "signal_bar_close",
                    "entry_mode": "limit_only",
                    "observation_timeframe": "5m",
                    "evaluation_lag_seconds": 30,
                    "indicator_history_bars": 10,
                    "sl_tp_rule": {
                        "stop_loss": {"type": "fixed_pct", "pct": "5" if outcome == "sl" else "90"},
                        "take_profit": {"type": "fixed_pct", "pct": "50"},
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
    body = TestClient(provider.app).post("/cutie/backtest", json=request).json()
    assert body["result_status"] == "success", body
    report = body["raw_report"]["strategy_signal_result"]
    assert funding_calls
    assert report["funding_evidence"]["rows"][0]["rate"] == "0.001"
    entries = [intent for intent in report["intents"] if intent["kind"] == "entry"]
    assert [(intent["at"], intent["signal"]["entry_price"]) for intent in entries] == [(5130, 80), (6030, 110)]
    assert all(intent["signal"]["direction"] == "short" for intent in entries)
    replay = report["replay"]
    settlements = replay["settlements"]
    assert len(settlements) == 1
    exit_price = D(84) if outcome == "sl" else D(130)
    expected = (D(80) - exit_price - (D(80) + exit_price) * D("0.0015") + D("0.1")) / (D(80) / 3)
    assert D(settlements[0]["net_return"]) == expected
    assert settlements[0]["closed_at"] == (5999 if outcome == "sl" else 7530)
    assert replay["risk_state"]["entries_paused"] == (outcome == "rule_exit")
    assert replay["open_signal"] is None
    assert [event["event_type"] for event in replay["events"]] == (
        ["entry_hit", "sl_hit", "expired_unfilled"] if outcome == "sl" else ["entry_hit"]
    )
    assert replay["outcomes"][2]["outcome"] == ("published" if outcome == "sl" else "open_signal")
    run = request["backtest"]
    run["params_snapshot"] = {key: run[key] for key in ("risk_policy", "signal_execution")}
    return {"run": run, "report": report}


@pytest.mark.parametrize("outcome", ["sl", "rule_exit"])
def test_cci_short_signal_http_accounts_for_costs_funding_and_price_surge(monkeypatch, tmp_path, outcome):
    run_cci_short_http(monkeypatch, tmp_path, outcome)

