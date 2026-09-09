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
