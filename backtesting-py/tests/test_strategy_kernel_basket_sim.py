"""123 组合策略 B1b: basket execution semantics ``simulate_v3`` and
``cutie.backtest_result.v3`` assembly (SPEC_组合策略v3契约 §2.6, §2.6.1, §3, §4)."""

from __future__ import annotations

import copy
import hashlib
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from canonical_json import canonical_json  # noqa: E402
from strategy_execution import (  # noqa: E402
    RESULT_V3_SCHEMA,
    build_data_manifests_v3,
    build_result_v3,
    data_manifests_hash,
    strategy_spec_v3_evidence,
)
from strategy_kernel import (  # noqa: E402
    ERR_COVERAGE_INCOMPLETE,
    StrategyContractError,
    compile_strategy_v3,
    simulate_v3,
)
from test_strategy_kernel_basket import (  # noqa: E402
    _dec,
    _feature,
    _stream,
    zscore_spec,
    example_spec,
)

H4 = 14400
# §3 example: the basket fills at bar 1 (1720000000) and exits at bar 3 open.
T0 = 1720000000 - H4
START_AT = 1719000000
RULES = {"price_tick": "0.01", "qty_step": "0.001", "min_qty": "0.001", "min_notional": "5"}


def signal_spec(**changes) -> dict:
    """Basket spec whose entry/signal_exit are driven by leg volumes, so a
    test places each decision on an exact bar: entry fires on a bar where
    leg a volume > 5, signal_exit where leg b volume > 5."""
    spec = example_spec()
    spec["features"] = [
        {
            "key": "entry_sig",
            "kind": "derived",
            "expr": _stream("a", "volume"),
            "interval": "4h",
            "value_kind": "flow",
            "output_type": "decimal",
        },
        {
            "key": "exit_sig",
            "kind": "derived",
            "expr": _stream("b", "volume"),
            "interval": "4h",
            "value_kind": "flow",
            "output_type": "decimal",
        },
    ]
    spec["entry"]["condition"] = {
        "node": "compare",
        "op": "gt",
        "left": _feature("entry_sig"),
        "right": _dec("5"),
    }
    spec["entry"]["cooldown_bars"] = changes.pop("cooldown_bars", 0)
    spec["exit"]["signal_exit"] = {
        "node": "compare",
        "op": "gt",
        "left": _feature("exit_sig"),
        "right": _dec("5"),
    }
    spec["exit"]["time_exit_bars"] = changes.pop("time_exit_bars", None)
    assert not changes
    return spec


def bar(index: int, open_: str, close: str, volume: str = "1") -> dict:
    high = max(Decimal(open_), Decimal(close))
    low = min(Decimal(open_), Decimal(close))
    return {
        "open_time": T0 + index * H4,
        "open": open_,
        "high": str(high),
        "low": str(low),
        "close": close,
        "volume": volume,
    }


def leg(prices: list, volumes: dict | None = None, missing: tuple = ()) -> list[dict]:
    """``prices[i]`` is a flat price or an ``(open, close)`` pair for bar i."""
    volumes = volumes or {}
    rows = []
    for index, price in enumerate(prices):
        if index in missing:
            continue
        open_, close = price if isinstance(price, tuple) else (price, price)
        rows.append(bar(index, open_, close, volumes.get(index, "1")))
    return rows


def rules(a: dict | None = None, b: dict | None = None) -> dict:
    return {
        "a": {**RULES, "symbol": "ETHUSDT", **(a or {})},
        "b": {**RULES, "symbol": "BTCUSDT", **(b or {})},
    }


def run(spec: dict, a_rows: list, b_rows: list, instrument_rules=None) -> dict:
    last_close = max(row["open_time"] for row in a_rows + b_rows) + H4
    return simulate_v3(
        compile_strategy_v3(spec),
        {"a": a_rows, "b": b_rows},
        instrument_rules or rules(),
        "10000",
        start_at=START_AT,
        end_at=last_close,
    )


# ------------------------------------------------------ §3 hand-computed case


SECTION_3_TRADE = {
    "seq": 1,
    "opened_at": 1720000000,
    "closed_at": 1720028800,
    "fee": "12.06",
    "slippage": "6.03",
    "pnl": "101.91",
    "exit_kind": "signal_exit",
    "legs": [
        {
            "leg_id": "a",
            "symbol": "ETHUSDT",
            "side": "long",
            "qty": "1",
            "entry_price": "3000",
            "exit_price": "3090",
            "fee": "6.09",
            "slippage": "3.045",
            "pnl": "80.865",
        },
        {
            "leg_id": "b",
            "symbol": "BTCUSDT",
            "side": "short",
            "qty": "0.05",
            "entry_price": "60000",
            "exit_price": "59400",
            "fee": "5.97",
            "slippage": "2.985",
            "pnl": "21.045",
        },
    ],
}


