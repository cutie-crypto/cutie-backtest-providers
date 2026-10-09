"""Keltner: independent scalar golden, strict boundaries and real next-open fills."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from backtesting import Backtest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as provider

TOOL = "local.backtesting_py.keltner_breakout"
FIXTURE = Path(__file__).parent / "fixtures/keltner_breakout_golden.json"
CONTRACT = {"ema_period": (20, 5, 100), "atr_period": (14, 2, 100), "multiplier": (2, 1, 4)}


def _fixture():
    return json.loads(FIXTURE.read_text())


def _hand(fx):
    """Scalar recurrence, independent of pandas and the strategy implementation."""
    p = fx["params"]
    alpha, beta = 2 / (p["ema_period"] + 1), 1 / p["atr_period"]
    mid, atr, upper = [], [], []
    for i, close in enumerate(fx["closes"]):
        high, low = fx["highs"][i], fx["lows"][i]
        tr = high - low if i == 0 else max(high-low, abs(high-fx["closes"][i-1]), abs(low-fx["closes"][i-1]))
        mid.append(close if i == 0 else alpha*close + (1-alpha)*mid[-1])
        atr.append(tr if i == 0 else beta*tr + (1-beta)*atr[-1])
        upper.append(mid[-1] + p["multiplier"]*atr[-1])
    return dict(mid=mid, atr=atr, upper=upper)


def _frame(fx):
    return pd.DataFrame(dict(Open=fx.get("opens", fx["closes"]), High=fx["highs"], Low=fx["lows"],
                             Close=fx["closes"], Volume=[1]*len(fx["closes"])),
                        index=pd.date_range("2026-01-01", periods=len(fx["closes"]), freq="h"))


def _run(fx, params=None):
    # Exercise the registered factory, with the same next-open and exclusive-order mode as the provider.
    built = provider.TOOL_SPECS[TOOL]["build"](fx["params"] if params is None else params)
    return Backtest(_frame(fx), built["strategy"], cash=100000,
                    exclusive_orders=True, finalize_trades=False).run()


def test_golden_independent_formula_and_provider_arrays():
    fx = _fixture()
    hand = _hand(fx)
    actual = provider._keltner_arrays(fx["highs"], fx["lows"], fx["closes"], **fx["params"])
    assert len(fx["closes"]) >= 80
    for key in hand:
        assert hand[key] == pytest.approx(fx["expected_"+key], abs=1e-10)
        assert actual[key] == pytest.approx(hand[key], abs=1e-10)
    entries, exits, held = [], [], False
    for i in range(fx["min_bars"]-1, len(fx["closes"])-1):
        if not held and fx["closes"][i] > hand["upper"][i]:
            entries.append(i)
            held = True
        elif held and fx["closes"][i] < hand["mid"][i]:
            exits.append(i)
            held = False
    assert (entries, exits) == (fx["expected_entries"], fx["expected_exits"])
    assert len(exits) >= 3


def test_golden_real_engine_times_prices_and_offset():
    fx = _fixture()
    result = _run(fx)
    trades = result["_trades"]
    assert len(trades) == len(fx["expected_trades"]) >= 3
    for (_, actual), expected in zip(trades.iterrows(), fx["expected_trades"]):
        for side in ("Entry", "Exit"):
            assert actual[side+"Bar"] == expected[side.lower()+"_bar"]
            assert actual[side+"Time"] == pd.Timestamp(expected[side.lower()+"_time"])
            assert actual[side+"Price"] == expected[side.lower()+"_price"]
        assert actual["Size"] > 0
    assert list(trades.EntryBar) == [i+1 for i in fx["expected_entries"]]
    assert list(trades.ExitBar) == [i+1 for i in fx["expected_exits"]]
    assert not result["_strategy"].position


def _boundary(close):
    # EMA(5)=102, ATR(2)=4 at bar 10 when Close=High=Low=106: upper=106 exactly.
    return dict(closes=[100.]*10+[close, 100., 100.], highs=[101.]*10+[close, 100., 100.],
                lows=[99.]*10+[close, 100., 100.], params=dict(ema_period=5, atr_period=2, multiplier=1))


def test_close_equal_upper_does_not_enter():
    fx = _boundary(106.)
    arrays = provider._keltner_arrays(fx["highs"], fx["lows"], fx["closes"], **fx["params"])
    assert arrays["upper"][10] == fx["closes"][10] == 106.
    result = _run(fx)
    assert len(result["_trades"]) == 0
    assert not result["_strategy"].position


def test_close_just_above_upper_enters_next_open():
    result = _run(_boundary(106.125))
    assert list(result["_trades"].EntryBar) == [11]
    assert list(result["_trades"].EntryPrice) == [100.]


def test_close_equal_mid_does_not_exit():
    fx = _boundary(106.125)
    mid = _hand(fx)["mid"][10]
    for i in (11, 12):
        for key in ("closes", "highs", "lows"):
            fx[key][i] = mid
    result = _run(fx)
    assert len(result["_trades"]) == 0
    assert result["_strategy"].position


def test_atr_first_true_range_and_rma_match_supertrend():
    fx = _fixture()
    actual = provider._keltner_arrays(fx["highs"], fx["lows"], fx["closes"], **fx["params"])["atr"]
    reference = provider._supertrend_arrays(fx["highs"], fx["lows"], fx["closes"], 14, 2)["atr"]
    assert actual[0] == fx["highs"][0] - fx["lows"][0] == 3.
    assert actual[1] == pytest.approx(3*(13/14) + 2/14)
    assert actual == pytest.approx(reference, abs=1e-12)


def test_indicator_prefix_is_independent_of_future_bars():
    fx = _fixture()
    full = provider._keltner_arrays(fx["highs"], fx["lows"], fx["closes"], **fx["params"])
    prefix = provider._keltner_arrays(fx["highs"][:40], fx["lows"][:40], fx["closes"][:40], **fx["params"])
    for key in full:
        np.testing.assert_array_equal(full[key][:40], prefix[key])


@pytest.mark.parametrize("ema,atr", [(20, 14), (5, 100), (100, 2)])
def test_min_bars_covers_both_periods_and_blocks_early_entry(ema, atr):
    built = provider._build_keltner_breakout(dict(ema_period=ema, atr_period=atr, multiplier=1))
    assert built["min_bars"] == max(ema, atr)+1
    count = max(ema, atr)
    closes = [100.+20*i for i in range(count)]
    fx = dict(closes=closes, highs=[v+1 for v in closes], lows=[v-1 for v in closes])
    result = Backtest(_frame(fx), built["strategy"], cash=100000, finalize_trades=False).run()
    assert not result["_strategy"].position
    assert len(result["_trades"]) == 0


def test_warmup_arrays_match_full_history():
    fx = _fixture()
    split = 25
    frame = _frame(fx)
    built = provider._build_keltner_breakout(fx["params"])
    strategy = built["strategy"]
    strategy._warmup_bars = split
    strategy._warmup_cols = {key: frame[key].to_numpy()[:split] for key in ("High", "Low", "Close")}
    result = Backtest(frame.iloc[split:], strategy, cash=100000, finalize_trades=False).run()
    for key in ("mid", "upper"):
        assert getattr(result["_strategy"], key) == pytest.approx(fx["expected_"+key][split:], abs=1e-10)
    assert list(result["_trades"].EntryBar) == [i+1-split for i in fx["expected_entries"]]


@pytest.mark.parametrize("key", CONTRACT)
@pytest.mark.parametrize("value_index", [0, 1, 2], ids=["default", "min", "max"])
def test_each_parameter_accepts_default_min_max(key, value_index):
    value = CONTRACT[key][value_index]
    built = provider._build_keltner_breakout({key: value})
    params = {k: v[0] for k, v in CONTRACT.items()}
    params[key] = value
    assert built["min_bars"] == max(params["ema_period"], params["atr_period"])+1
    assert built["executed_name"] == f"Keltner Breakout ({params['ema_period']}/{params['atr_period']}/{params['multiplier']:g})"
    assert provider._validate_params_against_schema({key: value}, provider.TOOL_SPECS[TOOL]["param_schema_properties"]) is None


@pytest.mark.parametrize("key", CONTRACT)
@pytest.mark.parametrize("side", ["below", "above"])
def test_each_parameter_rejects_out_of_range(key, side):
    _, lo, hi = CONTRACT[key]
    value = lo-1 if side == "below" else hi+1
    with pytest.raises(ValueError, match="^INVALID_PARAMS:"):
        provider._build_keltner_breakout({key: value})
    assert provider._validate_params_against_schema({key: value}, provider.TOOL_SPECS[TOOL]["param_schema_properties"]) is not None


@pytest.mark.parametrize("key", CONTRACT)
@pytest.mark.parametrize("value", [True, False, None, "20", float("nan"), float("inf"), -float("inf"), [], 10**400])
def test_rejects_bool_non_numbers_and_non_finite(key, value):
    with pytest.raises(ValueError, match="^INVALID_PARAMS:"):
        provider._build_keltner_breakout({key: value})


@pytest.mark.parametrize("key,value", [("ema_period", 20.), ("ema_period", 20.5), ("atr_period", 14.), ("atr_period", 14.5)])
def test_integer_parameters_reject_float_even_if_integral(key, value):
    with pytest.raises(ValueError, match="^INVALID_PARAMS:"):
        provider._build_keltner_breakout({key: value})


def test_defaults_catalog_and_risk_schema():
    built = provider._build_keltner_breakout({})
    assert built["executed_name"] == "Keltner Breakout (20/14/2)"
    assert built["min_bars"] == 21
    spec = provider.TOOL_SPECS[TOOL]
    assert spec["strategy_family"] == provider.TOOL_SPECS["local.backtesting_py.bollinger_breakout"]["strategy_family"] == "breakout"
    assert spec["is_default"] is False
    for key, (default, lo, hi) in CONTRACT.items():
        assert spec["param_schema_properties"][key] == dict(type="number" if key == "multiplier" else "integer",
                                                         default=default, minimum=lo, maximum=hi)
    for key in ("stop_loss_pct", "risk_layer_enabled"):
        assert key in spec["param_schema_properties"]
    assert built["strategy"]._risk == {}  # ATR stop is opt-in, absent from the template default.


def test_shared_atr_stop_exits_before_template_signal_at_next_open():
    fx = _fixture()
    entry_bar = fx["expected_entries"][0]+1
    # Entry bar touches the risk stop, while its close is still above EMA (no template exit).
    fx["lows"][entry_bar] = fx["opens"][entry_bar]-30
    plain = _run(fx)
    risk_params = dict(fx["params"], risk_layer_enabled=True, atr_stop_multiplier=2, risk_atr_period=14)
    stopped = _run(fx, risk_params)
    trade = stopped["_trades"].iloc[0]
    assert trade.EntryBar == entry_bar
    assert trade.ExitBar == entry_bar+1 < plain["_trades"].iloc[0].ExitBar
    assert trade.ExitTime == _frame(fx).index[entry_bar+1]
    assert trade.ExitPrice == fx["opens"][entry_bar+1]
    assert fx["closes"][entry_bar] > _hand(fx)["mid"][entry_bar]
    from strategy_risk_overlay import risk_atr_series
    atr = risk_atr_series(fx["highs"], fx["lows"], fx["closes"], 14)[entry_bar-1]
    assert fx["lows"][entry_bar] <= trade.EntryPrice-2*atr
    # Isolate this first trade to inspect its actual shared-layer reason and frozen signal ATR.
    short = {k: v[:entry_bar+2] if isinstance(v, list) else v for k, v in fx.items()}
    strategy = _run(short, risk_params)["_strategy"]
    assert strategy._risk_exit_reason == "stop_loss"
    assert float(strategy._risk_state.initial_distance) == pytest.approx(2*atr)
    assert float(strategy._risk_state.entry_price) == trade.EntryPrice
