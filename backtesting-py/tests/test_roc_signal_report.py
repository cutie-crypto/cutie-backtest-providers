"""Codex review 返修（cb9f491..85ad1cc，P1 半支持）：`strategy_signal_report.build_signal_report`
的 evaluator 白名单此前没有 "roc"，走信号复盘这条路会硬 `ValueError("signal evaluator is
not supported")`。这里直接调用 `build_signal_report`（不经 FastAPI/TestClient），用
`tests/fixtures/roc_golden.json` 的 40 根收盘价构造 `fetch_ohlcv` 假数据，断言产出的入场
intent 落在 golden 的 `expected_entries[0]`（bar 14）那一根。
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from strategy_signal_report import build_signal_report

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "roc_golden.json"
STEP = 300  # 5m


def _load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text())


def _make_fetch_ohlcv(closes: list[float]):
    df = pd.DataFrame(
        {
            "Open": closes,
            "High": [v + 1 for v in closes],
            "Low": [v - 1 for v in closes],
            "Close": closes,
            "Volume": [1] * len(closes),
        },
        index=pd.to_datetime([i * STEP for i in range(len(closes))], unit="s", utc=True),
    )

    def fetch(exchange, market, symbol, timeframe, start, end):
        return df.loc[
            (df.index >= pd.to_datetime(start, unit="s", utc=True))
            & (df.index < pd.to_datetime(end, unit="s", utc=True))
        ].copy()

    return fetch


def _execution_policy(indicator_history_bars: int) -> dict:
    return {
        "schema": "cutie.strategy_signal_execution.v1",
        "reference_price": "signal_bar_close",
        "entry_mode": "limit_only",
        "observation_timeframe": "5m",
        "evaluation_lag_seconds": 30,
        "indicator_history_bars": indicator_history_bars,
        "sl_tp_rule": {
            "stop_loss": {"type": "fixed_pct", "pct": "50"},
            "take_profit": {"type": "fixed_pct", "pct": "200"},
        },
    }


def test_build_signal_report_accepts_roc_and_matches_golden_entry_bar():
    fixture = _load_fixture()
    closes = fixture["closes"]
    roc_period = fixture["roc_period"]
    params = dict(
        roc_period=roc_period,
        entry_threshold=fixture["entry_threshold"],
        exit_threshold=fixture["exit_threshold"],
    )
    history_bars = roc_period + 1  # == required_warmup_bars("roc", params)
    start_at = history_bars * STEP  # first evaluated bar has exactly `history_bars` prehistory
    end_at = len(closes) * STEP + 30

    request = {
        "execution_policy": _execution_policy(history_bars),
        "cycle_limits": {
            "daily_limit": 10,
            "author_daily_limit": 10,
            "cooldown_seconds": 0,
            "day_offset_seconds": 0,
        },
    }
    risk_policy = {
        "schema": "cutie.strategy_risk_policy.v1",
        "direction": "long",
        "leverage": 1,
    }

    result = build_signal_report(
        request=request,
        risk_policy=risk_policy,
        tool_id="local.backtesting_py.roc",
        params=params,
        market="spot",
        symbol="BTCUSDT",
        exchange="okx",
        timeframe="5m",
        step=STEP,
        start_at=start_at,
        end_at=end_at,
        fee_bps=Decimal("10"),
        slippage_bps=Decimal("5"),
        fetch_ohlcv=_make_fetch_ohlcv(closes),
    )

    entry_intents = [i for i in result["intents"] if i["kind"] == "entry"]
    assert entry_intents, "expected at least one entry intent for the roc golden series"
    # golden expected_entries[0] == bar index 14 (0-indexed); bar's close_time is
    # (14+1)*STEP and the intent fires evaluation_lag_seconds (30) after that.
    expected_entry_bar = fixture["expected_entries"][0]
    expected_at = (expected_entry_bar + 1) * STEP + 30
    assert entry_intents[0]["at"] == expected_at