def section_3_rows() -> tuple[list, list]:
    # bar 0: entry signal; bar 1 open fills (3000 / 60000); bar 2: signal_exit;
    # bar 3 open exits (3090 / 59400).  Closes of bars 1-2 keep basket pnl 0.
    a_rows = leg(["3000", "3000", "3000", ("3090", "3090")], {0: "10"})
    b_rows = leg(["60000", "60000", "60000", ("59400", "59400")], {2: "10"})
    return a_rows, b_rows


def test_section_3_example_is_reproduced_field_by_field():
    spec = signal_spec()
    a_rows, b_rows = section_3_rows()
    end_at = T0 + 4 * H4
    plan = compile_strategy_v3(spec)
    simulation = simulate_v3(
        plan, {"a": a_rows, "b": b_rows}, rules(), "10000", start_at=START_AT, end_at=end_at
    )
    manifests = build_data_manifests_v3(
        legs=spec["market"]["legs"],
        leg_klines={"a": a_rows, "b": b_rows},
        source="binance_futures",
        market="futures",
        timeframe="4h",
        start_at=START_AT,
        end_at=end_at,
    )
    result = build_result_v3(simulation=simulation, data_manifests=manifests)

    assert list(result) == [
        "schema_version",
        "trades",
        "equity_curve",
        "metrics",
        "data_manifests",
    ]
    assert result["schema_version"] == "cutie.backtest_result.v3" == RESULT_V3_SCHEMA
    assert result["trades"] == [SECTION_3_TRADE]
    assert result["equity_curve"] == [
        {"ts": 1719000000, "equity": "10000"},
        {"ts": 1720028800, "equity": "10101.91"},
    ]
    assert result["metrics"] == {
        "total_return": "0.010191",
        "max_drawdown": "0",
        "trade_count": 1,
        "skipped_bars": 0,
    }

    # data_manifests: ten keys, one shared timeline, per-leg checksum that is
    # recomputed here from this test's own K-lines (§3 prints a placeholder).
    ten_keys = {
        "leg_id",
        "source",
        "symbol",
        "market",
        "timeframe",
        "start_at",
        "end_at",
        "kline_count",
        "checksum_algo",
        "checksum",
    }
    assert [item["leg_id"] for item in result["data_manifests"]] == ["a", "b"]
    for item, rows, symbol in zip(
        result["data_manifests"], (a_rows, b_rows), ("ETHUSDT", "BTCUSDT")
    ):
        assert set(item) == ten_keys
        assert item["symbol"] == symbol
        assert item["kline_count"] == 4
        assert item["checksum_algo"] == "sha256"
        canonical_rows = [
            {
                "open_time": row["open_time"],
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row["volume"],
            }
            for row in rows
        ]
        expected = hashlib.sha256(canonical_json(canonical_rows).encode()).hexdigest()
        assert item["checksum"] == expected
    first, second = result["data_manifests"]
    for key in ("start_at", "end_at", "timeframe", "market", "source"):
        assert first[key] == second[key]
    assert first["checksum"] != second["checksum"]
    assert data_manifests_hash(manifests) == hashlib.sha256(
        canonical_json(manifests).encode()
    ).hexdigest()

    evidence = strategy_spec_v3_evidence(spec)
    assert evidence["strategy_spec_json"] == canonical_json(spec)
    assert evidence["strategy_spec_hash"] == plan.spec_hash
    assert evidence["strategy_spec_hash"] == hashlib.sha256(
        canonical_json(spec).encode()
    ).hexdigest()


def test_result_v3_rejects_seq_gap_and_inexact_basket_sum():
    a_rows, b_rows = section_3_rows()
    simulation = run(signal_spec(), a_rows, b_rows)
    manifests = build_data_manifests_v3(
        legs=example_spec()["market"]["legs"],
        leg_klines={"a": a_rows, "b": b_rows},
        source="binance_futures",
        market="futures",
        timeframe="4h",
        start_at=START_AT,
        end_at=T0 + 4 * H4,
    )
    bad_seq = copy.deepcopy(simulation)
    bad_seq["trades"][0]["seq"] = 2
    with pytest.raises(StrategyContractError, match="seq"):
        build_result_v3(simulation=bad_seq, data_manifests=manifests)
    bad_sum = copy.deepcopy(simulation)
    bad_sum["trades"][0]["pnl"] = "101.9100000001"
    with pytest.raises(StrategyContractError, match="exact sum"):
        build_result_v3(simulation=bad_sum, data_manifests=manifests)
    bad_timeline = copy.deepcopy(manifests)
    bad_timeline[1]["end_at"] += H4
    with pytest.raises(StrategyContractError, match="timeline"):
        build_result_v3(simulation=simulation, data_manifests=bad_timeline)


