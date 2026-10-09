"""Stochastic K/D SMA loop golden, engine timing and strict zone filters."""
import json
import math
import sys
from pathlib import Path

import pandas as pd
import pytest
from backtesting import Backtest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as provider

TOOL = "local.backtesting_py.stoch_oversold_cross"
FIXTURE = Path(__file__).parent / "fixtures/stoch_oversold_cross_golden.json"
CONTRACT = {"stoch_period": ("integer", 14, 5, 30), "stoch_smooth": ("integer", 3, 1, 5),
            "stoch_d": ("integer", 3, 1, 5), "oversold": ("number", 20, 10, 30),
            "overbought": ("number", 80, 70, 90)}


def _hand(highs, lows, closes, p):
    period, smooth, d_period = p["stoch_period"], p["stoch_smooth"], p["stoch_d"]
    out = dict(raw=[], k=[], d=[])
    for i, c in enumerate(closes):
        raw = k = d = None
        if i+1 >= period:
            hi, lo = max(highs[i+1-period:i+1]), min(lows[i+1-period:i+1])
            raw = 50.0 if hi == lo else (c-lo)/(hi-lo)*100
        out["raw"].append(raw)
        window = out["raw"][-smooth:]
        if len(window) == smooth and all(v is not None for v in window):
            k = sum(window)/smooth
        out["k"].append(k)
        window = out["k"][-d_period:]
        if len(window) == d_period and all(v is not None for v in window):
            d = sum(window)/d_period
        out["d"].append(d)
    return out


def _signals(values, p):
    entries, exits, held = [], [], False
    k, d = values["k"], values["d"]
    for i in range(1, len(k)):
        if any(v is None for v in (k[i-1], k[i], d[i-1], d[i])):
            continue
        if not held and k[i-1] <= d[i-1] and k[i] > d[i] and k[i] < p["oversold"]:
            entries.append(i)
            held = True
        elif held and k[i-1] >= d[i-1] and k[i] < d[i] and k[i] > p["overbought"]:
            exits.append(i)
            held = False
    return entries, exits


def _frame(fx):
    closes = fx["closes"]
    return pd.DataFrame(dict(Open=fx.get("opens", closes), High=fx["highs"], Low=fx["lows"], Close=closes,
                             Volume=[1]*len(closes)), index=pd.date_range("2026-01-01", periods=len(closes), freq="h"))


def _run(fx, params=None):
    built = provider._build_stoch_oversold_cross(fx["params"] if params is None else params)
    return Backtest(_frame(fx), built["strategy"], cash=100000, finalize_trades=False).run()


def test_golden_independent_formula_and_signals():
    fx = json.loads(FIXTURE.read_text())
    values = _hand(fx["highs"], fx["lows"], fx["closes"], fx["params"])
    for key, series in values.items():
        for i, want in enumerate(fx["expected_"+key]):
            if want is None:
                assert series[i] is None
            else:
                assert series[i] == pytest.approx(want, abs=1e-9)
    entries, exits = _signals(values, fx["params"])
    assert entries == fx["expected_entries"]
    assert exits == fx["expected_exits"]
    assert len(exits) >= 2


def test_indicator_first_three_valid_values_and_full_series_to_six_decimals():
    fx = json.loads(FIXTURE.read_text())
    assert len(fx["closes"]) >= 40
    expected = _hand(fx["highs"], fx["lows"], fx["closes"], fx["params"])
    actual = provider._stoch_arrays(fx["highs"], fx["lows"], fx["closes"], 14, 3, 3)
    for key in expected:
        valid = [i for i, v in enumerate(expected[key]) if v is not None]
        assert len(valid) >= 3
        assert [actual[key][i] for i in valid[:3]] == pytest.approx([expected[key][i] for i in valid[:3]], abs=1e-6)
        for i, want in enumerate(expected[key]):
            if want is None:
                assert math.isnan(actual[key][i])
            else:
                assert actual[key][i] == pytest.approx(want, abs=1e-6)


def test_golden_real_engine_times_prices_next_open_and_long_only():
    fx = json.loads(FIXTURE.read_text())
    result = _run(fx)
    trades, df = result["_trades"], _frame(fx)
    for side, signals in (("Entry", fx["expected_entries"]), ("Exit", fx["expected_exits"])):
        bars = [i+1 for i in signals[:len(fx["expected_exits"])]]
        assert list(trades[side+"Bar"]) == bars
        assert list(trades[side+"Time"]) == list(df.index[bars])
        assert list(trades[side+"Price"]) == list(df.Open.iloc[bars])
    assert all(trades.Size > 0)
    assert bool(result["_strategy"].position) == (len(fx["expected_entries"]) > len(fx["expected_exits"]))


