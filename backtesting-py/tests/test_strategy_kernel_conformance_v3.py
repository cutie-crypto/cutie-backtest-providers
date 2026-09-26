"""123 组合策略 B1c: ``cutie.strategy_kernel_conformance.v3`` shared fixture
(SPEC_组合策略v3契约 §8).

Every case: the provider builder turns ``provider_params`` + ``envelope`` into
exactly ``spec`` (canonical bytes and ``expected_spec_hash``), and
``simulate_v3`` + ``build_result_v3`` over ``klines`` reproduce
``expected_result`` / ``expected_diagnostics`` -- fee/slippage/pnl and all
basket totals exactly, reference prices within 62-2 §8.4 ``price_abs``.

``HAND`` below re-states, per case, the semantics each case pins together
with the hand calculation; the fixture must agree with it, so a regenerated
fixture cannot silently drift from the hand-checked values.  Unless noted,
bar i opens at ``T + i*14400`` (T = 1720000000), leg a = ETHUSDT long,
leg b = BTCUSDT short, leverage 3, margin_per_leg 1000 (margin_total 2000),
fee_bps 10, slippage_bps 5, so ``qty_a = 3000/E_a``, ``qty_b = 3000/E_b``,
``fee_leg = (E+X)*qty*10/10000``, ``slippage_leg = (E+X)*qty*5/10000``.
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from canonical_json import canonical_json, canonical_json_sha256  # noqa: E402
from strategy_execution import build_data_manifests_v3, build_result_v3  # noqa: E402
from strategy_kernel import compile_strategy_v3, simulate_v3  # noqa: E402
from strategy_spec_v3_builder import build_strategy_spec_v3  # noqa: E402

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "strategy_kernel_conformance_v3.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
CASES = {case["case_id"]: case for case in FIXTURE["cases"]}

CASE_KEYS = [
    "case_id",
    "description",
    "provider_params",
    "envelope",
    "expected_spec_hash",
    "spec",
    "instrument_rules",
    "klines",
    "initial_capital",
    "expected_result",
    "expected_diagnostics",
]
REQUIRED_CASES = [
    "sma_cross_basic",
    "unaligned_bar_skipped",
    "no_next_bar_drop",
    "basket_stop_on_close",
    "basket_take_priority_over_signal",
    "leg_min_order_reject",
    "roc_basic",
    "zscore_basic",
    "sums_exact",
    "exit_candidate_across_gap",
    "warmup_history",
]
# 62-2 §8.4 recommended tolerance: price_abs per symbol, price_relative_ppm 0.
PRICE_ABS_BY_SYMBOL = {"BTCUSDT": Decimal("0.01"), "ETHUSDT": Decimal("0.01")}
PRICE_KEYS = {"entry_price", "exit_price"}
# §8 case keys carry no tool_id: the family is fixed by the family-specific
# parameter set (§6.1), which is disjoint across the three tools.
FAMILY_BY_PARAM_KEYS = {
    frozenset({"fast_window", "slow_window"}): "basket_ratio_sma_cross",
    frozenset({"roc_window", "entry_threshold", "exit_threshold"}): "basket_ratio_roc",
    frozenset({"zscore_window", "entry_z", "exit_z"}): "basket_ratio_zscore",
}
COMMON_PARAM_KEYS = {
    "legs",
    "leverage",
    "margin_per_leg",
    "basket_stop_loss_pct",
    "basket_take_profit_pct",
    "cooldown_bars",
    "time_exit_bars",
}

H4 = 14400
T = 1720000000

# Per case: pinned semantics.  ``trades`` lists (opened_at, closed_at,
# exit_kind, fee, slippage, pnl); ``legs`` is the first trade's per-leg
# (qty, fee, slippage, pnl).
HAND = {
    # sma_cross_basic (§2.7 spec, §3 example; bar i opens 1720000000+(i-21)*H4,
    # b close 60000 until bar 23).  ratio = a_close/60000:
    #   bars 0-19 a 2994 -> r 0.0499; bar 20 a 3000 -> 0.05;
    #   bar 20: fast=(4*0.0499+0.05)/5=0.04992 > slow=(19*0.0499+0.05)/20=0.049905,
    #           bar 19 fast=slow -> crosses_above, fill bar 21 open (3000/60000).
    #   bar 21 (r 0.05): fast 0.04994 >= slow 0.04991, pnl 0 -> no exit.
    #   bar 22 a 2970 (r 0.0495): fast 0.04986 < slow 0.04989 -> crosses_below;
    #           basket pnl (2970-3000)*1/2000 = -0.015 (no stop).
    #   bar 23 open a 3090 / b 59400 closes both.  §3 arithmetic:
    #   a qty 1000*3/3000=1, gross 90, fee 6090*1*10/10000=6.09, slip 3.045,
    #     pnl 80.865; b qty 3000/60000=0.05, gross 600*0.05=30,
    #     fee 119400*0.05/1000=5.97, slip 2.985, pnl 21.045; basket 101.91.
    "sma_cross_basic": {
        "skipped_bars": 0,
        "diagnostics": [],
        "trades": [(1720000000, 1720028800, "signal_exit", "12.06", "6.03", "101.91")],
        "legs": [("1", "6.09", "3.045", "80.865"), ("0.05", "5.97", "2.985", "21.045")],
        "equity_curve": [(1719000000, "10000"), (1720028800, "10101.91")],
    },
    # unaligned_bar_skipped (roc window 2, entry roc>0.02, exit roc<0,
    # time_exit_bars 4; b missing bar 4).  Aligned bars: 0,1,2,3,5,6,7.
    #   bar 2: roc=(3000-2940)/2940=0.0204>0.02 -> fill bar 3 open a 3000 b 60000.
    #   bar 4 skipped (skipped_bars=1, basket carries over).
    #   bar 5 (aligned idx 4): roc vs aligned idx 2 (bar 2, 3000): 3030/3000-1
    #     =0.01 >= 0 -> no signal (grid lag would compare bar 3 3060 -> <0).
    #   bar 6: roc vs bar 3 (3060): 0 -> no signal.
    #   held bars counted on aligned bars: 3=1, 5=2, 6=3, 7=4 -> time_exit at
    #     bar 7 close (a 3030), closed_at = bar 7 close = T+8*H4.
    #   a: gross 30, fee 6030/1000=6.03, slip 3.015, pnl 20.955;
    #   b: gross 0, fee 120000*0.05/1000=6, slip 3, pnl -9; basket 11.955.
    "unaligned_bar_skipped": {
        "skipped_bars": 1,
        "diagnostics": [],
        "trades": [(T + 3 * H4, T + 8 * H4, "time_exit", "12.03", "6.015", "11.955")],
        "legs": [("1", "6.03", "3.015", "20.955"), ("0.05", "6", "3", "-9")],
        "equity_curve": [(T, "10000"), (T + 8 * H4, "10011.955")],
    },
    # no_next_bar_drop: bar 2 roc 0.0204 -> pending; a missing bar 3 ->
    #   pending dropped, diagnostic at the signal bar (bar 2); bar 4 roc vs
    #   bar 1 = 50/2940 = 0.017, bar 5 roc vs bar 2 < 0 -> no further entry.
    "no_next_bar_drop": {
        "skipped_bars": 1,
        "diagnostics": [{"bar_open_at": T + 2 * H4, "kind": "no_next_bar"}],
        "trades": [],
        "legs": [],
        "equity_curve": [(T, "10000")],
    },
    # basket_stop_on_close: bar 2 roc=(2900-2800)/2800>0.02 -> fill bar 3
    #   open a 3000 b 60000 (qty 1 / 0.05).  bar 3 a low 2800 (intrabar
    #   -0.1) but close 3000 -> pnl 0, no stop.  bar 4 close a 2960, b 61200:
    #   (2960-3000)*1 + (60000-61200)*0.05 = -40-60 = -100, /2000 = -0.05
    #   <= -0.05 -> stop candidate (roc bar 4 = 2960/61200 vs 2900/60000 >= 0,
    #   no signal).  bar 5 open a 2950 b 61000:
    #   a gross -50, fee 5950/1000=5.95, slip 2.975, pnl -58.925;
    #   b gross (60000-61000)*0.05=-50, fee 121000*0.05/1000=6.05,
    #     slip 3.025, pnl -59.075; basket -118, max_drawdown 118/10000.
    "basket_stop_on_close": {
        "skipped_bars": 0,
        "diagnostics": [],
        "trades": [(T + 3 * H4, T + 5 * H4, "stop_loss", "12", "6", "-118")],
        "legs": [("1", "5.95", "2.975", "-58.925"), ("0.05", "6.05", "3.025", "-59.075")],
        "equity_curve": [(T, "10000"), (T + 5 * H4, "9882")],
    },
    # basket_take_priority_over_signal (exit_threshold 0.019, cooldown 1):
    #   bar 2 roc=(3150-3000)/3000=0.05 -> fill bar 3 open 3000.
    #   bar 3 close 3100: roc vs bar 1 = 0.033 >= 0.019, pnl 100/2000=0.05.
    #   bar 4 close 3200: roc vs bar 2 = 50/3150 = 0.0159 < 0.019 (signal
    #   candidate) and pnl 200/2000 = 0.1 >= 0.1 (take candidate) ->
    #   priority stop,take,time,signal picks take_profit; bar 5 open a 3250:
    #   a gross 250, fee 6250/1000=6.25, slip 3.125, pnl 240.625;
    #   b pnl -9; basket 231.625.  cooldown 1 blocks re-entry on the exit bar.
    "basket_take_priority_over_signal": {
        "skipped_bars": 0,
        "diagnostics": [],
        "trades": [(T + 3 * H4, T + 5 * H4, "take_profit", "12.25", "6.125", "231.625")],
        "legs": [("1", "6.25", "3.125", "240.625"), ("0.05", "6", "3", "-9")],
        "equity_curve": [(T, "10000"), (T + 5 * H4, "10231.625")],
    },
    # leg_min_order_reject (leg b qty_step 0.01, min_qty 0.1): bar 2 signal,
    #   bar 3 open fill: qty_a = 3000/3000 = 1 ok, qty_b = floor(0.05/0.01)
    #   *0.01 = 0.05 < 0.1 -> whole basket rejected, diagnostic at the fill
    #   bar with legs [b]; bar 3 roc 50/2940 and bar 4 roc 0 -> no re-entry.
    "leg_min_order_reject": {
        "skipped_bars": 0,
        "diagnostics": [{"bar_open_at": T + 3 * H4, "kind": "leg_min_order", "legs": ["b"]}],
        "trades": [],
        "legs": [],
        "equity_curve": [(T, "10000")],
    },
    # roc_basic (window 3, entry > 0.05, exit < 0.01):
    #   bar 3 ratio 3100/58000 vs bar 0 0.05: roc 0.0690 -> fill bar 4 open
    #   a 3100 b 58000: qty_a = floor(3000/3100 = 0.9677) = 0.967,
    #   qty_b = floor(3000/58000 = 0.05172) = 0.051.
    #   bar 4 roc 0.0957, bar 5 roc 0.0796 (>= 0.01); pnl 0.0369 / 0.0148.
    #   bar 6 ratio 3100/58400 vs bar 3 3100/58000: roc -0.0068 < 0.01 ->
    #   signal; pnl (58000-58400)*0.051/2000 = -0.0102.  bar 7 open a 3090 b 58500:
    #   a gross -10*0.967=-9.67, fee 6190*0.967/1000=5.98573,
    #     slip 2.992865, pnl -18.648595;
    #   b gross -500*0.051=-25.5, fee 116500*0.051/1000=5.9415,
    #     slip 2.97075, pnl -34.41225; basket fee 11.92723,
    #     slip 5.963615, pnl -53.060845.
    "roc_basic": {
        "skipped_bars": 0,
        "diagnostics": [],
        "trades": [
            (T + 4 * H4, T + 7 * H4, "signal_exit", "11.92723", "5.963615", "-53.060845")
        ],
        "legs": [
            ("0.967", "5.98573", "2.992865", "-18.648595"),
            ("0.051", "5.9415", "2.97075", "-34.41225"),
        ],
        "equity_curve": [(T, "10000"), (T + 7 * H4, "9946.939155")],
    },
    # zscore_basic (window 10, entry z < -2, exit z > -0.3), b flat 60000:
    #   bars 0-9 ratio 0.05 -> bar 9 stdev 0 -> missing (not 0).
    #   bar 10 ratio 0.049 (d=0.001): mean 0.0499, pop var
    #     (9*0.0001^2+0.0009^2)/10 = 9e-8, stdev 0.0003, z = -0.0009/0.0003 = -3
    #     < -2 -> fill bar 11 open a 2940: qty_a = floor(1.0204) = 1.02.
    #   bars 11-18 z = -2, -1.53, -1.22, -1, -0.82, -0.65, -0.5, -1/3 (none > -0.3).
    #   bar 19: window all 0.049 -> stdev 0 -> missing: signal_exit cannot
    #     fire (had it been 0, 0 > -0.3 would exit at bar 20 open 2940).
    #   bar 20 ratio 0.05: z = +3 > -0.3 -> exit bar 21 open a 3000:
    #   a gross 60*1.02=61.2, fee 5940*1.02/1000=6.0588, slip 3.0294,
    #     pnl 52.1118; b pnl -9; basket 43.1118.
    "zscore_basic": {
        "skipped_bars": 0,
        "diagnostics": [],
        "trades": [(T + 11 * H4, T + 21 * H4, "signal_exit", "12.0588", "6.0294", "43.1118")],
        "legs": [("1.02", "6.0588", "3.0294", "52.1118"), ("0.05", "6", "3", "-9")],
        "equity_curve": [(T, "10000"), (T + 21 * H4, "10043.1118")],
    },
    # sums_exact (sma 2/5, a short / b long, leverage 2, margin 1234.5,
    #   fee_bps 7.5, slippage_bps 2.5, stop 0.2, take 0.3):
    #   bars 0-4 ratio 3000/50000=0.06; bar 5 a 3060 -> 0.0612: fast 0.0606 >
    #   slow 0.06024 -> crosses_above; fill bar 6 open a 3071.37 b 49876.3:
    #   qty_a = floor(2469/3071.37=0.8039) = 0.803,
    #   qty_b = floor(2469/49876.3=0.04950) = 0.049.
    #   bar 7 a 2900 b 50100: fast 0.059842 < slow 0.0601768 -> crosses_below
    #   (pnl +0.0602 < take 0.3); bar 8 open a 2911.07 b 50123.9:
    #   a gross (3071.37-2911.07)*0.803 = 128.7209,
    #     fee 5982.44*0.803*7.5/10000 = 3.60292449, slip 1.20097483,
    #     pnl 123.91700068;
    #   b gross (50123.9-49876.3)*0.049 = 12.1324,
    #     fee 100000.2*0.049*7.5/10000 = 3.67500735, slip 1.22500245,
    #     pnl 7.2323902;
    #   basket fee 3.60292449+3.67500735 = 7.27793184,
    #     slip 1.20097483+1.22500245 = 2.42597728,
    #     pnl 123.91700068+7.2323902 = 131.14939088 (exact Decimal sums).
    "sums_exact": {
        "skipped_bars": 0,
        "diagnostics": [],
        "trades": [
            (T + 6 * H4, T + 8 * H4, "signal_exit", "7.27793184", "2.42597728", "131.14939088")
        ],
        "legs": [
            ("0.803", "3.60292449", "1.20097483", "123.91700068"),
            ("0.049", "3.67500735", "1.22500245", "7.2323902"),
        ],
        "equity_curve": [(T, "10000"), (T + 8 * H4, "10131.14939088")],
    },
    # exit_candidate_across_gap (roc window 2, exit roc < 0; b missing bar 5):
    #   bar 2 signal -> fill bar 3 open a 3000; bar 4 close 2970: roc vs bar 2
    #   (3000) = -0.01 < 0 -> signal candidate (pnl -0.015); bar 5 skipped,
    #   candidate kept; bar 6 (next aligned) open a 2985 closes both,
    #   closed_at = bar 6 open:
    #   a gross -15, fee 5985/1000=5.985, slip 2.9925, pnl -23.9775;
    #   b pnl -9; basket -32.9775.
    "exit_candidate_across_gap": {
        "skipped_bars": 1,
        "diagnostics": [],
        "trades": [(T + 3 * H4, T + 6 * H4, "signal_exit", "11.985", "5.9925", "-32.9775")],
        "legs": [("1", "5.985", "2.9925", "-23.9775"), ("0.05", "6", "3", "-9")],
        "equity_curve": [(T, "10000"), (T + 6 * H4, "9967.0225")],
    },
    # warmup_history (start_at = bar 4; b missing warmup bar 1):
    #   aligned bars 0,2,3,4,5,6.  bar 3 (warmup) roc vs bar 0: 3000/2940-1
    #   = 0.0204 would fire but warmup is never evaluated (else fill at bar 4).
    #   bar 4 (first evaluated) roc vs aligned idx-2 = bar 2 (2940, warmup
    #   history) = 0.0204 -> fill bar 5 open a 3000.  Warmup gap: not counted.
    #   end_of_data at bar 6 close a 3060, closed_at = T+7*H4:
    #   a gross 60, fee 6060/1000=6.06, slip 3.03, pnl 50.91; b pnl -9;
    #   basket 41.91.  Equity curve starts at start_at; data_manifests
    #   count bars 4-6 only (3 per leg).
    "warmup_history": {
        "skipped_bars": 0,
        "diagnostics": [],
        "trades": [(T + 5 * H4, T + 7 * H4, "end_of_data", "12.06", "6.03", "41.91")],
        "legs": [("1", "6.06", "3.03", "50.91"), ("0.05", "6", "3", "-9")],
        "equity_curve": [(T + 4 * H4, "10000"), (T + 7 * H4, "10041.91")],
    },
}


def _family(params: dict) -> str:
    return FAMILY_BY_PARAM_KEYS[frozenset(set(params) - COMMON_PARAM_KEYS)]


def _replay(case: dict) -> tuple[dict, list]:
    envelope = case["envelope"]
    simulation = simulate_v3(
        compile_strategy_v3(case["spec"]),
        case["klines"],
        case["instrument_rules"],
        case["initial_capital"],
        start_at=envelope["start_at"],
        end_at=envelope["end_at"],
    )
    manifests = build_data_manifests_v3(
        legs=case["spec"]["market"]["legs"],
        leg_klines=case["klines"],
        source="binance_futures",
        market=case["spec"]["market"]["market_type"],
        timeframe=envelope["timeframe"],
        start_at=envelope["start_at"],
        end_at=envelope["end_at"],
    )
    return build_result_v3(simulation=simulation, data_manifests=manifests), simulation[
        "diagnostics"
    ]


def _assert_result_matches(actual: dict, expected: dict) -> None:
    """Exact equality except per-leg reference prices (62-2 §8.4 price_abs)."""
    assert list(actual) == list(expected)
    assert len(actual["trades"]) == len(expected["trades"])
    for got, want in zip(actual["trades"], expected["trades"]):
        assert {k: v for k, v in got.items() if k != "legs"} == {
            k: v for k, v in want.items() if k != "legs"
        }
        assert len(got["legs"]) == len(want["legs"])
        for got_leg, want_leg in zip(got["legs"], want["legs"]):
            assert set(got_leg) == set(want_leg)
            for key, value in want_leg.items():
                if key in PRICE_KEYS:
                    allowed = PRICE_ABS_BY_SYMBOL[want_leg["symbol"]]
                    assert abs(Decimal(got_leg[key]) - Decimal(value)) <= allowed, key
                else:
                    assert got_leg[key] == value, key
    for key in ("schema_version", "equity_curve", "metrics", "data_manifests"):
        assert actual[key] == expected[key], key


def test_fixture_top_level_and_case_set():
    assert list(FIXTURE) == ["schema", "fixture_version", "cases"]
    assert FIXTURE["schema"] == "cutie.strategy_kernel_conformance.v3"
    assert FIXTURE["fixture_version"] == "1"
    assert [case["case_id"] for case in FIXTURE["cases"]] == REQUIRED_CASES
    for case in FIXTURE["cases"]:
        assert list(case) == CASE_KEYS, case["case_id"]
        assert list(case["envelope"]) == [
            "timeframe",
            "fee_bps",
            "slippage_bps",
            "start_at",
            "end_at",
        ]
        assert set(case["instrument_rules"]) == {"a", "b"}
        assert set(case["klines"]) == {"a", "b"}
        assert all(len(rows) <= 40 for rows in case["klines"].values())
        assert isinstance(case["description"], str) and case["description"]


def test_fixture_file_is_in_the_stable_byte_format():
    # Copied verbatim to TokenBeep docs: indent 2, fixed key order, UTF-8,
    # trailing newline -- re-serialising must reproduce the file byte for byte.
    raw = FIXTURE_PATH.read_bytes()
    assert raw == (json.dumps(FIXTURE, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


@pytest.mark.parametrize("case_id", REQUIRED_CASES)
def test_builder_reproduces_spec_bytes_and_hash(case_id):
    case = CASES[case_id]
    envelope = {key: case["envelope"][key] for key in ("timeframe", "fee_bps", "slippage_bps")}
    built = build_strategy_spec_v3(_family(case["provider_params"]), case["provider_params"], envelope)
    assert canonical_json(built) == canonical_json(case["spec"])
    assert canonical_json_sha256(built) == case["expected_spec_hash"]
    assert canonical_json_sha256(case["spec"]) == case["expected_spec_hash"]


@pytest.mark.parametrize("case_id", REQUIRED_CASES)
def test_kernel_replay_matches_expected_result(case_id):
    case = CASES[case_id]
    result, diagnostics = _replay(case)
    _assert_result_matches(result, case["expected_result"])
    assert diagnostics == case["expected_diagnostics"]


@pytest.mark.parametrize("case_id", REQUIRED_CASES)
def test_fixture_agrees_with_hand_calculation(case_id):
    case = CASES[case_id]
    hand = HAND[case_id]
    expected = case["expected_result"]
    assert expected["metrics"]["skipped_bars"] == hand["skipped_bars"]
    assert expected["metrics"]["trade_count"] == len(hand["trades"])
    assert case["expected_diagnostics"] == hand["diagnostics"]
    assert [
        (t["opened_at"], t["closed_at"], t["exit_kind"], t["fee"], t["slippage"], t["pnl"])
        for t in expected["trades"]
    ] == hand["trades"]
    if hand["legs"]:
        assert [
            (leg["qty"], leg["fee"], leg["slippage"], leg["pnl"])
            for leg in expected["trades"][0]["legs"]
        ] == hand["legs"]
    assert [(p["ts"], p["equity"]) for p in expected["equity_curve"]] == hand["equity_curve"]
    for trade in expected["trades"]:
        for key in ("fee", "slippage", "pnl"):
            assert Decimal(trade[key]) == sum(
                (Decimal(leg[key]) for leg in trade["legs"]), Decimal(0)
            )


def test_warmup_history_manifests_cover_the_evaluation_window_only():
    case = CASES["warmup_history"]
    manifests = case["expected_result"]["data_manifests"]
    start_at = case["envelope"]["start_at"]
    assert [m["kline_count"] for m in manifests] == [3, 3]
    assert all(m["start_at"] == start_at for m in manifests)
    assert any(row["open_time"] < start_at for row in case["klines"]["a"])