# ---------------------------------------------------------- §2.6 behaviours


def test_unaligned_bar_is_skipped_and_the_basket_carries_over():
    # bar 2 lacks leg b: not evaluated (its volume-10 exit signal on leg b is
    # never seen), skipped_bars=1, the basket opened at bar 1 carries over and
    # exits on bar 3's signal at bar 4 open.
    a_rows = leg(["3000"] * 5, {0: "10"})
    b_rows = leg(["60000"] * 5, {2: "10", 3: "10"}, missing=(2,))
    result = run(signal_spec(), a_rows, b_rows)
    assert result["metrics"]["skipped_bars"] == 1
    assert result["unaligned_bars"] == [
        {"bar_open_at": T0 + 2 * H4, "missing_legs": ["b"]}
    ]
    [trade] = result["trades"]
    assert trade["opened_at"] == T0 + H4
    assert trade["closed_at"] == T0 + 4 * H4
    assert trade["exit_kind"] == "signal_exit"


def test_unaligned_bar_does_not_count_toward_time_exit():
    # Fill at bar 1 (bar 1 of the hold), bar 2 skipped, bars 3 and 4 are the
    # 2nd and 3rd aligned bars: time_exit_bars=3 closes at bar 4 close.
    a_rows = leg(["3000"] * 6, {0: "10"})
    b_rows = leg(["60000"] * 6, missing=(2,))
    [trade] = run(signal_spec(time_exit_bars=3), a_rows, b_rows)["trades"]
    assert trade["exit_kind"] == "time_exit"
    assert trade["closed_at"] == T0 + 5 * H4


def test_pending_entry_is_dropped_when_a_leg_lacks_t_plus_1():
    a_rows = leg(["3000"] * 4, {0: "10"}, missing=(1,))
    b_rows = leg(["60000"] * 4)
    result = run(signal_spec(), a_rows, b_rows)
    assert result["trades"] == []
    assert result["diagnostics"] == [{"bar_open_at": T0, "kind": "no_next_bar"}]
    assert result["metrics"]["trade_count"] == 0
    assert result["metrics"]["skipped_bars"] == 1


def test_pending_entry_on_the_last_bar_is_dropped_as_no_next_bar():
    a_rows = leg(["3000"] * 3, {2: "10"})
    b_rows = leg(["60000"] * 3)
    result = run(signal_spec(), a_rows, b_rows)
    assert result["trades"] == []
    assert result["diagnostics"] == [{"bar_open_at": T0 + 2 * H4, "kind": "no_next_bar"}]


def test_basket_stop_is_judged_on_close_and_fills_both_legs_at_t_plus_1_open():
    # qty a=1 (long), b=0.05 (short).  Bar 1 dips intrabar far below any stop
    # (no high/low touch).  Bar 2 close: a -50, b (60000-61000)*0.05 = -50,
    # basket = -100/2000 = -0.05 <= -stop -> stop at bar 3 open.
    a_rows = leg(["3000", "3000", ("3000", "2950"), ("2945", "2900")], {0: "10"})
    a_rows[1]["low"] = "100"
    b_rows = leg(["60000", "60000", ("60000", "61000"), ("60900", "61500")])
    [trade] = run(signal_spec(), a_rows, b_rows)["trades"]
    assert trade["exit_kind"] == "stop_loss"
    assert trade["opened_at"] == T0 + H4
    assert trade["closed_at"] == T0 + 3 * H4
    assert [item["exit_price"] for item in trade["legs"]] == ["2945", "60900"]


def test_take_and_signal_exit_on_the_same_bar_takes_take_profit():
    # Bar 2 close: a +150, b (60000-59000)*0.05 = +50 -> 200/2000 = 0.1 >= take;
    # leg b volume on bar 2 also fires signal_exit.  Priority picks take.
    a_rows = leg(["3000", "3000", ("3000", "3150"), ("3160", "3160")], {0: "10"})
    b_rows = leg(["60000", "60000", ("60000", "59000"), ("59100", "59100")], {2: "10"})
    [trade] = run(signal_spec(), a_rows, b_rows)["trades"]
    assert trade["exit_kind"] == "take_profit"
    assert trade["closed_at"] == T0 + 3 * H4
    assert [item["exit_price"] for item in trade["legs"]] == ["3160", "59100"]


