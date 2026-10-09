"""Independent population-std, inclusive prior-window rank and real fill golden."""
import json
import math
import sys
from pathlib import Path

import pandas as pd
import pytest
from backtesting import Backtest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as provider

TOOL = "local.backtesting_py.bollinger_squeeze_breakout"
FIXTURE = Path(__file__).parent / "fixtures/bollinger_squeeze_breakout_golden.json"
CONTRACT = {"bb_period": ("integer", 20, 10, 50), "bb_std": ("number", 2.0, 1, 3),
            "bandwidth_lookback": ("integer", 120, 50, 300), "squeeze_pct": ("number", 20, 5, 40)}


def _hand(closes, p):
    period, lookback, mult = p["bb_period"], p["bandwidth_lookback"], p["bb_std"]
    out = {key: [] for key in ("middle", "upper", "lower", "bandwidth", "previous_rank")}
    for i, close in enumerate(closes):
        middle = upper = lower = bw = rank = None
        if i+1 >= period:
            window = closes[i+1-period:i+1]
            middle = sum(window)/period
            sd = math.sqrt(sum((v-middle)**2 for v in window)/period)
            upper, lower = middle+mult*sd, middle-mult*sd
            bw = (upper-lower)/middle if middle else None
        if i >= lookback:
            window = out["bandwidth"][i-lookback:i]
            if all(v is not None for v in window):
                rank = sum(v <= window[-1] for v in window)/lookback
        for key, value in zip(out, (middle, upper, lower, bw, rank)):
            out[key].append(value)
    return out


def _signals(closes, values, p):
    entries, exits, held = [], [], False
    for i, c in enumerate(closes):
        rank = values["previous_rank"][i]
        if not held and rank is not None and rank <= p["squeeze_pct"]/100 and c > values["upper"][i]:
            entries.append(i)
            held = True
        elif held and c < values["middle"][i]:
            exits.append(i)
            held = False
    return entries, exits


def _frame(closes):
    return pd.DataFrame(dict(Open=closes, High=[v+1 for v in closes], Low=[v-1 for v in closes],
                             Close=closes, Volume=[1]*len(closes)),
                        index=pd.date_range("2026-01-01", periods=len(closes), freq="h"))


def _run(closes, params=None):
    built = provider._build_bollinger_squeeze_breakout(params or {})
    return Backtest(_frame(closes), built["strategy"], cash=100000, finalize_trades=False).run()


def test_golden_formula_signals_and_provider_values():
    fx = json.loads(FIXTURE.read_text())
    assert len(fx["closes"]) >= 150
    hand = _hand(fx["closes"], fx["params"])
    actual = provider._bollinger_squeeze_arrays(fx["closes"], 20, 2, 120)
    for key, series in hand.items():
        for i, want in enumerate(fx["expected_"+key]):
            if want is None:
                assert series[i] is None
                assert math.isnan(actual[key][i])
            else:
                assert series[i] == pytest.approx(want, abs=1e-9)
                assert actual[key][i] == pytest.approx(want, abs=1e-9)
    entries, exits = _signals(fx["closes"], hand, fx["params"])
    assert entries == fx["expected_entries"]
    assert exits == fx["expected_exits"]
    assert len(exits) >= 2


def test_golden_engine_times_prices_and_next_open():
    fx = json.loads(FIXTURE.read_text())
    result = _run(fx["closes"], fx["params"])
    trades = result["_trades"]
    df = _frame(fx["closes"])
    for side, signals in (("Entry", fx["expected_entries"]), ("Exit", fx["expected_exits"])):
        bars = [i+1 for i in signals[:len(fx["expected_exits"])]]
        assert list(trades[side+"Bar"]) == bars
        assert list(trades[side+"Time"]) == list(df.index[bars])
        assert list(trades[side+"Price"]) == list(df.Open.iloc[bars])
    assert all(trades.Size > 0)
    assert bool(result["_strategy"].position) == (len(fx["expected_entries"]) > len(fx["expected_exits"]))


def _boundary_closes():
    # bw rises from 0 to a peak, then contracts. At bar 139 the prior window
    # contains exactly 24/120 values <= bw[138], including itself (rank == 20%).
    c = ([100.0]*42 + [100+(-1)**i*10 for i in range(70)]
         + [100+(-1)**i*(1-i*.03) for i in range(27)])
    return c + [110.0, 110.0, 90.0, 90.0]


def test_rank_equal_percentile_is_a_squeeze():
    closes = _boundary_closes()
    values = _hand(closes, dict(bb_period=20, bb_std=2, bandwidth_lookback=120))
    # The fixture below is tuned independently and verifies the literal rank boundary.
    assert values["previous_rank"][139] == .2
    result = _run(closes)
    assert list(result["_trades"].EntryBar) == [140]


