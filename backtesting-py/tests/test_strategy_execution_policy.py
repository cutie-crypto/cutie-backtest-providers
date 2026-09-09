import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from copy import deepcopy

import pytest

from strategy_execution_policy import (
    execution_policy_hash,
    require_matching_execution_policy,
    validate_execution_policy,
)


def policy():
    return {
        "schema": "cutie.strategy_signal_execution.v1",
        "reference_price": "signal_bar_close",
        "entry_mode": "limit_only",
        "observation_timeframe": "5m",
        "evaluation_lag_seconds": 30,
        "indicator_history_bars": 14,
        "sl_tp_rule": {
            "stop_loss": {"type": "fixed_pct", "pct": "5"},
            "take_profit": {"type": "atr_multiplier", "period": 14, "multiplier": "2"},
        },
    }


def test_canonical_equivalent_policy_and_changed_risk_rule():
    first, second = policy(), policy()
    second["sl_tp_rule"]["stop_loss"]["pct"] = "5.00"
    assert execution_policy_hash(first) == execution_policy_hash(second)
    assert require_matching_execution_policy(first, second) == validate_execution_policy(first)
    second["sl_tp_rule"]["stop_loss"]["pct"] = "6"
    with pytest.raises(ValueError, match="new backtest"):
        require_matching_execution_policy(first, second)


@pytest.mark.parametrize("value", [True, None, "NaN", "Infinity", "-1", "0"])
def test_invalid_or_nonpositive_prices_cannot_enter_frozen_profile(value):
    raw = policy()
    raw["sl_tp_rule"]["stop_loss"]["pct"] = value
    with pytest.raises(ValueError):
        validate_execution_policy(raw)


@pytest.mark.parametrize(
    "field,value",
    [
        ("reference_price", "ticker"),
        ("entry_mode", "market_base_limit"),
        ("observation_timeframe", "1h"),
        ("evaluation_lag_seconds", True),
        ("evaluation_lag_seconds", 0),
    ],
)
def test_model_drift_rejected(field, value):
    raw = policy()
    raw[field] = value
    with pytest.raises(ValueError):
        validate_execution_policy(raw)


def test_unknown_fields_and_input_mutation_do_not_change_frozen_profile():
    raw = policy()
    original = deepcopy(raw)
    validated = validate_execution_policy(raw)
    validated["sl_tp_rule"]["stop_loss"]["pct"] = "9"
    assert raw == original
    raw["sl_tp_rule"]["stop_loss"]["trailing"] = True
    with pytest.raises(ValueError):
        validate_execution_policy(raw)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        {"daily_limit": True, "author_daily_limit": 10, "cooldown_seconds": 0, "day_offset_seconds": 0},
        {"daily_limit": 3, "author_daily_limit": 10, "cooldown_seconds": 0, "day_offset_seconds": 86400},
    ],
)
def test_signal_request_rejects_incomplete_or_invalid_limits(bad):
    from strategy_execution_policy import requested_signal_execution

    with pytest.raises(ValueError):
        requested_signal_execution({"execution_policy": policy(), "cycle_limits": bad}, {"direction": "long"})


def test_signal_request_requires_risk_and_normalizes_before_snapshot():
    from strategy_execution_policy import requested_signal_execution

    request = {
        "execution_policy": policy(),
        "cycle_limits": {"daily_limit": 3, "author_daily_limit": 10, "cooldown_seconds": 0, "day_offset_seconds": 0},
    }
    request["execution_policy"]["sl_tp_rule"]["stop_loss"]["pct"] = 5.0
    with pytest.raises(ValueError, match="requires risk_policy"):
        requested_signal_execution(request, None)
    normalized = requested_signal_execution(request, {"direction": "long"})
    assert normalized["execution_policy"]["sl_tp_rule"]["stop_loss"]["pct"] == "5"
    assert request["execution_policy"]["sl_tp_rule"]["stop_loss"]["pct"] == 5.0
