from test_phase5_signal_http import run_phase5_http, CASES
import pytest
from decimal import Decimal

RULES = {"trailing_pct": "3", "breakeven": True, "update_timeframe": "strategy", "entry_bar": "exclude", "effective_from": "next_bar"}

@pytest.mark.parametrize("name,params", CASES)
def test_managed_http_report(monkeypatch, tmp_path, name, params):
    fixture = run_phase5_http(monkeypatch, tmp_path, name, params, RULES)
    replay = fixture["report"]["replay"]
    assert replay["stop_updates"]
    assert replay["settlements"]
    assert all(Decimal(str(update["stop_loss"])) >= Decimal(str(update["previous_stop"])) for update in replay["stop_updates"])
    assert all(update["effective_from"] == update["observed_at"] + 1 for update in replay["stop_updates"])
    assert len(replay["settlements"]) == 5
    assert len(replay["stop_updates"]) == 5
