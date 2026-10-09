"""Ichimoku: hand rolling extrema, displaced cloud, real fills and causal prefix proof."""
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from backtesting import Backtest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as provider

TOOL = "local.backtesting_py.ichimoku_cloud_breakout"
FIXTURE = Path(__file__).parent / "fixtures/ichimoku_cloud_breakout_golden.json"
CONTRACT = {"tenkan_period": (9, 5, 20), "kijun_period": (26, 10, 60), "senkou_b_period": (52, 20, 120)}


def _hand(fx, params=None):
    p = fx["params"] if params is None else params
    h, l = fx["highs"], fx["lows"]
    out = {k: [] for k in ("tenkan", "kijun", "senkou_a", "senkou_b", "cloud_top")}
    raw_a, raw_b = [], []
    t, k, b = (p[key] for key in CONTRACT)
    for i in range(len(h)):
        mids = [(max(h[i-n+1:i+1])+min(l[i-n+1:i+1]))/2 if i+1 >= n else None for n in (t, k, b)]
        raw_a.append((mids[0]+mids[1])/2 if mids[1] is not None else None)
        raw_b.append(mids[2])
        a = raw_a[i-k] if i >= k else None
        bb = raw_b[i-k] if i >= k else None
        top = max(a, bb) if a is not None and bb is not None else None
        for key, v in zip(out, (mids[0], mids[1], a, bb, top)):
            out[key].append(v)
    return out


def _signals(fx, values, params=None):
    p = fx["params"] if params is None else params
    held = False
    entries, exits = [], []
    c, top, t, k = fx["closes"], values["cloud_top"], values["tenkan"], values["kijun"]
    for i in range(1, len(c)):
        if top[i] is None:
            continue
        if held and c[i] <= top[i]:
            exits.append(i)
            held = False
        elif (not held and i+1 >= p["senkou_b_period"]+p["kijun_period"] and top[i-1] is not None
              and c[i-1] <= top[i-1] and c[i] > top[i] and t[i] > k[i]):
            entries.append(i)
            held = True
    return entries, exits


def _frame(fx):
    return pd.DataFrame(dict(Open=fx.get("opens", fx["closes"]), High=fx["highs"], Low=fx["lows"],
                             Close=fx["closes"], Volume=[1]*len(fx["closes"])),
                        index=pd.date_range("2026-01-01", periods=len(fx["closes"]), freq="h"))


def _run(fx, params=None):
    built = provider._build_ichimoku_cloud_breakout(fx["params"] if params is None else params)
    return Backtest(_frame(fx), built["strategy"], cash=100000, finalize_trades=False).run()


def test_golden_hand_formula_provider_arrays_and_signals():
    fx = json.loads(FIXTURE.read_text())
    hand = _hand(fx)
    actual = provider._ichimoku_arrays(fx["highs"], fx["lows"], **fx["params"])
    assert len(fx["closes"]) >= 120
    for key, series in hand.items():
        for i, v in enumerate(series):
            expected = fx["expected_"+key][i]
            if v is None:
                assert expected is None and math.isnan(actual[key][i]), (key, i)
            else:
                assert v == pytest.approx(expected, abs=1e-10), (key, i)
                assert actual[key][i] == pytest.approx(expected, abs=1e-10), (key, i)
    assert _signals(fx, hand) == (fx["expected_entries"], fx["expected_exits"])
    assert len(fx["expected_exits"]) >= 3
    assert fx["min_bars"] == provider._build_ichimoku_cloud_breakout(fx["params"])["min_bars"]


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
    counts = [len(_run(fx, p)["_trades"]) for p in ({}, dict(tenkan_period=5, kijun_period=10, senkou_b_period=20),
                                                  dict(tenkan_period=15, kijun_period=40, senkou_b_period=80))]
    assert counts[0] != counts[1], counts
    assert counts[0] != counts[2], counts


def test_visible_cloud_is_exactly_kijun_bars_old():
    h, l = list(range(102, 222)), list(range(98, 218))
    a = provider._ichimoku_arrays(h, l, 9, 26, 52)
    # At i=77 the raw B at i=51 spans highs 102..153, lows 98..149.
    assert np.isnan(a["cloud_top"][:77]).all()
    assert a["senkou_b"][77] == (153+98)/2 == 125.5
    assert a["tenkan"][77] == (179+167)/2 == 173
    for i in range(77, 120):
        j = i-26
        t = (max(h[j-8:j+1])+min(l[j-8:j+1]))/2
        k = (max(h[j-25:j+1])+min(l[j-25:j+1]))/2
        b = (max(h[j-51:j+1])+min(l[j-51:j+1]))/2
        assert a["cloud_top"][i] == max((t+k)/2, b)