def test_leg_below_min_order_rejects_the_whole_basket():
    a_rows = leg(["3000"] * 4, {0: "10"})
    b_rows = leg(["60000"] * 4)
    result = run(signal_spec(), a_rows, b_rows, rules(b={"min_qty": "0.1"}))
    assert result["trades"] == []
    assert result["diagnostics"] == [
        {"bar_open_at": T0 + H4, "kind": "leg_min_order", "legs": ["b"]}
    ]


def test_basket_totals_equal_the_exact_sum_of_legs():
    # Awkward prices: qty a = floor(3000/3333.33, 0.001) = 0.9;
    # qty b = floor(3000/65432.1, 0.001) = 0.045.
    a_rows = leg(["3333.33", "3333.33", "3333.33", ("3210.01", "3210.01")], {0: "10"})
    b_rows = leg(["65432.1", "65432.1", "65432.1", ("61234.57", "61234.57")], {2: "10"})
    [trade] = run(signal_spec(), a_rows, b_rows)["trades"]
    leg_a, leg_b = trade["legs"]
    assert (leg_a["qty"], leg_b["qty"]) == ("0.9", "0.045")
    for key in ("fee", "slippage", "pnl"):
        assert Decimal(trade[key]) == Decimal(leg_a[key]) + Decimal(leg_b[key])
    # Leg a by the v2 formula: (E+X)*qty*bps/10000, long gross (X-E)*qty.
    notional = (Decimal("3333.33") + Decimal("3210.01")) * Decimal("0.9")
    fee = notional * 10 / 10000
    slippage = notional * 5 / 10000
    pnl = (Decimal("3210.01") - Decimal("3333.33")) * Decimal("0.9") - fee - slippage
    assert (Decimal(leg_a["fee"]), Decimal(leg_a["slippage"]), Decimal(leg_a["pnl"])) == (
        fee,
        slippage,
        pnl,
    )


@pytest.mark.parametrize(
    "condition",
    [
        {"node": "compare", "op": "lt", "left": _feature("ratio_z"), "right": _dec("1")},
        {
            "node": "not",
            "arg": {
                "node": "compare",
                "op": "gt",
                "left": _feature("ratio_z"),
                "right": _dec("5"),
            },
        },
    ],
)
def test_zscore_with_zero_stdev_never_triggers(condition):
    # A constant ratio makes stdev 0: ratio_z is a gap, not 0, so a condition
    # that would fire on z=0 (and ``not`` over it) never enters.
    spec = zscore_spec(window=3)
    spec["entry"]["condition"] = condition
    result = run(spec, leg(["3000"] * 8), leg(["60000"] * 8))
    assert result["trades"] == []
    assert {gap["reason"] for gap in result["feature_gaps"] if gap["feature"] == "ratio_z"} == {
        "insufficient_history",
        "zero_stdev",
    }


def test_end_of_data_closes_at_the_last_aligned_bar_close():
    # Leg b has no bar 4: bar 4 is unaligned, so end_of_data uses bar 3 close.
    a_rows = leg(["3000", "3000", "3000", ("3010", "3020"), "3100"], {0: "10"})
    b_rows = leg(["60000", "60000", "60000", ("60100", "59900")])
    result = run(signal_spec(), a_rows, b_rows)
    [trade] = result["trades"]
    assert trade["exit_kind"] == "end_of_data"
    assert trade["closed_at"] == T0 + 4 * H4
    assert [item["exit_price"] for item in trade["legs"]] == ["3020", "59900"]
    assert result["metrics"]["skipped_bars"] == 1
    assert result["equity_curve"][-1]["ts"] == T0 + 4 * H4


def test_cooldown_counts_aligned_bars_only():
    # Exit at bar 3 open (aligned index 3, signal on bar 2).  cooldown_bars=2
    # and bar 4 is skipped: bar 5 is aligned index 4 (distance 1, not
    # eligible, though its grid distance is 2); bar 6 is aligned index 5
    # (distance 2), so only bar 6's entry signal fills, at bar 7 open.
    a_rows = leg(["3000"] * 8, {0: "10", 3: "10", 5: "10", 6: "10"})
    b_rows = leg(["60000"] * 8, {2: "10"}, missing=(4,))
    trades = run(signal_spec(cooldown_bars=2), a_rows, b_rows)["trades"]
    assert [(t["opened_at"], t["exit_kind"]) for t in trades] == [
        (T0 + H4, "signal_exit"),
        (T0 + 7 * H4, "end_of_data"),
    ]


