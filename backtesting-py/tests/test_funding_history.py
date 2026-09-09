import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from funding_history import fetch_history, funding_paid


def row(ts, kind="Regular", rate="0.001"):
    return {
        "symbol": "BTCUSDT",
        "fundingTime": ts,
        "fundingRate": rate,
        "markPrice": "100",
        "rateType": kind,
    }


def test_pagination_preserves_two_rates_at_boundary():
    pages = [
        [row(i) for i in range(1, 1001)],
        [row(1000), row(1000, "Special"), row(1001)],
    ]
    calls = []

    def request(params):
        calls.append(params)
        return pages.pop(0)

    history = fetch_history("BTCUSDT", 0, 1001, request=request, now_ms=2000)
    assert calls[1]["startTime"] == 1000
    assert len(history["rows"]) == 1002
    segments = [{"entry_ms": 999, "exit_ms": 1000, "quantity": "0.5"}]
    assert funding_paid(history, segments, "long") == Decimal("0.1")
    assert funding_paid(history, segments, "short") == Decimal("-0.1")


def test_position_changes_and_negative_rates():
    history = fetch_history(
        "BTCUSDT", 0, 10, request=lambda _: [row(5), row(10, rate="-0.002")], now_ms=20
    )
    segments = [
        {"entry_ms": 0, "exit_ms": 5, "quantity": "0.25"},
        {"entry_ms": 0, "exit_ms": 10, "quantity": "0.75"},
    ]
    assert funding_paid(history, segments, "long") == Decimal("-0.05")
    assert funding_paid(
        history, [{"entry_ms": 5, "exit_ms": 10, "quantity": "1"}], "long"
    ) == Decimal("-0.2")


@pytest.mark.parametrize(
    "rows",
    [
        [row(1), row(1, rate="0.002")],
        [row(2), row(1)],
        [dict(row(1), markPrice="NaN")],
        [dict(row(1), symbol="ETHUSDT")],
    ],
)
def test_bad_evidence_is_rejected(rows):
    with pytest.raises(ValueError):
        fetch_history("BTCUSDT", 0, 10, request=lambda _: rows, now_ms=20)


def test_transport_failure_is_not_zero_cost():
    def fail(_):
        raise OSError("unavailable")

    with pytest.raises(OSError):
        fetch_history("BTCUSDT", 0, 10, request=fail, now_ms=20)


def test_checksum_and_coverage_prevent_changed_or_partial_evidence():
    history = fetch_history("BTCUSDT", 0, 10, request=lambda _: [row(5)], now_ms=20)
    with pytest.raises(ValueError, match="coverage"):
        funding_paid(history, [{"entry_ms": 0, "exit_ms": 11, "quantity": "1"}], "long")
    history["rows"][0]["rate"] = "0"
    with pytest.raises(ValueError, match="checksum"):
        funding_paid(history, [{"entry_ms": 0, "exit_ms": 10, "quantity": "1"}], "long")
