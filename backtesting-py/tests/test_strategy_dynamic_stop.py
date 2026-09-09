import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from decimal import Decimal as D

import pytest

from strategy_dynamic_stop import StopRules, advance_stop, initial_stop_state


def start(direction="long"):
    return initial_stop_state(
        direction=direction, entry_price=D(100), initial_stop=D(90 if direction == "long" else 110), entry_at=100
    )


def bar(state, rules, i, high, low, close):
    return advance_stop(
        state, rules, open_at=101 + i * 300, close_at=400 + i * 300, high=D(high), low=D(low), close=D(close)
    )


def test_combined_ratchet_uses_extreme_but_breakeven_requires_close_and_frozen_r():
    rules = StopRules(D(3), True)
    original = start()
    first = bar(original, rules, 0, 109, 99, 105)
    assert original.effective_stop == 90  # caller's current-bar stop unchanged
    assert first.effective_stop == D("105.73")
    assert not first.breakeven_active
    # Current stop is above entry; R must still equal initial 100-90=10.
    second = bar(first, rules, 1, 111, 104, 109)
    assert second.effective_stop == D("107.67")
    assert not second.breakeven_active  # intrabar high alone cannot activate
    third = bar(second, rules, 2, 112, 108, 110)
    assert third.breakeven_active
    assert third.effective_stop == D("108.64")  # combined stop never resets to 100
    fourth = bar(third, rules, 3, 109, 108, 108)
    assert fourth.effective_stop == third.effective_stop
    assert fourth.breakeven_active


@pytest.mark.parametrize(
    "direction,high,low,close,expected",
    [
        ("long", 120, 95, 110, 100),
        ("short", 105, 80, 90, 100),
    ],
)
def test_breakeven_exact_1r_and_permanent_activation(direction, high, low, close, expected):
    rules = StopRules(breakeven=True)
    state = bar(start(direction), rules, 0, high, low, close)
    assert state.breakeven_active and state.effective_stop == expected
    assert bar(state, rules, 1, 105, 95, 100).effective_stop == expected


def test_short_trailing_mirrors_long_and_never_loosens():
    rules = StopRules(D(3), True)
    state = bar(start("short"), rules, 0, 101, 95, 98)
    assert state.effective_stop == D("97.85")
    state = bar(state, rules, 1, 98, 89, 90)
    assert state.effective_stop == D("91.67")
    assert state.breakeven_active
    assert bar(state, rules, 2, 96, 92, 95).effective_stop == state.effective_stop


def test_entry_bar_is_excluded_and_duplicate_or_older_bar_cannot_rewind():
    rules = StopRules(D(3))
    original = start()
    assert advance_stop(original, rules, open_at=0, close_at=299, high=D(1000), low=D(1), close=D(105)) == original
    state = bar(original, rules, 1, 110, 101, 108)
    assert bar(state, rules, 1, 999, 1, 999) == state
    assert bar(state, rules, 0, 999, 1, 999) == state
    with pytest.raises(ValueError, match="overlapping"):
        advance_stop(state, rules, open_at=600, close_at=900, high=D(110), low=D(100), close=D(105))


@pytest.mark.parametrize("value", [D(0), D(100), D(-1), D("NaN"), D("Infinity"), 3, True])
def test_invalid_trailing_rules(value):
    with pytest.raises(ValueError):
        StopRules(value)
