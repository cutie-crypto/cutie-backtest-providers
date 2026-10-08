"""R1-T4：EMA 趋势过滤 + RSI 回调模板（local.backtesting_py.ema_rsi_pullback）。

`tests/fixtures/ema_rsi_pullback_golden.json` 是逐 bar 手算 golden（ema_period=10 /
rsi_period=5 的小参数）：expected_ema / expected_rsi / expected_dipped 逐 bar，
expected_entries / expected_exits 为信号 bar 序号（成交在信号 bar + 1 根开盘）。
本文件用独立的纯 Python 循环复算，不 import 被测的 build / 策略类；另用真引擎跑一遍
确认执行侧偏移恰好一根。
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import pytest
from backtesting import Backtest

from cutie_backtesting_provider import (
    TOOL_SPECS,
    _build_ema_rsi_pullback,
    _rsi_series,
    _validate_params_against_schema,
)

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "ema_rsi_pullback_golden.json"
TOOL = "local.backtesting_py.ema_rsi_pullback"


def _load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text())


def _frame(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        dict(
            Open=closes,
            High=[v + 1 for v in closes],
            Low=[v - 1 for v in closes],
            Close=closes,
            Volume=[1] * len(closes),
        ),
        index=pd.date_range("2026-01-01", periods=len(closes), freq="h"),
    )


def _hand_ema(closes, n):
    a = 2.0 / (n + 1)
    out = [float(closes[0])]
    for x in closes[1:]:
        out.append((1 - a) * out[-1] + a * x)
    return out


def _hand_rsi(closes, n):
    a = 1.0 / n
    out = [50.0]
    g = l = None
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gg, ll = max(d, 0.0), max(-d, 0.0)
        if g is None:
            g, l = gg, ll
        else:
            g = (1 - a) * g + a * gg
            l = (1 - a) * l + a * ll
        if l == 0:
            out.append(100.0 if g > 0 else 50.0)
        else:
            out.append(100 - 100 / (1 + g / l))
    return out


def _hand_machine(closes, ema_period, rsi_period, rsi_entry, rsi_exit):
    ema = _hand_ema(closes, ema_period)
    rsi = _hand_rsi(closes, rsi_period)
    min_bars = max(ema_period, rsi_period + 1)
    dipped, held = False, False
    dl, entries, exits, reasons = [], [], [], []
    for i, close in enumerate(closes):
        if i + 1 < min_bars:
            dl.append(False)
            continue
        if rsi[i] < rsi_entry:
            dipped = True
        if held:
            if rsi[i] > rsi_exit or close < ema[i]:
                reasons.append("rsi" if rsi[i] > rsi_exit else "ema")
                exits.append(i)
                held = False
        elif dipped and rsi[i] > rsi_entry and close > ema[i]:
            entries.append(i)
            held = True
            dipped = False
        dl.append(dipped)
    return ema, rsi, dl, entries, exits, reasons


def _fx_params(fx):
    return fx["ema_period"], fx["rsi_period"], fx["rsi_entry"], fx["rsi_exit"]


def _engine(closes, **params):
    built = _build_ema_rsi_pullback(params)
    return Backtest(_frame(closes), built["strategy"], cash=100000, finalize_trades=False).run()


# ---------------------------------------------------------------------------
# 1. Golden.
# ---------------------------------------------------------------------------


def test_golden_indicator_values_match_hand_formula():
    fx = _load_fixture()
    ema, rsi, dl, *_ = _hand_machine(fx["closes"], *_fx_params(fx))
    assert len(ema) == len(fx["expected_ema"]) == len(fx["closes"])
    for i in range(len(ema)):
        assert math.isclose(ema[i], fx["expected_ema"][i], abs_tol=1e-8), i
        assert math.isclose(rsi[i], fx["expected_rsi"][i], abs_tol=1e-8), i
    assert dl == fx["expected_dipped"]


def test_golden_entries_exits_and_reasons():
    fx = _load_fixture()
    *_, entries, exits, reasons = _hand_machine(fx["closes"], *_fx_params(fx))
    assert entries == fx["expected_entries"]
    assert exits == fx["expected_exits"]
    assert reasons == fx["expected_exit_reasons"]
    assert len(entries) >= 2 and len(exits) >= 2
    assert set(reasons) == {"rsi", "ema"}


def test_golden_pullback_rsi_recovered_but_close_below_ema_does_not_enter():
    fx = _load_fixture()
    closes = fx["closes"]
    # bars 32/33: RSI back above entry, Close <= EMA -> no entry, dipped stays True.
    for i in (32, 33):
        assert fx["expected_rsi"][i] > fx["rsi_entry"]
        assert closes[i] <= fx["expected_ema"][i]
        assert fx["expected_dipped"][i] is True
        assert i not in fx["expected_entries"]
    assert 34 in fx["expected_entries"]
    assert closes[34] > fx["expected_ema"][34]


def test_golden_matches_real_strategy_engine_execution_offset_by_one():
    fx = _load_fixture()
    result = _engine(
        fx["closes"],
        ema_period=fx["ema_period"],
        rsi_period=fx["rsi_period"],
        rsi_entry=fx["rsi_entry"],
        rsi_exit=fx["rsi_exit"],
    )
    trades = result["_trades"]
    assert list(trades["EntryBar"]) == [i + 1 for i in fx["expected_entries"]]
    assert list(trades["ExitBar"]) == [i + 1 for i in fx["expected_exits"]]
    assert not result["_strategy"].position
    assert all(size > 0 for size in trades["Size"])  # long only


# ---------------------------------------------------------------------------
# 2. Params change trade count (Jessie 9/9: "改参数结果不变").
# ---------------------------------------------------------------------------


def _long_series():
    return [200 + 40 * math.sin(i / 11.0) + 0.1 * i + 6 * math.sin(i / 2.7) for i in range(900)]


def test_changing_params_changes_trade_count_on_real_engine():
    closes = _long_series()
    default = _engine(closes)
    n_default = len(default["_trades"]) + (1 if default["_strategy"].position else 0)
    assert n_default > 0
    # Different parameter sets must not give the same trade count as default.
    counts = {}
    for name, p in {
        "ema50": dict(ema_period=50),
        "entry25_exit60": dict(rsi_entry=25, rsi_exit=60),
    }.items():
        r = _engine(closes, **p)
        counts[name] = len(r["_trades"]) + (1 if r["_strategy"].position else 0)
        assert counts[name] != n_default, (name, counts, n_default)
    # default run also agrees with the independent hand machine
    *_, entries, exits, _ = _hand_machine(closes, 200, 14, 40, 70)
    assert len(default["_trades"]) == len(exits)
    assert list(default["_trades"]["EntryBar"]) == [i + 1 for i in entries[: len(exits)]]


# ---------------------------------------------------------------------------
# 3. Strict-inequality boundaries.
# ---------------------------------------------------------------------------


def test_rsi_equal_entry_neither_sets_dipped_nor_enters():
    closes = [float(v) for v in range(100, 113)] + [108.0, 112.0, 116.0, 120.0]
    rsi = _rsi_series(closes, 5)
    entry = float(rsi[13])  # the only dip bar; RSI == rsi_entry exactly
    assert 20 <= entry <= 50
    flat = _engine(closes, ema_period=10, rsi_period=5, rsi_entry=entry, rsi_exit=90)
    assert len(flat["_trades"]) == 0 and not flat["_strategy"].position
    # control: entry nudged above RSI -> dipped is set and the pullback entry fires on bar 14.
    ctl = _engine(closes, ema_period=10, rsi_period=5, rsi_entry=math.nextafter(entry, 100.0), rsi_exit=90)
    assert ctl["_strategy"].position
    assert ctl["_strategy"].trades[0].entry_bar == 15


def test_close_equal_ema_does_not_enter():
    closes = [100.0, 102.0, 99.0, 98.0, 95.0, 95.0, 92.0, 94.0, 93.0, 93.0]
    ema, rsi, dl, entries, *_ = _hand_machine(closes, 3, 5, 40, 70)
    # bar 7: dipped, RSI > entry, Close == EMA exactly (ema_period=3 -> alpha 0.5, exact floats)
    assert closes[7] == ema[7] and dl[7] and rsi[7] > 40
    assert entries == []
    result = _engine(closes, ema_period=3, rsi_period=5)
    assert len(result["_trades"]) == 0 and not result["_strategy"].position


def test_close_equal_ema_does_not_exit():
    closes = [100.0, 98.0, 95.0, 97.0, 97.0, 95.0, 98.0, 99.0, 98.0, 100.0]
    ema, rsi, _, entries, exits, _ = _hand_machine(closes, 3, 5, 40, 70)
    assert entries == [7] and exits == []
    assert closes[8] == ema[8] and rsi[8] <= 70  # held bar with Close == EMA exactly
    result = _engine(closes, ema_period=3, rsi_period=5)
    assert len(result["_trades"]) == 0
    assert result["_strategy"].position  # still holding


def test_rsi_equal_exit_does_not_exit():
    fx = _load_fixture()
    closes = fx["closes"]
    rsi = _rsi_series(closes, 5)
    exit_level = float(rsi[18])  # held bar (entry signal at 17), RSI == rsi_exit exactly
    assert 55 <= exit_level <= 85
    r = _engine(closes, ema_period=10, rsi_period=5, rsi_entry=40, rsi_exit=exit_level)
    assert list(r["_trades"]["EntryBar"])[0] == 18
    assert list(r["_trades"]["ExitBar"])[0] == 20  # exits at bar 19 (RSI 73 > level), not 18
    ctl = _engine(closes, ema_period=10, rsi_period=5, rsi_entry=40, rsi_exit=math.nextafter(exit_level, 0.0))
    assert list(ctl["_trades"]["ExitBar"])[0] == 19  # signal at bar 18


def test_warmup_bars_never_set_dipped_or_enter():
    # Fewer than min_bars bars: nothing may happen even with a deep dip and rebound.
    closes = [100.0, 90.0, 80.0, 90.0, 100.0, 110.0]
    result = _engine(closes, ema_period=50, rsi_period=14)
    assert len(result["_trades"]) == 0 and not result["_strategy"].position
    assert _build_ema_rsi_pullback(dict(ema_period=50, rsi_period=14))["min_bars"] == 50
    assert _build_ema_rsi_pullback(dict(ema_period=2, rsi_period=14))["min_bars"] == 15


# ---------------------------------------------------------------------------
# 4. Validation.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        dict(rsi_entry=50, rsi_exit=50),
        dict(rsi_entry=60, rsi_exit=55),
        dict(ema_period=1),
        dict(rsi_period=1),
        dict(ema_period="abc"),
    ],
)
def test_invalid_params_rejected(bad):
    with pytest.raises(ValueError, match="INVALID_PARAMS"):
        _build_ema_rsi_pullback(bad)


# ---------------------------------------------------------------------------
# 5. Catalog / schema.
# ---------------------------------------------------------------------------


def test_tool_spec_registered_with_expected_schema():
    spec = TOOL_SPECS[TOOL]
    assert spec["strategy_family"] == "trend"
    assert spec["is_default"] is False
    assert spec["description"].endswith("maps to KOL 'EMA200 过滤 + RSI 回调'.") or "EMA200 过滤 + RSI 回调" in spec["description"]
    props = spec["param_schema_properties"]
    assert props["ema_period"] == {"type": "integer", "default": 200, "minimum": 50, "maximum": 300}
    assert props["rsi_period"] == {"type": "integer", "default": 14, "minimum": 2, "maximum": 100}
    assert props["rsi_entry"] == {"type": "number", "default": 40, "minimum": 20, "maximum": 50}
    assert props["rsi_exit"] == {"type": "number", "default": 70, "minimum": 55, "maximum": 85}
    for key in ("stop_loss_pct", "take_profit_pct", "position_size_pct", "position_size_notional"):
        assert key in props


def test_schema_validation_rejects_out_of_range_params():
    props = TOOL_SPECS[TOOL]["param_schema_properties"]
    assert _validate_params_against_schema({"ema_period": 49}, props) is not None
    assert _validate_params_against_schema({"ema_period": 301}, props) is not None
    assert _validate_params_against_schema({"rsi_entry": 19}, props) is not None
    assert _validate_params_against_schema({"rsi_entry": 51}, props) is not None
    assert _validate_params_against_schema({"rsi_exit": 54}, props) is not None
    assert _validate_params_against_schema({"rsi_exit": 86}, props) is not None
    assert _validate_params_against_schema({"ema_period": 200}, props) is None