def _controlled(monkeypatch, entry_k=19, exit_k=81, risk=None):
    count = 40
    k = [10.0]*20 + [entry_k]*5 + [90.0]*5 + [exit_k]*10
    d = [15.0]*25 + [85.0]*5 + [85.0]*10
    monkeypatch.setattr(provider, "_stoch_arrays", lambda *args: dict(k=k,d=d))
    return _run(dict(closes=[100.0]*count,highs=[101.0]*count,lows=[99.0]*count,params=risk or {}))


@pytest.mark.parametrize("entry_k", [20, 21])
def test_k_at_or_above_oversold_cross_does_not_enter(monkeypatch, entry_k):
    result = _controlled(monkeypatch, entry_k=entry_k)
    assert result["_trades"].empty
    assert not result["_strategy"].position


@pytest.mark.parametrize("exit_k", [80, 79])
def test_k_at_or_below_overbought_death_cross_does_not_exit(monkeypatch, exit_k):
    result = _controlled(monkeypatch, exit_k=exit_k)
    assert result["_trades"].empty
    assert result["_strategy"].position


def test_crosses_inside_zones_enter_then_exit(monkeypatch):
    trades = _controlled(monkeypatch)["_trades"]
    assert list(trades.EntryBar) == [21]
    assert list(trades.ExitBar) == [31]


def test_equal_k_d_is_not_a_cross(monkeypatch):
    count = 40
    monkeypatch.setattr(provider, "_stoch_arrays", lambda *args: dict(k=[10.0]*count, d=[10.0]*count))
    result = _run(dict(closes=[100]*count,highs=[101]*count,lows=[99]*count,params={}))
    assert not result["_strategy"].position


def test_zero_high_low_range_raw_is_fifty_and_sma_warmup_preserved():
    actual = provider._stoch_arrays([100]*45,[100]*45,[100]*45,14,3,3)
    for key, first in (("raw",13),("k",15),("d",17)):
        assert all(math.isnan(v) for v in actual[key][:first])
        assert all(v == 50 for v in actual[key][first:])


def test_changing_two_parameter_sets_changes_trade_count():
    fx = json.loads(FIXTURE.read_text())
    counts = [len(_run(fx,p)["_trades"]) for p in ({}, {"oversold":10,"overbought":90}, {"stoch_period":30})]
    assert counts[0] != counts[1]
    assert counts[0] != counts[2]


def test_name_and_warmup():
    built = provider._build_stoch_oversold_cross({})
    assert built["executed_name"] == "Stochastic Oversold Cross (14/3/3, 20/80)"
    assert built["min_bars"] == 19


@pytest.mark.parametrize("key", CONTRACT)
def test_each_parameter_schema_defaults_and_bounds(key):
    typ, default, lo, hi = CONTRACT[key]
    props = provider.TOOL_SPECS[TOOL]["param_schema_properties"]
    assert props[key] == dict(type=typ,default=default,minimum=lo,maximum=hi)
    assert "stop_loss_pct" in props
    for value in (default,lo,hi):
        assert provider._build_stoch_oversold_cross({key:value})["strategy"]


@pytest.mark.parametrize("key", CONTRACT)
@pytest.mark.parametrize("bad", ["low", "high", None, True, "14", float("nan"), float("inf")])
def test_each_parameter_invalid_rejected(key,bad):
    if bad == "low":
        bad = CONTRACT[key][2]-1
    elif bad == "high":
        bad = CONTRACT[key][3]+1
    with pytest.raises(ValueError,match="INVALID_PARAMS"):
        provider._build_stoch_oversold_cross({key:bad})


@pytest.mark.parametrize("key", ["stoch_period", "stoch_smooth", "stoch_d"])
def test_fractional_period_rejected(key):
    with pytest.raises(ValueError,match="INVALID_PARAMS"):
        provider._build_stoch_oversold_cross({key:CONTRACT[key][1]+.5})


@pytest.mark.parametrize("risk,price", [({"stop_loss_pct":.02},98.0),({"take_profit_pct":.03},103.0)])
def test_fixed_risk_mixin_exits_without_a_signal(monkeypatch,risk,price):
    count = 40
    monkeypatch.setattr(provider, "_stoch_arrays", lambda *args: dict(
        k=[10.0]*20 + [19.0]*20, d=[15.0]*count))
    closes = [100.0]*25 + [price]*15
    result = _run(dict(closes=closes,highs=[v+1 for v in closes],lows=[v-1 for v in closes],params=risk))
    trades = result["_trades"]
    assert list(trades.EntryBar) == [21]
    assert list(trades.ExitBar) == [26]
    assert list(trades.ExitPrice) == [price]
