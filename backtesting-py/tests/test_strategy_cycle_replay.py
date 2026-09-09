import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from decimal import Decimal as D

from signal_lifecycle_kernel import Candle
from strategy_cycle_replay import replay_cycle


def signal(at, sid):
    return {
        "id": sid,
        "signal_type": "crypto",
        "symbol": "BTCUSDT",
        "status": "published",
        "direction": "long",
        "leverage": 1,
        "entry_execution_mode": "limit_only",
        "created_at": at,
        "entry_price": D(100),
        "create_snapshot_price": D(100),
        "stop_loss": D(99),
        "target_prices": [D(110)],
        "filled_position_pct": D(0),
        "lifecycle_status": "awaiting_entry",
        "expires_at": at + 10000,
    }


def candle(at, low=100, high=100):
    return Candle(at, at + 299, D(low), D(high), D(low), D(high))


def run(intents, candles, **kw):
    params = dict(
        market="spot",
        symbol="BTCUSDT",
        direction="long",
        leverage=1,
        fee_bps=D(0),
        slippage_bps=D(0),
        daily_limit=10,
        author_daily_limit=10,
        cooldown_seconds=0,
        day_offset_seconds=0,
    )
    return replay_cycle(intents=intents, candles=candles, **{**params, **kw})


def test_fifth_loss_stops_sixth_intent_in_actual_chronological_replay():
    intents = [{"id": str(i), "kind": "entry", "at": i * 600, "signal": signal(i * 600, str(i))} for i in range(6)]
    candles = [candle(at, 98 if at % 600 else 100, 100) for at in range(0, 3600, 300)]
    result = run(intents, candles)
    assert len(result["settlements"]) == 5
    assert result["risk_state"]["entries_paused"]
    assert result["outcomes"][-1]["outcome"] == "risk_paused"
    assert result["risk_state"]["equity"] == D(".99") ** 5


def test_rule_exit_settles_before_later_entry_without_forced_liquidation():
    result = run(
        [
            {"id": "a", "kind": "entry", "at": 0, "signal": signal(0, "a")},
            {"id": "b", "kind": "rule_exit", "at": 350, "price": "101"},
            {"id": "c", "kind": "entry", "at": 400, "signal": signal(400, "c")},
        ],
        [candle(0), candle(300), candle(600)],
    )
    assert result["settlements"][0]["net_return"] == "0.01"
    assert len(result["settlements"]) == 1
    assert result["open_signal"]["id"] == "c"


def test_other_strategy_future_entries_do_not_consume_quota_early():
    result = run(
        [{"id": "a", "kind": "entry", "at": 0, "signal": signal(0, "a")}],
        [candle(0), candle(300)],
        external_entry_times=[500] * 10,
    )
    assert result["outcomes"][0]["outcome"] == "published"


def test_daily_author_limits_and_cooldown_apply_after_exit():
    intents = [
        {"id": "a", "kind": "entry", "at": 0, "signal": signal(0, "a")},
        {"id": "exit", "kind": "rule_exit", "at": 350, "price": "101"},
        {"id": "b", "kind": "entry", "at": 400, "signal": signal(400, "b")},
    ]
    for overrides, reason in [
        ({"daily_limit": 1}, "daily_limit_reached"),
        ({"author_daily_limit": 1}, "author_daily_limit_reached"),
        ({"cooldown_seconds": 600}, "cooldown"),
    ]:
        result = run(intents, [candle(0), candle(300), candle(600)], **overrides)
        assert result["outcomes"][-1]["outcome"] == reason
        assert len(result["settlements"]) == 1


def test_empty_intent_sequence_still_rejects_missing_observations():
    import pytest
    with pytest.raises(ValueError, match="contiguous"):
        run([], [candle(0), candle(600)])
