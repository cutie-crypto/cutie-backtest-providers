"""SAR: independent Wilder loop golden, strict boundaries and real next-open fills."""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from backtesting import Backtest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as provider

TOOL = "local.backtesting_py.parabolic_sar"
FIXTURE = Path(__file__).parent / "fixtures/parabolic_sar_golden.json"
CONTRACT = {"af_start": (0.02, 0.01, 0.1), "af_step": (0.02, 0.01, 0.1), "af_max": (0.2, 0.1, 0.5)}


def _hand(fx, params=None):
    p = params or fx["params"]
    h, l, c = (fx[k] for k in ("highs", "lows", "closes"))
    values = {k: [None]*len(c) for k in ("sar", "trend", "ep", "af")}
    rising = c[1] > c[0]
    s = l[0] if rising else h[0]
    extreme = h[1] if rising else l[1]
    acceleration = p["af_start"]
    for i in range(1, len(c)):
        if i >= 2:
            candidate = s + acceleration*(extreme-s)
            if rising:
                candidate = min([candidate] + l[i-2:i])
                reverse = l[i] < candidate
                new_extreme = h[i] > extreme
            else:
                candidate = max([candidate] + h[i-2:i])
                reverse = h[i] > candidate
                new_extreme = l[i] < extreme
            if reverse:
                candidate = extreme
                rising = not rising
                extreme = h[i] if rising else l[i]
                acceleration = p["af_start"]
            elif new_extreme:
                extreme = h[i] if rising else l[i]
                acceleration = min(acceleration+p["af_step"], p["af_max"])
            s = candidate
        for key, value in zip(values, (s, 1 if rising else -1, extreme, acceleration)):
            values[key][i] = value
    return values


def _signals(values):
    held = False
    entries, exits = [], []
    t = values["trend"]
    for i in range(2, len(t)):
        if not held and t[i-1] < 0 < t[i]:
            entries.append(i)
            held = True
        elif held and t[i-1] > 0 > t[i]:
            exits.append(i)
            held = False
    return entries, exits


def _frame(fx):
    return pd.DataFrame(dict(Open=fx.get("opens", fx["closes"]), High=fx["highs"], Low=fx["lows"],
                             Close=fx["closes"], Volume=[1]*len(fx["closes"])),
                        index=pd.date_range("2026-01-01", periods=len(fx["closes"]), freq="h"))


def _run(fx, params=None):
    built = provider._build_parabolic_sar(fx["params"] if params is None else params)
    return Backtest(_frame(fx), built["strategy"], cash=100000, finalize_trades=False).run()


def test_golden_independent_formula_and_provider_arrays():
    fx = json.loads(FIXTURE.read_text())
    hand = _hand(fx)
    actual = provider._parabolic_sar_arrays(fx["highs"], fx["lows"], fx["closes"], **fx["params"])
    assert len(fx["closes"]) >= 80
    for key in hand:
        assert hand[key][1:] == pytest.approx(fx["expected_"+key][1:], abs=1e-10)
        assert np.isnan(actual[key][0])
        assert actual[key][1:] == pytest.approx(hand[key][1:], abs=1e-10)
    assert _signals(hand) == (fx["expected_entries"], fx["expected_exits"])
    assert len(fx["expected_exits"]) >= 3
    assert fx["min_bars"] == provider._build_parabolic_sar({})["min_bars"] == 3


def test_golden_real_engine_times_prices_and_offset():
    fx = json.loads(FIXTURE.read_text())
    result = _run(fx)
    trades, frame = result["_trades"], _frame(fx)
    for side, signals in (("Entry", fx["expected_entries"]), ("Exit", fx["expected_exits"])):
        bars = [i+1 for i in signals[:len(fx["expected_exits"])]]
        assert list(trades[side+"Bar"]) == bars
        assert list(trades[side+"Time"]) == list(frame.index[bars])
        assert list(trades[side+"Price"]) == list(frame.Open.iloc[bars])
    assert all(trades.Size > 0)
    assert bool(result["_strategy"].position) == (len(fx["expected_entries"]) > len(fx["expected_exits"]))


def test_changing_two_parameter_sets_changes_trade_count():
    fx = json.loads(FIXTURE.read_text())
    counts = [len(_run(fx, p)["_trades"]) for p in ({}, {"af_start": 0.1, "af_step": 0.1, "af_max": 0.5},
                                                       {"af_start": 0.01, "af_step": 0.01, "af_max": 0.1})]
    assert counts[0] != counts[1], counts
    assert counts[0] != counts[2], counts


