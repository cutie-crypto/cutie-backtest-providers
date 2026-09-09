import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from copy import deepcopy
from decimal import Decimal as D

import pytest

from signal_lifecycle_kernel import Candle
from strategy_signal_replay import replay_limit_signal


def signal():
    return {
        "signal_type": "crypto",
        "status": "published",
        "direction": "long",
        "entry_execution_mode": "limit_only",
        "created_at": 1000,
        "entry_price": D(100),
        "create_snapshot_price": D(100),
        "stop_loss": D(90),
        "target_prices": [D(110)],
        "expires_at": 3000,
        "lifecycle_status": "awaiting_entry",
        "tp_hit_count": 0,
        "filled_position_pct": D(0),
    }


def bar(start, low, high):
    return Candle(open_time=start, close_time=start + 299, open=D(low), high=D(high), low=D(low), close=D(high))


def test_actual_detector_entry_then_exit_historical_clock_and_no_input_mutation():
    raw = signal()
    original = deepcopy(raw)
    result = replay_limit_signal(raw, [bar(1000, 99, 101), bar(1300, 101, 111)])
    assert [e["event_type"] for e in result["events"]] == ["entry_hit", "tp_hit"]
    assert result["signal"]["lifecycle_status"] == "tp_full" and result["terminal"]
    assert result["signal"]["entry_hit_at"] == 1299 and result["signal"]["closed_at"] == 1599
    assert raw == original


def test_same_bar_sl_before_entry_and_tp_preference_match_runtime():
    assert [e["event_type"] for e in replay_limit_signal(signal(), [bar(1000, 89, 101)])["events"]] == [
        "expired_unfilled"
    ]
    both = replay_limit_signal(signal(), [bar(1000, 89, 111)])
    assert [e["event_type"] for e in both["events"]] == ["entry_hit", "tp_hit"]


def test_open_position_not_forced_closed_and_unfilled_not_a_trade():
    pending = replay_limit_signal(signal(), [bar(1000, 101, 102)])
    assert not pending["terminal"] and pending["events"] == []
    opened = replay_limit_signal(signal(), [bar(1000, 99, 101)])
    assert not opened["terminal"] and opened["signal"]["status"] == "active"
    raw = signal()
    raw["expires_at"] = 1050
    expired = replay_limit_signal(raw, [bar(1000, 101, 102)])
    assert expired["signal"]["lifecycle_status"] == "expired_unfilled"
    assert expired["signal"]["filled_position_pct"] == 0


def test_missing_observation_bar_is_evidence_failure():
    with pytest.raises(ValueError, match="contiguous"):
        replay_limit_signal(signal(), [bar(1000, 99, 101), bar(1600, 101, 111)])


@pytest.mark.parametrize("low,high,expected", [(89, 99, "tp_full"), (99, 111, "stopped_out")])
def test_short_entry_and_exit_are_direction_symmetric(low, high, expected):
    raw = signal()
    raw.update(direction="short", stop_loss=D(110), target_prices=[D(90)])
    result = replay_limit_signal(raw, [bar(1000, 99, 101), bar(1300, low, high)])
    assert result["signal"]["lifecycle_status"] == expected


def test_missing_initial_observation_is_not_a_later_fill():
    with pytest.raises(ValueError, match="cover signal creation"):
        replay_limit_signal(signal(), [bar(1300, 99, 101)])
