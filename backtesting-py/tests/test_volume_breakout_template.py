"""R1-T3：放量突破（volume_breakout）模板。

`tests/fixtures/volume_breakout_golden.json` 是逐 bar 手算 golden：
- ``expected_prior_high``：前 lookback 根 High 最大值，不含本根（与 BreakoutStrategy 同口径）；
- ``expected_volume_avg``：前 volume_avg_period 根 Volume 均值，不含本根；
- ``expected_exit_ema``：Close 的 EMA(exit_ema)，ewm(span, adjust=False)；
- ``expected_entries`` / ``expected_exits``：信号 bar 序号（真引擎成交在信号 bar + 1）。
入场：Close > 前高 且 Volume > 均量 x volume_multiple（均严格大于）；出场：Close < EMA。
golden 含「突破但量不够」（bar 5）与「放量但没突破」（bar 9）各一处。
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import pytest
from backtesting import Backtest

from cutie_backtesting_provider import (
    TOOL_SPECS,
    _build_volume_breakout,
    _validate_params_against_schema,
)

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "volume_breakout_golden.json"
TOOL = "local.backtesting_py.volume_breakout"


def _load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text())


def _frame(closes, highs, volumes) -> pd.DataFrame:
    return pd.DataFrame(
        dict(
            Open=closes,
            High=highs,
            Low=[c - 1 for c in closes],
            Close=closes,
            Volume=volumes,
        ),
        index=pd.date_range("2026-01-01", periods=len(closes), freq="h"),
    )


def _params(fx: dict) -> dict:
    return dict(
        lookback=fx["lookback"],
        volume_multiple=fx["volume_multiple"],
        volume_avg_period=fx["volume_avg_period"],
        exit_ema=fx["exit_ema"],
    )


def _hand_prior_high(highs, n):
    return [max(highs[i - n : i]) if i >= n else None for i in range(len(highs))]


def _hand_vol_avg(vols, n):
    return [sum(vols[i - n : i]) / n if i >= n else None for i in range(len(vols))]


def _hand_ema(closes, span):
    alpha = 2.0 / (span + 1)
    out = []
    for i, c in enumerate(closes):
        out.append(c if i == 0 else out[-1] + alpha * (c - out[-1]))
    return out


def _close_enough(got, want):
    if want is None:
        return got is None
    return got is not None and math.isclose(got, want, abs_tol=1e-9)


# ---------------------------------------------------------------------------
# 1. Golden.
# ---------------------------------------------------------------------------


def test_golden_indicators_match_hand_formula():
    fx = _load_fixture()
    ph = _hand_prior_high(fx["highs"], fx["lookback"])
    va = _hand_vol_avg(fx["volumes"], fx["volume_avg_period"])
    ema = _hand_ema(fx["closes"], fx["exit_ema"])
    n = len(fx["closes"])
    assert len(fx["expected_prior_high"]) == len(fx["expected_volume_avg"]) == len(fx["expected_exit_ema"]) == n
    for i in range(n):
        assert _close_enough(ph[i], fx["expected_prior_high"][i]), i
        assert _close_enough(va[i], fx["expected_volume_avg"][i]), i
        assert _close_enough(ema[i], fx["expected_exit_ema"][i]), i


def test_golden_entries_exits_from_hand_state_machine():
    fx = _load_fixture()
    closes, vols = fx["closes"], fx["volumes"]
    ph = fx["expected_prior_high"]
    va = fx["expected_volume_avg"]
    ema = fx["expected_exit_ema"]
    held = False
    entries, exits = [], []
    for i in range(len(closes)):
        if ph[i] is None or va[i] is None or i + 1 < fx["min_bars"]:
            continue
        if not held and closes[i] > ph[i] and vols[i] > va[i] * fx["volume_multiple"]:
            entries.append(i)
            held = True
        elif held and closes[i] < ema[i]:
            exits.append(i)
            held = False
    assert entries == fx["expected_entries"]
    assert exits == fx["expected_exits"]
    assert len(entries) >= 2 and len(exits) >= 2


def test_golden_covers_breakout_without_volume_and_volume_without_breakout():
    fx = _load_fixture()
    closes, vols, ph, va = fx["closes"], fx["volumes"], fx["expected_prior_high"], fx["expected_volume_avg"]
    m = fx["volume_multiple"]
    # bar 5: price breaks out, volume not enough -> no entry
    assert closes[5] > ph[5] and not vols[5] > va[5] * m and 5 not in fx["expected_entries"]
    # bar 9: volume surge, no breakout -> no entry
    assert vols[9] > va[9] * m and not closes[9] > ph[9] and 9 not in fx["expected_entries"]


def test_golden_matches_real_strategy_engine_execution_offset_by_one():
    fx = _load_fixture()
    df = _frame(fx["closes"], fx["highs"], fx["volumes"])
    built = _build_volume_breakout(_params(fx))
    assert built["min_bars"] == fx["min_bars"]
    result = Backtest(df, built["strategy"], cash=100000, finalize_trades=False).run()
    trades = result["_trades"]
    assert list(trades["EntryBar"]) == [i + 1 for i in fx["expected_entries"][: len(fx["expected_exits"])]]
    assert list(trades["ExitBar"]) == [i + 1 for i in fx["expected_exits"]]
    assert all(size > 0 for size in trades["Size"])


def test_changing_params_changes_trade_count():
    fx = _load_fixture()
    df = _frame(fx["closes"], fx["highs"], fx["volumes"])

    def count(**over):
        built = _build_volume_breakout({**_params(fx), **over})
        return len(Backtest(df, built["strategy"], cash=100000, finalize_trades=True).run()["_trades"])

    base = count()
    assert base == 2
    mult3 = count(volume_multiple=3)
    mult5 = count(volume_multiple=5)
    assert (mult3, mult5) == (1, 0)
    assert len({base, mult3, mult5}) == 3


# ---------------------------------------------------------------------------
# 2. Strict boundaries.
# ---------------------------------------------------------------------------


def _run(closes, highs, vols, **over):
    params = dict(lookback=3, volume_multiple=2, volume_avg_period=3, exit_ema=3)
    params.update(over)
    built = _build_volume_breakout(params)
    return Backtest(_frame(closes, highs, vols), built["strategy"], cash=100000, finalize_trades=False).run()


def test_close_equal_prior_high_does_not_enter():
    closes = [100.0] * 5 + [101.0, 101.0, 101.0]
    highs = [101.0] * 8  # prior high of bar 5 = 101 == close
    vols = [100.0] * 5 + [1000.0, 1000.0, 1000.0]
    result = _run(closes, highs, vols)
    assert len(result["_trades"]) == 0 and not result["_strategy"].position


def test_close_above_prior_high_enters_control():
    closes = [100.0] * 5 + [102.0, 102.0, 102.0]
    highs = [101.0] * 5 + [102.0] * 3
    vols = [100.0] * 5 + [1000.0, 1000.0, 1000.0]
    assert bool(_run(closes, highs, vols)["_strategy"].position)


def test_volume_equal_avg_times_multiple_does_not_enter():
    closes = [100.0] * 5 + [105.0, 105.0]
    highs = [101.0] * 5 + [105.0, 105.0]
    vols = [100.0] * 5 + [200.0, 100.0]  # 200 == avg(100) * 2 exactly
    result = _run(closes, highs, vols)
    assert not result["_strategy"].position and len(result["_trades"]) == 0


def test_volume_just_above_threshold_enters_control():
    closes = [100.0] * 5 + [105.0, 105.0]
    highs = [101.0] * 5 + [105.0, 105.0]
    vols = [100.0] * 5 + [201.0, 100.0]
    assert bool(_run(closes, highs, vols)["_strategy"].position)


def test_close_equal_ema_does_not_exit():
    # Highs sit below the flat closes, so the volume spike on bar 5 alone triggers entry;
    # afterwards close == EMA (constant series) on every bar -> must never exit.
    n = 12
    closes = [100.0] * n
    highs = [90.0] * n
    vols = [100.0] * 5 + [500.0] + [100.0] * (n - 6)
    assert _hand_ema(closes, 3)[-1] == 100.0
    result = _run(closes, highs, vols)
    assert bool(result["_strategy"].position)
    assert len(result["_trades"]) == 0


def test_no_entry_before_min_bars():
    # min_bars = max(4, 4, 3) = 4; a qualifying signal on bar index 2 (3 bars) must not fire.
    closes = [100.0, 100.0, 110.0]
    result = _run(closes, [101.0, 101.0, 111.0], [100.0, 100.0, 1000.0])
    assert len(result["_trades"]) == 0 and not result["_strategy"].position


# ---------------------------------------------------------------------------
# 3. Validation.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        dict(lookback="x"),
        dict(lookback=1),
        dict(volume_avg_period=1),
        dict(exit_ema=1),
        dict(volume_multiple=0),
        dict(volume_multiple=-1),
        dict(volume_multiple="abc"),
        dict(volume_multiple=float("nan")),
    ],
)
def test_invalid_params_rejected(bad):
    with pytest.raises(ValueError, match="INVALID_PARAMS"):
        _build_volume_breakout(bad)


def test_default_min_bars():
    assert _build_volume_breakout({})["min_bars"] == 21


# ---------------------------------------------------------------------------
# 4. Catalog / schema.
# ---------------------------------------------------------------------------


def test_tool_spec_registered_with_expected_schema():
    spec = TOOL_SPECS[TOOL]
    assert spec["strategy_family"] == "breakout"
    assert spec["is_default"] is False
    assert spec["description"].endswith("Maps to KOL '放量突破 / 量价突破'.")
    props = spec["param_schema_properties"]
    assert props["lookback"] == {"type": "integer", "default": 20, "minimum": 10, "maximum": 100}
    assert props["volume_multiple"] == {"type": "number", "default": 2, "minimum": 1.2, "maximum": 5}
    assert props["volume_avg_period"] == {"type": "integer", "default": 20, "minimum": 5, "maximum": 100}
    assert props["exit_ema"] == {"type": "integer", "default": 20, "minimum": 5, "maximum": 100}
    for key in ("stop_loss_pct", "take_profit_pct", "position_size_pct", "position_size_notional", "exchange"):
        assert key in props


def test_schema_validation_rejects_out_of_range_params():
    props = TOOL_SPECS[TOOL]["param_schema_properties"]
    assert _validate_params_against_schema({"lookback": 9}, props) is not None
    assert _validate_params_against_schema({"lookback": 101}, props) is not None
    assert _validate_params_against_schema({"volume_multiple": 1.1}, props) is not None
    assert _validate_params_against_schema({"volume_multiple": 5.1}, props) is not None
    assert _validate_params_against_schema({"volume_avg_period": 4}, props) is not None
    assert _validate_params_against_schema({"exit_ema": 101}, props) is not None
    assert _validate_params_against_schema({"lookback": 20}, props) is None
