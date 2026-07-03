"""Regression tests for Freqtrade result parsing None/NaN safety.

Freqtrade's backtest result JSON can contain explicit `null` values for
metric fields (e.g. when a strategy makes zero trades). dict.get(key, default)
only falls back to `default` for *missing* keys -- an explicit `null` still
comes back as None, and arithmetic on None raises TypeError. This mirrors the
_safe_float/_safe_int protection added to the backtesting-py provider in
commit 11a9619.
"""
import json
import os
import tempfile
from pathlib import Path

import pytest

import cutie_freqtrade_provider as provider


def _write_result(strat_result: dict) -> Path:
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump({"strategy": {"SampleStrategy": strat_result}}, f)
    return Path(path)


def test_parse_freqtrade_result_handles_null_metrics():
    """Explicit JSON null metric fields must not raise TypeError."""
    result_path = _write_result({
        "trades": [],
        "total_trades": None,
        "winning_trades": None,
        "losing_trades": None,
        "profit_total": None,
        "profit_total_abs": None,
        "profit_factor": None,
        "max_drawdown": None,
        "max_drawdown_abs": None,
        "backtest_start": None,
        "backtest_end": None,
    })
    try:
        parsed = provider._parse_freqtrade_result(result_path, "BTC/USDT")
    finally:
        result_path.unlink()

    assert parsed["metrics"] == {
        "total_return_pct": 0.0,
        "win_rate_pct": 0,
        "max_drawdown_pct": 0.0,
        "trade_count": 0,
    }


def test_parse_freqtrade_result_handles_null_trade_and_daily_profit():
    """Explicit JSON null in trades[].profit_abs and daily_profit entries must not crash."""
    result_path = _write_result({
        "trades": [
            {
                "pair": "BTC/USDT",
                "is_short": False,
                "profit_abs": None,
                "open_date": None,
                "close_date": None,
            }
        ],
        "total_trades": 1,
        "winning_trades": 0,
        "profit_total": 0,
        "profit_total_abs": 0,
        "max_drawdown": 0,
        "max_drawdown_abs": 0,
        "daily_profit": [["2024-01-01", None, None]],
    })
    try:
        parsed = provider._parse_freqtrade_result(result_path, "BTC/USDT")
    finally:
        result_path.unlink()

    assert parsed["trades"][0]["pnl"] == "0"
    assert parsed["equity_curve"][0]["equity"] == "0"


@pytest.mark.parametrize("value", [None, "not-a-number", float("nan")])
def test_safe_float_defaults_on_bad_input(value):
    assert provider._safe_float({"k": value}, "k", 1.5) == 1.5


@pytest.mark.parametrize("value", [None, "not-a-number"])
def test_safe_int_defaults_on_bad_input(value):
    assert provider._safe_int({"k": value}, "k", 7) == 7


def test_safe_decimal_defaults_on_none():
    from decimal import Decimal

    assert provider._safe_decimal(None) == Decimal("0")


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", float("nan"), float("inf")])
def test_safe_decimal_defaults_on_non_finite(value):
    # Decimal("NaN")/"Infinity" 能成功构造非有限 Decimal，会污染 cumulative 累加
    # （2026-07-03 Codex review P2）
    from decimal import Decimal

    result = provider._safe_decimal(value)
    assert result == Decimal("0")
    assert result.is_finite()
