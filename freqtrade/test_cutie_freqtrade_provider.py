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
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import cutie_freqtrade_provider as provider


def _ts(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp())


def _configure_local_provider(monkeypatch, tmp_path: Path, rows: list[list]) -> None:
    userdir = tmp_path / "user_data"
    strategy_dir = userdir / "strategies"
    strategy_dir.mkdir(parents=True)
    (strategy_dir / "SampleStrategy.py").write_text(
        "class SampleStrategy(IStrategy):\n    pass\n",
        encoding="utf-8",
    )
    data_dir = userdir / "data" / "okx"
    data_dir.mkdir(parents=True)
    (data_dir / "BTC_USDT-1h.json").write_text(json.dumps(rows), encoding="utf-8")
    monkeypatch.setattr(provider, "FREQTRADE_USERDIR", userdir)
    monkeypatch.setattr(provider, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(provider, "_get_engine_version", lambda: "2026.1")


def _request_body(start_at: int, end_at: int) -> dict:
    return {
        "backtest": {
            "run_id": "coverage-test",
            "provider_tool_id": "local.freqtrade.sample_strategy",
            "provider_params": {"strategy_name": "SampleStrategy", "exchange": "okx"},
            "symbol": "BTCUSDT",
            "timeframe": "1h",
            "start_at": start_at,
            "end_at": end_at,
            "initial_capital": "10000",
            "fee_bps": "10",
            "slippage_bps": "5",
        }
    }


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


def test_long_freqtrade_diagnostic_preserves_actionable_tail():
    head = "BEGIN_OF_LOG_SHOULD_BE_DROPPED\n" + ("startup noise\n" * 200)
    tail = "No data found. Terminating."

    result = provider._diagnostic_tail(head + tail)

    assert result.startswith("... (truncated; showing last 1000 characters)")
    assert tail in result
    assert "BEGIN_OF_LOG_SHOULD_BE_DROPPED" not in result


def test_stale_local_data_fails_before_freqtrade_with_actionable_coverage(monkeypatch, tmp_path):
    first = _ts("2026-05-01T00:00:00")
    last = _ts("2026-05-29T23:00:00")
    _configure_local_provider(
        monkeypatch,
        tmp_path,
        [[first * 1000, 1, 1, 1, 1, 1], [last * 1000, 1, 1, 1, 1, 1]],
    )

    def should_not_run(*args, **kwargs):
        raise AssertionError("stale coverage must fail before spawning Freqtrade")

    monkeypatch.setattr(provider.subprocess, "run", should_not_run)
    client = TestClient(provider.app)
    response = client.post(
        "/cutie/backtest",
        json=_request_body(_ts("2026-06-01T00:00:00"), _ts("2026-07-01T00:00:00")),
    )
    body = response.json()

    assert response.status_code == 200
    assert body["result_status"] == "failed"
    assert body["error_type"] == "NO_DATA"
    assert body["limitations"]["reason"] == "local_data_outside_coverage"
    assert "2026-05-01 00:00 UTC through 2026-05-29 23:00 UTC" in body["error_message"]
    assert "freqtrade download-data --exchange okx --pairs BTC/USDT --timeframes 1h" in body["error_message"]
    coverage = body["raw_report"]["market_data_coverage"]
    assert coverage == {
        "status": "outside_coverage",
        "requested_start_at": _ts("2026-06-01T00:00:00"),
        "requested_end_at": _ts("2026-07-01T00:00:00"),
        "actual_start_at": first,
        "actual_last_open_at": last,
        "actual_end_at": last + 3600,
        "data_format": "json",
    }
    assert str(tmp_path) not in json.dumps(body)


def test_exact_timeframe_file_is_required_even_when_pair_has_other_data(monkeypatch, tmp_path):
    first = _ts("2026-05-01T00:00:00")
    userdir = tmp_path / "user_data"
    strategy_dir = userdir / "strategies"
    strategy_dir.mkdir(parents=True)
    (strategy_dir / "SampleStrategy.py").write_text(
        "class SampleStrategy(IStrategy):\n    pass\n",
        encoding="utf-8",
    )
    data_dir = userdir / "data" / "okx"
    data_dir.mkdir(parents=True)
    (data_dir / "BTC_USDT-4h.json").write_text(
        json.dumps([[first * 1000, 1, 1, 1, 1, 1]]),
        encoding="utf-8",
    )
    monkeypatch.setattr(provider, "FREQTRADE_USERDIR", userdir)
    monkeypatch.setattr(provider, "_get_engine_version", lambda: "2026.1")
    monkeypatch.setattr(
        provider.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )

    body = TestClient(provider.app).post(
        "/cutie/backtest",
        json=_request_body(first, first + 3600),
    ).json()

    assert body["error_type"] == "NO_DATA"
    assert body["limitations"]["reason"] == "local_data_missing"
    assert "BTC/USDT 1h" in body["error_message"]


def test_freqtrade_exit_error_response_keeps_tail_after_coverage_passes(monkeypatch, tmp_path):
    first = _ts("2026-05-01T00:00:00")
    last = _ts("2026-05-01T23:00:00")
    _configure_local_provider(
        monkeypatch,
        tmp_path,
        [[first * 1000, 1, 1, 1, 1, 1], [last * 1000, 1, 1, 1, 1, 1]],
    )
    stderr = "BEGIN_OF_LOG_SHOULD_BE_DROPPED\n" + ("startup noise\n" * 200) + "No data found. Terminating."
    observed_config = {}

    def fail_after_reading_config(command, **kwargs):
        config_path = Path(command[command.index("--config") + 1])
        observed_config.update(json.loads(config_path.read_text(encoding="utf-8")))
        return SimpleNamespace(returncode=2, stdout="", stderr=stderr)

    monkeypatch.setattr(provider.subprocess, "run", fail_after_reading_config)

    body = TestClient(provider.app).post(
        "/cutie/backtest",
        json=_request_body(first, last + 3600),
    ).json()

    assert body["error_type"] == "NO_DATA"
    assert body["limitations"]["reason"] == "freqtrade_no_data"
    assert "No data found. Terminating." in body["error_message"]
    assert "BEGIN_OF_LOG_SHOULD_BE_DROPPED" not in body["error_message"]
    assert observed_config["dataformat_ohlcv"] == "json"


# ---------------------------------------------------------------------------
# JES-T15: 日线 1080 天 / 日内 365 天的分区上限
# ---------------------------------------------------------------------------

DAY = 24 * 60 * 60


def _body(timeframe: str, start_at: int, end_at: int) -> dict:
    body = _request_body(start_at, end_at)
    body["backtest"]["timeframe"] = timeframe
    return body


def _configure_local_data(monkeypatch, tmp_path: Path, timeframe: str, start_at: int, end_at: int) -> None:
    """写出一个恰好覆盖 [start_at, end_at) 的时间帧文件（只含首末两根）。"""
    userdir = tmp_path / "user_data"
    strategy_dir = userdir / "strategies"
    strategy_dir.mkdir(parents=True)
    (strategy_dir / "SampleStrategy.py").write_text(
        "class SampleStrategy(IStrategy):\n    pass\n",
        encoding="utf-8",
    )
    data_dir = userdir / "data" / "okx"
    data_dir.mkdir(parents=True)
    step = provider.TIMEFRAME_SECONDS[timeframe]
    rows = [
        [start_at * 1000, 1, 1, 1, 1, 1],
        [(end_at - step) * 1000, 1, 1, 1, 1, 1],
    ]
    (data_dir / f"BTC_USDT-{timeframe}.json").write_text(json.dumps(rows), encoding="utf-8")
    monkeypatch.setattr(provider, "FREQTRADE_USERDIR", userdir)
    monkeypatch.setattr(provider, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(provider, "_get_engine_version", lambda: "2026.1")


def _spy_freqtrade(monkeypatch) -> list[list]:
    spawned: list[list] = []

    def record(command, **kwargs):
        spawned.append(command)
        return SimpleNamespace(returncode=2, stdout="", stderr="No data found. Terminating.")

    monkeypatch.setattr(provider.subprocess, "run", record)
    return spawned


def test_execution_range_freqtrade_catalog_advertises_1080():
    """目录里的 max_range_days 与 artifact provider 的日线上限对齐（1080）。"""
    assert provider.EXECUTION_MAX_RANGE_DAYS == 1080


def test_execution_range_freqtrade_1d_1080_reaches_freqtrade(monkeypatch, tmp_path):
    """1d、1080 天：没有区间闸门，覆盖够就直接进 freqtrade。

    修前也绿——freqtrade 的入口从来只有本地数据覆盖检查，没有按区间的拒绝。
    这条钉的是「1080 天上限放开后，日线 1080 天不会被自家校验误拒」。
    """
    start = _ts("2026-01-01T00:00:00")
    end = start + 1080 * DAY
    _configure_local_data(monkeypatch, tmp_path, "1d", start, end)
    spawned = _spy_freqtrade(monkeypatch)

    body = TestClient(provider.app).post("/cutie/backtest", json=_body("1d", start, end)).json()

    assert spawned, "1080 天日线必须一路走到 freqtrade，而不是被区间校验挡下"
    assert body["error_type"] == "NO_DATA"
    assert body["limitations"]["reason"] == "freqtrade_no_data"


def test_execution_range_freqtrade_1h_366_has_no_range_gate(monkeypatch, tmp_path):
    """1h、366 天：本 provider 没有区间闸门，超 365 天照旧放行到 freqtrade。

    JES-T15 的按周期上限只加在 artifact provider 的统一校验入口
    （strategy_execution.validate_execution_request）；freqtrade 是独立进程、
    既有代码只有本地数据覆盖检查（_inspect_local_data_coverage），没有可复用的
    「区间超限」错误路径。给它新造一个闸门会改变既有错误契约、并可能挡掉
    现在合法的长区间日内回测，故本仓只改目录声明（1080），不动校验行为。
    """
    start = _ts("2026-01-01T00:00:00")
    end = start + 366 * DAY
    _configure_local_data(monkeypatch, tmp_path, "1h", start, end)
    spawned = _spy_freqtrade(monkeypatch)

    body = TestClient(provider.app).post("/cutie/backtest", json=_body("1h", start, end)).json()

    assert spawned
    assert body["limitations"]["reason"] == "freqtrade_no_data"
