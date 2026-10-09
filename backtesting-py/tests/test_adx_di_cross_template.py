"""ADX/DI: independent loop golden, real fills and strict signal boundaries."""
import json
import math
import sys
from pathlib import Path

import pandas as pd
import pytest
from backtesting import Backtest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as provider

TOOL = "local.backtesting_py.adx_di_cross"
FIXTURE = Path(__file__).parent / "fixtures/adx_di_cross_golden.json"
CONTRACT = {"adx_period": ("integer", 14, 7, 30), "adx_threshold": ("number", 25, 15, 40),
            "adx_exit": ("number", 20, 10, 30)}


def _hand(highs, lows, closes, n):
    atr = plus_rma = minus_rma = adx = 0.0
    out = {k: [] for k in ("adx", "plus_di", "minus_di")}
    alpha = 1 / n
    for i, c in enumerate(closes):
        tr = highs[i] - lows[i]
        up = down = 0.0
        if i:
            tr = max(tr, abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
            up, down = highs[i] - highs[i-1], lows[i-1] - lows[i]
        pdm = up if up > down and up > 0 else 0.0
        mdm = down if down > up and down > 0 else 0.0
        atr = tr if not i else alpha * tr + (1-alpha) * atr
        plus_rma = alpha * pdm + (1-alpha) * plus_rma
        minus_rma = alpha * mdm + (1-alpha) * minus_rma
        plus = 100 * plus_rma / atr if atr else 0.0
        minus = 100 * minus_rma / atr if atr else 0.0
        dx = 100 * abs(plus-minus)/(plus+minus) if plus+minus else 0.0
        adx = dx if not i else alpha * dx + (1-alpha) * adx
        for key, value in zip(out, (adx, plus, minus)):
            out[key].append(value)
    return out


def _signals(values, p):
    entries, exits, reasons = [], [], []
    held = False
    a, plus, minus = (values[k] for k in ("adx", "plus_di", "minus_di"))
    for i in range(1, len(a)):
        if not held and i+1 >= 2*p["adx_period"] and a[i] > p["adx_threshold"] and plus[i-1] <= minus[i-1] and plus[i] > minus[i]:
            entries.append(i)
            held = True
        elif held and (minus[i-1] <= plus[i-1] and minus[i] > plus[i] or a[i] < p["adx_exit"]):
            exits.append(i)
            reasons.append("cross" if minus[i-1] <= plus[i-1] and minus[i] > plus[i] else "weak_adx")
            held = False
    return entries, exits, reasons


def _frame(fx):
    return pd.DataFrame(dict(Open=fx.get("opens", fx["closes"]), High=fx["highs"], Low=fx["lows"],
                             Close=fx["closes"], Volume=[1]*len(fx["closes"])),
                        index=pd.date_range("2026-01-01", periods=len(fx["closes"]), freq="h"))


def _run(fx, params=None):
    built = provider._build_adx_di_cross(fx["params"] if params is None else params)
    return Backtest(_frame(fx), built["strategy"], cash=100000, finalize_trades=False).run()


def test_golden_independent_formula_and_signals():
    fx = json.loads(FIXTURE.read_text())
    values = _hand(fx["highs"], fx["lows"], fx["closes"], fx["params"]["adx_period"])
    for key, series in values.items():
        assert series == pytest.approx(fx["expected_"+key], abs=1e-9)
    entries, exits, reasons = _signals(values, fx["params"])
    assert entries == fx["expected_entries"]
    assert exits == fx["expected_exits"]
    assert len(exits) >= 2
    assert set(reasons) == {"cross", "weak_adx"}
    assert provider._build_adx_di_cross(fx["params"])["min_bars"] == fx["min_bars"]


def test_golden_real_engine_times_prices_and_offset():
    fx = json.loads(FIXTURE.read_text())
    result = _run(fx)
    trades = result["_trades"]
    frame = _frame(fx)
    for side, signals in (("Entry", fx["expected_entries"]), ("Exit", fx["expected_exits"])):
        bars = [i+1 for i in signals[:len(fx["expected_exits"])]]
        assert list(trades[side+"Bar"]) == bars
        assert list(trades[side+"Time"]) == list(frame.index[bars])
        assert list(trades[side+"Price"]) == list(frame.Open.iloc[bars])
    assert all(trades.Size > 0)
    assert bool(result["_strategy"].position) == (len(fx["expected_entries"]) > len(fx["expected_exits"]))


def test_indicator_first_three_values_and_full_series_to_six_decimals():
    fx = json.loads(FIXTURE.read_text())
    assert len(fx["closes"]) >= 40
    expected = _hand(fx["highs"], fx["lows"], fx["closes"], 14)
    actual = provider._adx_di_arrays(fx["highs"], fx["lows"], fx["closes"], 14)
    for key in expected:
        assert actual[key][:3] == pytest.approx(expected[key][:3], abs=1e-6)
        assert actual[key] == pytest.approx(expected[key], abs=1e-6)
    zeros = provider._adx_di_arrays([100]*45, [100]*45, [100]*45, 14)
    assert all(value == 0 for series in zeros.values() for value in series)


def _controlled(monkeypatch, adx_value=25, weak_exit=False):
    """Isolate equality/exit predicates with injected indicators; use the real engine."""
    count = 40
    a, plus, minus = [adx_value]*count, [10.0]*count, [20.0]*count
    plus[28:] = [30.0]*(count-28)
    if weak_exit:
        a[32:] = [19.0]*(count-32)
    monkeypatch.setattr(provider, "_adx_di_arrays", lambda *args: dict(adx=a, plus_di=plus, minus_di=minus))
    fx = dict(closes=[100]*count, highs=[101]*count, lows=[99]*count, params={})
    return _run(fx)


def test_adx_equal_threshold_di_cross_does_not_enter(monkeypatch):
    result = _controlled(monkeypatch)
    assert not result["_strategy"].position
    assert result["_trades"].empty


def test_adx_below_threshold_di_cross_does_not_enter(monkeypatch):
    assert not _controlled(monkeypatch, 24)["_strategy"].position


def test_weak_adx_alone_exits(monkeypatch):
    result = _controlled(monkeypatch, 26, True)
    assert list(result["_trades"].ExitBar) == [33]


def test_changing_two_parameter_sets_changes_trade_count():
    fx = json.loads(FIXTURE.read_text())
    counts = [len(_run(fx, p)["_trades"]) for p in ({}, {"adx_period": 7}, {"adx_period": 30})]
    assert counts[0] != counts[1]
    assert counts[0] != counts[2]


@pytest.mark.parametrize("key", CONTRACT)
def test_each_parameter_schema_defaults_and_bounds(key):
    typ, default, lo, hi = CONTRACT[key]
    props = provider.TOOL_SPECS[TOOL]["param_schema_properties"]
    assert props[key] == dict(type=typ, default=default, minimum=lo, maximum=hi)
    assert "stop_loss_pct" in props
    for v in (default, lo, hi):
        p = {key: v}
        if key == "adx_threshold" and v <= 20:
            p["adx_exit"] = 10
        if key == "adx_exit" and v >= 25:
            p["adx_threshold"] = 40
        assert provider._build_adx_di_cross(p)["strategy"]


@pytest.mark.parametrize("key", CONTRACT)
@pytest.mark.parametrize("bad", ["low", "high", None, True, "14", float("nan"), float("inf")])
def test_each_parameter_invalid_rejected(key, bad):
    if bad == "low":
        bad = CONTRACT[key][2]-1
    elif bad == "high":
        bad = CONTRACT[key][3]+1
    with pytest.raises(ValueError, match="INVALID_PARAMS"):
        provider._build_adx_di_cross({key: bad})


@pytest.mark.parametrize("exit_value", [25, 26])
def test_exit_at_or_above_threshold_rejected(exit_value):
    with pytest.raises(ValueError, match="INVALID_PARAMS"):
        provider._build_adx_di_cross(dict(adx_exit=exit_value))


def test_adx_equal_exit_does_not_close_without_reverse_cross(monkeypatch):
    count = 40
    a = [26.0]*32 + [20.0]*8
    plus = [10.0]*28 + [30.0]*12
    monkeypatch.setattr(provider, "_adx_di_arrays", lambda *args: dict(adx=a, plus_di=plus, minus_di=[20.0]*count))
    result = _run(dict(closes=[100]*count, highs=[101]*count, lows=[99]*count, params={}))
    assert result["_trades"].empty
    assert result["_strategy"].position


def test_cross_before_two_period_warmup_is_ignored(monkeypatch):
    count = 40
    monkeypatch.setattr(provider, "_adx_di_arrays", lambda *args: dict(
        adx=[30.0]*count, plus_di=[10.0]*10 + [30.0]*30, minus_di=[20.0]*count))
    result = _run(dict(closes=[100]*count, highs=[101]*count, lows=[99]*count, params={}))
    assert not result["_strategy"].position


def test_fractional_period_rejected():
    with pytest.raises(ValueError, match="INVALID_PARAMS"):
        provider._build_adx_di_cross(dict(adx_period=14.5))