def test_rewriting_all_future_bars_preserves_cloud_and_signals():
    fx = json.loads(FIXTURE.read_text())
    # Choose an actual entry event, so invariant signals include a positive signal.
    i = fx["expected_entries"][1]
    changed = json.loads(json.dumps(fx))
    for key in ("opens", "highs", "lows", "closes"):
        changed[key][i+1:] = [1000+j*17 for j in range(len(fx[key])-i-1)]
    a = provider._ichimoku_arrays(fx["highs"], fx["lows"], **fx["params"])
    b = provider._ichimoku_arrays(changed["highs"], changed["lows"], **fx["params"])
    for key in a:
        np.testing.assert_allclose(a[key][:i+1], b[key][:i+1], equal_nan=True)
    # Capture emitted orders at next(), before broker filling; future opens can differ.
    def emitted(source):
        cls = provider._build_ichimoku_cloud_breakout(source["params"])["strategy"]
        base_next = cls.next
        events = []
        class Observed(cls):
            def next(self):
                base_next(self)
                if self.orders:
                    events.append((len(self.data)-1, bool(self.position)))
        Backtest(_frame(source), Observed, cash=100000).run()
        return [event for event in events if event[0] <= i]
    original_events = emitted(fx)
    assert (i, False) in original_events
    assert original_events == emitted(changed)
    assert a["cloud_top"][i] == b["cloud_top"][i]


def _controlled(monkeypatch, tenkan=101, kijun=100, previous=100, current=101, exit_close=99, cross_bar=80):
    n = 90
    close = [previous]*cross_bar + [current]*(n-cross_bar)
    close[85:] = [exit_close]*5
    monkeypatch.setattr(provider, "_ichimoku_arrays", lambda *args: dict(
        tenkan=[tenkan]*n, kijun=[kijun]*n, cloud_top=[100]*n))
    return _run(dict(closes=close, highs=[max(v, tenkan, kijun)+1 for v in close],
                     lows=[min(v, tenkan, kijun)-1 for v in close], params={}))


@pytest.mark.parametrize("tenkan", [99, 100])
def test_cross_above_cloud_with_tenkan_at_or_below_kijun_does_not_enter(monkeypatch, tenkan):
    result = _controlled(monkeypatch, tenkan=tenkan)
    assert result["_trades"].empty and not result["_strategy"].position


def test_close_equal_cloud_does_not_enter(monkeypatch):
    result = _controlled(monkeypatch, current=100)
    assert result["_trades"].empty and not result["_strategy"].position


def test_previous_equal_cloud_enters_and_current_equal_cloud_exits(monkeypatch):
    result = _controlled(monkeypatch, exit_close=100)
    assert list(result["_trades"].EntryBar) == [81]
    assert list(result["_trades"].ExitBar) == [86]


def test_already_above_cloud_is_not_a_new_cross(monkeypatch):
    result = _controlled(monkeypatch, previous=101)
    assert result["_trades"].empty and not result["_strategy"].position


def test_cross_before_min_bars_is_ignored(monkeypatch):
    result = _controlled(monkeypatch, cross_bar=40)
    assert result["_trades"].empty and not result["_strategy"].position


def test_indicator_warmup_matches_full_series():
    fx = json.loads(FIXTURE.read_text())
    df = _frame(fx)
    cls = provider._build_ichimoku_cloud_breakout(fx["params"])["strategy"]
    cls._warmup_bars = 78
    cls._warmup_cols = {k: df[k].iloc[:78].to_numpy() for k in provider._WARMUP_COLUMNS}
    st = Backtest(df.iloc[78:], cls, cash=100000).run()["_strategy"]
    a = provider._ichimoku_arrays(fx["highs"], fx["lows"], **fx["params"])
    for key in ("tenkan", "kijun", "cloud_top"):
        np.testing.assert_allclose(np.asarray(getattr(st, key)), a[key][78:], equal_nan=True)


@pytest.mark.parametrize("key", CONTRACT)
def test_each_parameter_default_min_max_and_schema(key):
    default, lo, hi = CONTRACT[key]
    spec = provider.TOOL_SPECS[TOOL]
    assert spec["strategy_family"] == "trend" and spec["is_default"] is False
    props = spec["param_schema_properties"]
    assert "stop_loss_pct" in props
    assert props[key] == dict(type="integer", default=default, minimum=lo, maximum=hi)
    for v in (default, lo, hi):
        p = {key: v}
        if key == "kijun_period" and v == 60:
            p["senkou_b_period"] = 120
        if key == "senkou_b_period" and v == 20:
            p["kijun_period"] = 10
        assert provider._build_ichimoku_cloud_breakout(p)["strategy"]
        assert provider._validate_params_against_schema(p, props) is None
    built = provider._build_ichimoku_cloud_breakout({})
    assert built["executed_name"] == "Ichimoku Cloud Breakout (9/26/52)"
    assert built["min_bars"] == 78


@pytest.mark.parametrize("key", CONTRACT)
@pytest.mark.parametrize("bad", ["low", "high", None, True, "9", 9.5, float("nan"), float("inf")])
def test_each_parameter_invalid_rejected(key, bad):
    if bad == "low":
        bad = CONTRACT[key][1]-1
    elif bad == "high":
        bad = CONTRACT[key][2]+1
    with pytest.raises(ValueError, match="INVALID_PARAMS"):
        provider._build_ichimoku_cloud_breakout({key: bad})


@pytest.mark.parametrize("params", [dict(tenkan_period=10, kijun_period=10),
                                    dict(tenkan_period=20, kijun_period=10),
                                    dict(kijun_period=52, senkou_b_period=52),
                                    dict(kijun_period=60, senkou_b_period=20)])
def test_invalid_period_order_rejected(params):
    with pytest.raises(ValueError, match="INVALID_PARAMS"):
        provider._build_ichimoku_cloud_breakout(params)
