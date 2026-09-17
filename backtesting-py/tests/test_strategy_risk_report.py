import sys
from pathlib import Path
from decimal import Decimal as D

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from strategy_risk_report import build_risk_report
from canonical_json import canonical_json_sha256
from funding_history import fetch_history

POLICY = {"schema": "cutie.strategy_risk_policy.v1", "direction": "long", "leverage": 1}


def result(pnls):
    return {
        "trades": [
            {
                "seq": i + 1,
                "side": "long",
                "opened_at": i * 10,
                "closed_at": i * 10 + 5,
                "qty": "1",
                "entry_price": "100",
                "pnl": str(p),
            }
            for i, p in enumerate(pnls)
        ]
    }


def report(pnls):
    return build_risk_report(
        result(pnls),
        POLICY,
        market="spot",
        symbol="BTCUSDT",
        exchange="binance",
        fee_bps=D(10),
        slippage_bps=D(5),
    )


def test_fifth_loss_stops_entire_trade_sequence_and_hash_binds_base():
    r = report([-1] * 7)
    assert len(r["payload"]["trades"]) == 5
    assert r["payload"]["trades"][2]["warn_author"]
    assert r["payload"]["skipped_trade_count"] == 2
    assert r["payload"]["pause_reason"] == "consecutive_losses"
    assert r["sha256"] == canonical_json_sha256(r["payload"])
    assert r["sha256"] != report([-1] * 6 + [-2])["sha256"]


def test_tiny_win_cannot_reset_drawdown_and_zero_does_not_reset_losses():
    assert (
        report([-8, 0.001, -8, 0.001, -8])["payload"]["pause_reason"] == "net_drawdown"
    )
    assert (
        report([-1, 0, -1, 0, -1, 0, -1, 0, -1])["payload"]["pause_reason"]
        == "consecutive_losses"
    )


def test_funding_credit_and_cost_use_same_full_trade_window():
    def loader(symbol, start, end):
        return fetch_history(
            symbol,
            start,
            end,
            now_ms=end + 1,
            request=lambda _: [
                {
                    "symbol": symbol,
                    "fundingTime": end,
                    "fundingRate": ".02",
                    "markPrice": "100",
                }
            ],
        )

    r = build_risk_report(
        result([1]),
        POLICY,
        market="futures",
        symbol="BTCUSDT",
        exchange="binance",
        fee_bps=D(10),
        slippage_bps=D(5),
        history_loader=loader,
    )
    assert r["payload"]["trades"][0]["net_return"] == "-0.01"


def test_mixed_direction_and_wrong_funding_venue_are_rejected():
    mixed = result([1, 1])
    mixed["trades"][1]["side"] = "short"
    with pytest.raises(ValueError, match="single-side"):
        build_risk_report(
            mixed,
            POLICY,
            market="spot",
            symbol="BTCUSDT",
            exchange="binance",
            fee_bps=D(10),
            slippage_bps=D(5),
        )
    with pytest.raises(ValueError, match="venue"):
        build_risk_report(
            result([1]),
            POLICY,
            market="futures",
            symbol="BTCUSDT",
            exchange="okx",
            fee_bps=D(10),
            slippage_bps=D(5),
        )