def test_uptrend_extreme_refresh_increments_af_and_caps_it():
    c = list(range(100, 130))
    a = provider._parabolic_sar_arrays([v+1 for v in c], [v-1 for v in c], c, 0.02, 0.02, 0.1)
    assert list(a["trend"][1:]) == [1]*29
    assert a["sar"][1:4] == pytest.approx([99, 99, 99.16])
    assert a["af"][1:] == pytest.approx([min(0.02*i, 0.1) for i in range(1, 30)])
    assert list(a["ep"][1:]) == [v+1 for v in c[1:]]


def test_rising_sar_is_clamped_to_previous_two_lows():
    c = list(range(100, 130))
    lows = [v-1 for v in c]
    a = provider._parabolic_sar_arrays([v+1 for v in c], lows, c, 0.1, 0.1, 0.5)
    for i in range(2, len(c)):
        assert a["trend"][i] == 1
        assert a["sar"][i] <= min(lows[i-2:i]), i


@pytest.mark.parametrize("up", [False, True])
def test_equal_sar_does_not_reverse_but_strict_cross_does(up):
    # Initial SAR=101 (down) or 99 (up), clamped to the first bar at i=2.
    c = [100, 101 if up else 99, 100, 100]
    h, l = [101, 102 if up else 100, 101, 101], [99, 100 if up else 98, 99, 99]
    a = provider._parabolic_sar_arrays(h, l, c, 0.02, 0.02, 0.2)
    assert a["trend"][2] == (1 if up else -1)
    if up:
        l[2] -= 0.01
    else:
        h[2] += 0.01
    b = provider._parabolic_sar_arrays(h, l, c, 0.02, 0.02, 0.2)
    assert b["trend"][2] == (-1 if up else 1)
    if not up:
        result = _run(dict(closes=c, highs=h, lows=l, params={}))
        assert result["_strategy"].trades[0].entry_bar == 3


def test_equal_initial_closes_mean_down_and_no_initial_signal():
    a = provider._parabolic_sar_arrays([101]*3, [99]*3, [100]*3, 0.02, 0.02, 0.2)
    assert a["sar"][1] == 101
    assert a["trend"][1] == -1
    assert not _run(dict(closes=[100]*3, highs=[101]*3, lows=[99]*3, params={}))["_strategy"].position


def test_arrays_are_causal_and_warmup_matches_full_prefix():
    fx = json.loads(FIXTURE.read_text())
    df = _frame(fx)
    built = provider._build_parabolic_sar(fx["params"])
    cls = built["strategy"]
    cls._warmup_bars = 20
    cls._warmup_cols = {k: df[k].iloc[:20].to_numpy() for k in provider._WARMUP_COLUMNS}
    st = Backtest(df.iloc[20:], cls, cash=100000).run()["_strategy"]
    a = provider._parabolic_sar_arrays(fx["highs"], fx["lows"], fx["closes"], **fx["params"])
    for key in ("sar", "trend"):
        assert np.asarray(getattr(st, key)) == pytest.approx(a[key][20:])
    changed = {**fx, **{k: fx[k][:40] + [999]*len(fx[k][40:]) for k in ("highs", "lows", "closes")}}
    b = provider._parabolic_sar_arrays(changed["highs"], changed["lows"], changed["closes"], **fx["params"])
    np.testing.assert_allclose(a["sar"][:40], b["sar"][:40], equal_nan=True)


@pytest.mark.parametrize("key", CONTRACT)
def test_each_parameter_default_min_max_and_schema(key):
    default, lo, hi = CONTRACT[key]
    spec = provider.TOOL_SPECS[TOOL]
    assert spec["strategy_family"] == "trend" and spec["is_default"] is False
    props = spec["param_schema_properties"]
    assert "stop_loss_pct" in props
    assert props[key] == dict(type="number", default=default, minimum=lo, maximum=hi)
    for v in (default, lo, hi):
        assert provider._build_parabolic_sar({key: v})["strategy"]
        assert provider._validate_params_against_schema({key: v}, props) is None
    assert provider._build_parabolic_sar({})["executed_name"] == "Parabolic SAR (0.02/0.02/0.2)"


@pytest.mark.parametrize("key", CONTRACT)
@pytest.mark.parametrize("bad", ["low", "high", None, True, "0.02", float("nan"), float("inf")])
def test_each_parameter_invalid_rejected(key, bad):
    if bad == "low":
        bad = CONTRACT[key][1]-0.001
    elif bad == "high":
        bad = CONTRACT[key][2]+0.001
    with pytest.raises(ValueError, match="INVALID_PARAMS"):
        provider._build_parabolic_sar({key: bad})


@pytest.mark.parametrize("key", ["af_start", "af_step"])
def test_acceleration_above_max_rejected(key):
    with pytest.raises(ValueError, match="INVALID_PARAMS"):
        provider._build_parabolic_sar({key: 0.11, "af_max": 0.1})