def test_prior_bar_not_squeezed_breakout_does_not_enter():
    closes = [100+(-1)**i*(1+i*.01) for i in range(160)] + [120,120]
    values = _hand(closes, dict(bb_period=20, bb_std=2, bandwidth_lookback=120))
    assert closes[160] > values["upper"][160]
    assert values["previous_rank"][160] > .2
    assert not _run(closes)["_strategy"].position


def test_incomplete_bandwidth_window_does_not_enter():
    closes = [100.0]*100 + [120.0,120.0]
    result = _run(closes)
    assert result["_trades"].empty
    assert not result["_strategy"].position


def test_bandwidth_zero_middle_is_undefined():
    values = provider._bollinger_squeeze_arrays([0]*150, 20, 2, 120)
    assert all(math.isnan(v) for v in values["previous_rank"])


def test_changing_two_parameter_sets_changes_trade_count():
    fx = json.loads(FIXTURE.read_text())
    counts = [len(_run(fx["closes"], p)["_trades"]) for p in ({}, {"squeeze_pct":5}, {"bb_period":50, "bb_std":3})]
    assert counts[0] != counts[1]
    assert counts[0] != counts[2]


def test_name_and_warmup():
    built = provider._build_bollinger_squeeze_breakout({})
    assert built["executed_name"] == "Bollinger Squeeze Breakout (20/2.0, 120/20%)"
    assert built["min_bars"] == 140


@pytest.mark.parametrize("key", CONTRACT)
def test_each_parameter_schema_defaults_and_bounds(key):
    typ, default, lo, hi = CONTRACT[key]
    props = provider.TOOL_SPECS[TOOL]["param_schema_properties"]
    assert props[key] == dict(type=typ, default=default, minimum=lo, maximum=hi)
    assert "stop_loss_pct" in props
    for value in (default, lo, hi):
        assert provider._build_bollinger_squeeze_breakout({key:value})["strategy"]


@pytest.mark.parametrize("key", CONTRACT)
@pytest.mark.parametrize("bad", ["low", "high", None, True, "20", float("nan"), float("inf")])
def test_each_parameter_invalid_rejected(key, bad):
    if bad == "low":
        bad = CONTRACT[key][2]-1
    elif bad == "high":
        bad = CONTRACT[key][3]+1
    with pytest.raises(ValueError, match="INVALID_PARAMS"):
        provider._build_bollinger_squeeze_breakout({key:bad})


def test_close_equal_upper_does_not_enter(monkeypatch):
    count = 150
    monkeypatch.setattr(provider, "_bollinger_squeeze_arrays", lambda *args: dict(
        middle=[90.0]*count, upper=[100.0]*count, previous_rank=[.1]*count))
    result = _run([100.0]*count)
    assert result["_trades"].empty
    assert not result["_strategy"].position


def test_close_equal_middle_does_not_exit(monkeypatch):
    count = 150
    monkeypatch.setattr(provider, "_bollinger_squeeze_arrays", lambda *args: dict(
        middle=[100.0]*count, upper=[105.0]*count, previous_rank=[.1]*count))
    result = _run([100.0]*139 + [110.0] + [100.0]*10)
    assert result["_trades"].empty
    assert result["_strategy"].position


def test_prior_rank_does_not_include_current_or_future_bar():
    closes = _boundary_closes()
    a = provider._bollinger_squeeze_arrays(closes, 20, 2, 120)
    changed = closes[:139] + [200.0]*4
    b = provider._bollinger_squeeze_arrays(changed, 20, 2, 120)
    assert a["previous_rank"][139] == b["previous_rank"][139] == .2


@pytest.mark.parametrize("key", ["bb_period", "bandwidth_lookback"])
def test_fractional_period_rejected(key):
    with pytest.raises(ValueError, match="INVALID_PARAMS"):
        provider._build_bollinger_squeeze_breakout({key:CONTRACT[key][1]+.5})


def test_default_window_with_real_warmup_prefix():
    fx = json.loads(FIXTURE.read_text())
    df = _frame(fx["closes"])
    built = provider._build_bollinger_squeeze_breakout({})
    strategy = built["strategy"]
    prefix = 140
    strategy._warmup_bars = prefix
    strategy._warmup_cols = {col:df[col].iloc[:prefix].to_numpy() for col in df.columns}
    result = Backtest(df.iloc[prefix:], strategy, cash=100000, finalize_trades=False).run()
    trades = result["_trades"]
    assert all(i >= prefix for i in fx["expected_entries"])
    assert list(trades.EntryBar) == [i+1-prefix for i in fx["expected_entries"][:len(fx["expected_exits"])]]
    assert list(trades.ExitBar) == [i+1-prefix for i in fx["expected_exits"]]
    assert result["_strategy"].previous_rank[0] == pytest.approx(fx["expected_previous_rank"][prefix])
