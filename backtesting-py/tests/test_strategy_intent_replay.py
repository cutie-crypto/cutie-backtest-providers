import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from decimal import Decimal as D

import pytest

from signal_lifecycle_kernel import Candle
from strategy_cycle_replay import replay_cycle
from strategy_entry_evaluators import Bar
from strategy_intent_replay import generate_intents


def policy():
    return {
        "schema": "cutie.strategy_signal_execution.v1",
        "reference_price": "signal_bar_close",
        "entry_mode": "limit_only",
        "observation_timeframe": "5m",
        "evaluation_lag_seconds": 30,
        "indicator_history_bars": 4,
        "sl_tp_rule": {
            "stop_loss": {"type": "fixed_pct", "pct": "5"},
            "take_profit": {"type": "fixed_pct", "pct": "10"},
        },
    }


def bars(closes):
    return [Bar(i * 300, (i + 1) * 300, c, c + 1, c - 1, c) for i, c in enumerate(closes)]


def generate(closes, **overrides):
    args = dict(
        bars=bars(closes),
        evaluator="ema_cross",
        params={"ema_fast": 2, "ema_slow": 3},
        direction="long",
        leverage=1,
        symbol="BTCUSDT",
        execution_policy=policy(),
        start_at=1200,
    )
    return generate_intents(**{**args, **overrides})


def test_real_ema_edge_creates_priced_limit_intent_after_lag():
    result = generate([100, 101, 100, 99, 110, 112, 114, 90])
    entry = next(r for r in result if r["kind"] == "entry")
    assert entry["at"] == 1530
    assert entry["signal"]["entry_price"] == D(110)
    assert entry["signal"]["stop_loss"] == D("104.5")
    assert entry["signal"]["target_prices"] == [D(121)]
    assert entry["signal"]["filled_position_pct"] == 0
    assert len([r for r in result if r["kind"] == "entry"]) == 1
    assert result[-1]["kind"] == "rule_exit"


def test_first_enabled_bar_only_initializes_edge_state():
    assert not any(r["kind"] == "entry" for r in generate([103, 102, 100, 110, 111]))


def test_roc_short_direction_is_explicitly_rejected_not_silently_empty():
    """Codex review 返修（P3 方向守卫）：ROC 是 long-only（对齐 cci_rsi/breakout 的显式
    方向守卫），direction="short" 必须报错，不能静默产出空信号列表。"""
    with pytest.raises(ValueError, match="long-only"):
        generate(
            [100] * 13 + [110] * 5,
            evaluator="roc",
            params={"roc_period": 12, "entry_threshold": 5, "exit_threshold": 0},
            direction="short",
        )


def test_missing_prehistory_is_not_silently_reseeded():
    with pytest.raises(ValueError, match="prehistory"):
        generate([100, 100, 100, 110], start_at=600)


def test_generated_intent_waits_for_fill_and_settles_sl_before_rule_exit():
    source = bars([100, 101, 100, 99, 110, 112, 114, 90])
    source[6] = Bar(1800, 2100, 114, 115, 109, 114)
    source[7] = Bar(2100, 2400, 114, 115, 89, 90)
    intents = generate_intents(
        bars=source,
        evaluator="ema_cross",
        params={"ema_fast": 2, "ema_slow": 3},
        direction="long",
        leverage=1,
        symbol="BTCUSDT",
        execution_policy=policy(),
        start_at=1200,
    )
    candles = [Candle(b.open_time, b.close_time - 1, D(b.open), D(b.high), D(b.low), D(b.close)) for b in source]
    candles.append(Candle(2400, 2699, D(90), D(91), D(89), D(90)))
    result = replay_cycle(
        intents=intents,
        candles=candles,
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
    assert len(result["settlements"]) == 1
    assert result["settlements"][0]["net_return"] == "-0.05"
    assert result["open_signal"] is None
    assert result["outcomes"][-1]["outcome"] == "no_position"