def test_k_lines_outside_the_execution_window_are_rejected():
    a_rows, b_rows = section_3_rows()
    with pytest.raises(StrategyContractError) as exc:
        simulate_v3(
            compile_strategy_v3(signal_spec()),
            {"a": a_rows, "b": b_rows},
            rules(),
            "10000",
            start_at=START_AT,
            end_at=T0 + 3 * H4,
        )
    assert exc.value.code == ERR_COVERAGE_INCOMPLETE


# ------------------------------------------------------ §2.6.1 预热 (warmup)


def warmup_spec() -> dict:
    # Entry reads the previous aligned bar's leg-a volume, so the first
    # evaluated bar can only fire from warmup history.
    spec = signal_spec()
    spec["entry"]["condition"]["left"] = _feature("entry_sig", 1)
    return spec


def test_warmup_bars_feed_history_but_are_never_evaluated():
    # start_at = bar 3.  Bars 0-2 are warmup; leg b lacks bar 0 (a warmup
    # gap).  Bar 1 volume 10 makes the entry true at bar 2 (warmup: ignored,
    # otherwise the basket would open at bar 3); bar 2 volume 10 makes it
    # true at bar 3 through lag 1 into warmup history -> fill at bar 4 open.
    a_rows = leg(["3000"] * 7, {1: "10", 2: "10"})
    b_rows = leg(["60000"] * 7, missing=(0,))
    start_at = T0 + 3 * H4
    end_at = T0 + 7 * H4
    simulation = simulate_v3(
        compile_strategy_v3(warmup_spec()),
        {"a": a_rows, "b": b_rows},
        rules(),
        "10000",
        start_at=start_at,
        end_at=end_at,
    )
    assert [(t["opened_at"], t["exit_kind"]) for t in simulation["trades"]] == [
        (T0 + 4 * H4, "end_of_data")
    ]
    assert simulation["metrics"]["skipped_bars"] == 0
    assert simulation["equity_curve"][0] == {"ts": start_at, "equity": "10000"}
    assert simulation["diagnostics"] == []


def test_warmup_gap_is_not_counted_but_an_evaluated_gap_is():
    a_rows = leg(["3000"] * 6)
    b_rows = leg(["60000"] * 6, missing=(1, 4))
    simulation = simulate_v3(
        compile_strategy_v3(warmup_spec()),
        {"a": a_rows, "b": b_rows},
        rules(),
        "10000",
        start_at=T0 + 2 * H4,
        end_at=T0 + 6 * H4,
    )
    assert simulation["metrics"]["skipped_bars"] == 1


def test_data_manifests_exclude_warmup_rows_like_v2():
    # v2 data_manifest proves the evaluation window only: start_at/end_at are
    # the run window and kline_count/checksum cover start_at <= open_time <
    # end_at; v3 applies the same rule per leg.
    a_rows = leg(["3000"] * 7)
    b_rows = leg(["60000"] * 7, missing=(0,))
    start_at = T0 + 3 * H4
    end_at = T0 + 7 * H4
    manifests = build_data_manifests_v3(
        legs=example_spec()["market"]["legs"],
        leg_klines={"a": a_rows, "b": b_rows},
        source="binance_futures",
        market="futures",
        timeframe="4h",
        start_at=start_at,
        end_at=end_at,
    )
    assert [m["kline_count"] for m in manifests] == [4, 4]
    assert [(m["start_at"], m["end_at"]) for m in manifests] == [(start_at, end_at)] * 2
    evaluated = [row for row in a_rows if row["open_time"] >= start_at]
    assert manifests[0]["checksum"] == hashlib.sha256(
        canonical_json(
            [
                {
                    "open_time": row["open_time"],
                    **{k: str(Decimal(row[k])) for k in ("open", "high", "low", "close", "volume")},
                }
                for row in evaluated
            ]
        ).encode()
    ).hexdigest()


def test_bars_closing_after_end_at_are_still_rejected():
    a_rows = leg(["3000"] * 4)
    b_rows = leg(["60000"] * 4)
    with pytest.raises(StrategyContractError) as exc:
        simulate_v3(
            compile_strategy_v3(warmup_spec()),
            {"a": a_rows, "b": b_rows},
            rules(),
            "10000",
            start_at=T0 + H4,
            end_at=T0 + 3 * H4,
        )
    assert exc.value.code == ERR_COVERAGE_INCOMPLETE
