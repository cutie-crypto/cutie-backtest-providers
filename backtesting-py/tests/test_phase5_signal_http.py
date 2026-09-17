import math
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


CASES = [
    ("macd", {"fast": 3, "slow": 6, "signal": 3}),
    ("bollinger_reversal", {"bb_period": 5, "bb_std": 1}),
    ("bollinger_breakout", {"bb_period": 5, "bb_std": 1}),
]


def run_phase5_http(monkeypatch, tmp_path, name, params, stop_management=None):
    closes = [100.] * 50 + [100 + 10 * math.sin(i * math.pi / 10) for i in range(100)] + [100.] * 30
    df = pd.DataFrame(
        {
            "Open": closes,
            "High": [v + 5 for v in closes],
            "Low": [v - 5 for v in closes],
            "Close": closes,
            "Volume": [1] * len(closes),
        },
        index=pd.to_datetime([i * 300 for i in range(len(closes))], unit="s", utc=True),
    )
    calls = []

    def fetch(exchange, market, symbol, timeframe, start, end):
        calls.append((timeframe, start, end))
        result = df.loc[
            (df.index >= pd.to_datetime(start, unit="s", utc=True))
            & (df.index < pd.to_datetime(end, unit="s", utc=True))
        ].copy()
        return result

    monkeypatch.setattr(provider, "_fetch_ohlcv", fetch)
    monkeypatch.setattr(provider, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(Backtest, "plot", lambda *args, **kwargs: None)
    request = {
        "backtest": {
            "run_id": "signal_replay",
            "provider_tool_id": "local.backtesting_py." + name,
            "provider_params": {**params, "exchange": "okx"},
            "symbol": "BTCUSDT",
            "market": "spot",
            "timeframe": "5m",
            "start_at": 15000,
            "end_at": 54000,
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
                    "indicator_history_bars": 50,
                    "sl_tp_rule": {
                        "stop_loss": {"type": "fixed_pct", "pct": "50"},
                        "take_profit": {"type": "fixed_pct", "pct": "200"},
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
    if stop_management is not None:
        request["backtest"]["signal_execution"]["execution_policy"]["stop_management"] = stop_management
    response = TestClient(provider.app).post("/cutie/backtest", json=request)
    body = response.json()
    assert body["result_status"] == "success", body
    report = body["raw_report"]["strategy_signal_result"]
    assert report["sha256"] == canonical_json_sha256({k: v for k, v in report.items() if k != "sha256"})
    if stop_management is None:
        expected = {
            "macd": [(19800,22830),(25800,28830),(31800,34830),(37800,40830),(43800,46230)],
            "bollinger_reversal": [(17400,20430),(23400,26430)],
            "bollinger_breakout": [(15600,17430),(20400,23430),(26400,29430),(32400,35430),(38400,41430)],
        }[name]
        replay = report["replay"]
        assert [(int(item["signal_id"]), item["closed_at"]) for item in replay["settlements"]] == expected
        for item, (entry_bar, exited_at) in zip(replay["settlements"], expected):
            entry = Decimal(str(closes[entry_bar // 300 - 1]))
            exit_price = Decimal(str(closes[(exited_at - 30) // 300 - 1]))
            net = (exit_price - entry - (entry + exit_price) * Decimal("0.0015")) / entry
            assert Decimal(item["net_return"]) == net
        assert replay["risk_state"]["entries_paused"] == (name == "bollinger_reversal")
        if name == "bollinger_reversal":
            assert replay["risk_state"]["pause_reason"] == "net_drawdown"
            assert any(item["outcome"] == "risk_paused" for item in replay["outcomes"])
        if name == "bollinger_breakout":
            assert replay["open_signal"]["id"] == "44400"
            assert replay["open_signal"]["lifecycle_status"] == "awaiting_entry"
        else:
            assert replay["open_signal"] is None
    run = request["backtest"]
    run["params_snapshot"] = {key: run[key] for key in ("risk_policy", "signal_execution")}
    return {"run": run, "report": report}


@pytest.mark.parametrize("name,params", CASES)
def test_phase5_http_full_sequence(monkeypatch, tmp_path, name, params):
    run_phase5_http(monkeypatch, tmp_path, name, params)
