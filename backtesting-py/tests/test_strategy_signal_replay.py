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


def test_managed_stop_applies_next_bar_and_excludes_entry_bar_extreme():
    from strategy_dynamic_stop import StopRules
    from strategy_signal_replay import replay_managed_limit_signal

    raw = signal()
    raw["target_prices"] = [D(200)]
    bars = [bar(1000, 99, 150), bar(1300, 95, 112), bar(1600, 105, 109)]
    result = replay_managed_limit_signal(raw, bars, rules=StopRules(D(3), True), management_candles=bars)
    assert [e["event_type"] for e in result["events"]] == ["entry_hit", "sl_hit"]
    assert result["signal"]["closed_at"] == 1899
    assert result["signal"]["close_price"] == D("108.64")
    assert result["stop_updates"] == [
        {
            "observed_at": 1599,
            "effective_from": 1600,
            "previous_stop": D(90),
            "stop_loss": D("108.64"),
            "breakeven_active": True,
        }
    ]
    assert raw["stop_loss"] == 90


def test_current_bar_old_stop_has_priority_over_new_trailing_candidate():
    from strategy_dynamic_stop import StopRules
    from strategy_signal_replay import replay_managed_limit_signal

    raw = signal()
    raw["target_prices"] = [D(200)]
    bars = [bar(1000, 99, 101), bar(1300, 89, 120)]
    result = replay_managed_limit_signal(raw, bars, rules=StopRules(D(3), True), management_candles=bars)
    assert result["signal"]["close_price"] == 90
    assert result["stop_updates"] == []


def test_managed_updates_use_strategy_timeframe_not_each_observation():
    from strategy_dynamic_stop import StopRules
    from strategy_signal_replay import replay_managed_limit_signal

    raw = signal()
    raw["target_prices"] = [D(200)]
    bars = [bar(1000 + 300 * i, 99, 101) for i in range(3)]
    bars += [bar(1900 + 300 * i, 95, 112) for i in range(3)]
    bars += [bar(2800 + 300 * i, 105, 109) for i in range(3)]
    management = [
        Candle(
            open_time=group[0].open_time,
            close_time=group[-1].close_time,
            open=group[0].open,
            high=max(b.high for b in group),
            low=min(b.low for b in group),
            close=group[-1].close,
        )
        for group in (bars[:3], bars[3:6], bars[6:])
    ]
    result = replay_managed_limit_signal(raw, bars, rules=StopRules(D(3)), management_candles=management)
    assert result["stop_updates"][0]["effective_from"] == 2800
    assert result["signal"]["closed_at"] == 3099
    assert result["signal"]["close_price"] == D("108.64")
    with pytest.raises(ValueError, match="OHLC"):
        wrong = [
            Candle(
                open_time=b.open_time, close_time=b.close_time, open=b.open, high=b.high + 1, low=b.low, close=b.close
            )
            for b in management
        ]
        replay_managed_limit_signal(raw, bars, rules=StopRules(D(3)), management_candles=wrong)
