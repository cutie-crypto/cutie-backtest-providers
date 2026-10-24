"""Independent reference implementation for the v2c conformance golden
(``fixtures/strategy_kernel_conformance_v2c.json``): ``ema`` / ``macd`` /
``rolling_zscore`` in StrategySpec v2.

Deliberately imports nothing from ``strategy_kernel``: the golden values are
produced here from the formulas written in the fixture's ``note`` and are
cross-checked against pandas in floating point, so the kernel is measured
against the golden, never the other way round.

Regenerate (only when a case is added, never to make the kernel pass):
    .venv/bin/python tests/_v2c_reference.py --write
"""

from __future__ import annotations

import json
import sys
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from canonical_json import canonical_decimal_str, canonical_json_sha256  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "strategy_kernel_conformance_v2c.json"
SCHEMA = "cutie.strategy_kernel_conformance.v2c"
DECIMAL128 = Context(prec=34, rounding=ROUND_HALF_EVEN)

NOTE = (
    "All series use a fixed 1h (3600s) step starting at ts=0. `value: null` in "
    "`input` is a genuine mid-stream gap; `expected: null` is a frame the "
    "primitive must report missing. Arithmetic is cutie.decimal128.v1 "
    "(34 significant digits, ROUND_HALF_EVEN), every operation rounded. "
    "ema(window_bars=N): alpha=2/(N+1); seed at the N-th fetched value = "
    "(x_0+...+x_{N-1})/N summed left to right; afterwards "
    "ema_t = alpha*x_t + (1-alpha)*ema_{t-1}; fewer than N values -> missing; "
    "a gap breaks the chain irrecoverably (same rule as v2 rsi_wilder). "
    "macd(fast,slow,signal,output): line_t = ema(fast)_t - ema(slow)_t from "
    "the slow seed onward; signal = ema(signal) over the line series, seeded "
    "from the first line value; hist_t = line_t - signal_t; output selects "
    "line/signal/hist; a gap breaks all three irrecoverably. "
    "rolling_zscore(window_bars=N): exactly N values ending at x_t, "
    "mean = sum/N, population variance = sum((v-mean)*(v-mean))/N, "
    "z = (x_t-mean)/sqrt(variance); variance 0 -> missing (never 0); a gap "
    "inside the window -> missing until the window slides past it. Bollinger "
    "templates use population stdev (rolling(...).std(ddof=0)), so "
    "`rolling_zscore gte k` / `lte -k` is exactly touching the k-sigma band."
)


def _dec(raw) -> Optional[Decimal]:
    if raw is None or isinstance(raw, Decimal):
        return raw
    return Decimal(raw)


def ema_reference(values: list[Optional[str]], window: int) -> list[Optional[Decimal]]:
    out: list[Optional[Decimal]] = []
    with localcontext(DECIMAL128) as ctx:
        alpha = ctx.divide(Decimal(2), Decimal(window + 1))
        keep = ctx.subtract(Decimal(1), alpha)
        broken = False
        seed: list[Decimal] = []
        previous: Optional[Decimal] = None
        for raw in values:
            value = _dec(raw)
            if broken or value is None:
                broken = True
                out.append(None)
                continue
            if previous is None:
                seed.append(value)
                if len(seed) < window:
                    out.append(None)
                    continue
                total = Decimal(0)
                for item in seed:
                    total = ctx.add(total, item)
                previous = ctx.divide(total, Decimal(window))
            else:
                previous = ctx.add(
                    ctx.multiply(alpha, value), ctx.multiply(keep, previous)
                )
            out.append(+previous)
    return out


def macd_reference(
    values: list[Optional[str]], fast: int, slow: int, signal: int, output: str
) -> list[Optional[Decimal]]:
    fast_ema = ema_reference(values, fast)
    slow_ema = ema_reference(values, slow)
    with localcontext(DECIMAL128) as ctx:
        line: list[Optional[Decimal]] = [
            ctx.subtract(f, s) if f is not None and s is not None else None
            for f, s in zip(fast_ema, slow_ema)
        ]
    first = next((i for i, v in enumerate(line) if v is not None), len(line))
    # The signal EMA's own stream starts at the first line value; anything
    # missing after that point is a broken chain, never a fresh seed.
    signal_tail = ema_reference(line[first:], signal) if first < len(line) else []
    signal_series: list[Optional[Decimal]] = [None] * first + signal_tail
    if output == "line":
        return line
    if output == "signal":
        return signal_series
    with localcontext(DECIMAL128) as ctx:
        return [
            ctx.subtract(a, b) if a is not None and b is not None else None
            for a, b in zip(line, signal_series)
        ]


def zscore_reference(values: list[Optional[str]], window: int) -> list[Optional[Decimal]]:
    out: list[Optional[Decimal]] = []
    for index in range(len(values)):
        if index + 1 < window:
            out.append(None)
            continue
        raw_window = values[index + 1 - window : index + 1]
        if any(item is None for item in raw_window):
            out.append(None)
            continue
        items = [Decimal(item) for item in raw_window]
        with localcontext(DECIMAL128) as ctx:
            total = Decimal(0)
            for item in items:
                total = ctx.add(total, item)
            mean = ctx.divide(total, Decimal(window))
            squares = Decimal(0)
            for item in items:
                delta = ctx.subtract(item, mean)
                squares = ctx.add(squares, ctx.multiply(delta, delta))
            variance = ctx.divide(squares, Decimal(window))
            if variance == 0:
                out.append(None)
                continue
            out.append(+ctx.divide(ctx.subtract(items[-1], mean), ctx.sqrt(variance)))
    return out


def reference(case: dict) -> list[Optional[str]]:
    values = [row["value"] for row in case["input"]]
    params = case["params"]
    if case["primitive"] == "ema":
        series = ema_reference(values, params["window_bars"])
    elif case["primitive"] == "macd":
        series = macd_reference(
            values, params["fast"], params["slow"], params["signal"], params["output"]
        )
    elif case["primitive"] == "rolling_zscore":
        series = zscore_reference(values, params["window_bars"])
    else:  # pragma: no cover - fixture authoring error
        raise ValueError(case["primitive"])
    with localcontext(DECIMAL128):
        return [canonical_decimal_str(v) if v is not None else None for v in series]


def pandas_reference(case: dict) -> list[Optional[float]]:
    """Float cross-check with pandas (ewm adjust=False over an SMA seed,
    rolling std ddof=0). Gaps are handled the same way as the Decimal
    reference: everything after a gap is NaN for the recursive primitives."""
    import numpy as np
    import pandas as pd

    raw = [None if row["value"] is None else float(row["value"]) for row in case["input"]]
    params = case["params"]

    def ema(series: "pd.Series", n: int) -> "pd.Series":
        out = pd.Series(np.nan, index=series.index)
        valid = series.notna()
        stop = int(valid.values.argmin()) if not valid.all() else len(series)
        if stop < n:
            return out
        seeded = series.iloc[:stop].copy()
        seeded.iloc[: n - 1] = np.nan
        seeded.iloc[n - 1] = series.iloc[:n].mean()
        out.iloc[:stop] = seeded.ewm(alpha=2.0 / (n + 1), adjust=False).mean()
        out.iloc[: n - 1] = np.nan
        return out

    series = pd.Series(raw, dtype="float64")
    if case["primitive"] == "ema":
        result = ema(series, params["window_bars"])
    elif case["primitive"] == "macd":
        line = ema(series, params["fast"]) - ema(series, params["slow"])
        first = int(line.notna().values.argmax()) if line.notna().any() else len(line)
        signal = pd.Series(np.nan, index=line.index)
        if first < len(line):
            tail = ema(line.iloc[first:].reset_index(drop=True), params["signal"])
            signal.iloc[first:] = tail.values
        result = {"line": line, "signal": signal, "hist": line - signal}[params["output"]]
    else:
        n = params["window_bars"]
        mean = series.rolling(n).mean()
        std = series.rolling(n).std(ddof=0)
        result = (series - mean) / std
        result[std.abs() < 1e-12] = np.nan
    return [None if pd.isna(v) else float(v) for v in result]


def _rows(values: list[Optional[str]]) -> list[dict]:
    return [{"ts": index * 3600, "value": value} for index, value in enumerate(values)]


_TREND = ["10", "11", "13", "12", "15", "14", "16", "18", "17", "19", "21", "20", "23", "22"]

CASE_SPECS: list[tuple[str, str, dict, str, list[Optional[str]]]] = [
    (
        "ema_sma_seed_then_recursion",
        "ema",
        {"window_bars": 3},
        "f=0,1 < window -> missing (warmup); f=2 seeds with the SMA of the first 3; alpha=0.5 afterwards.",
        _TREND[:8],
    ),
    (
        "ema_warmup_never_reached",
        "ema",
        {"window_bars": 5},
        "Only 4 fetched values for a 5-bar EMA: every frame is missing.",
        ["10", "11", "12", "13"],
    ),
    (
        "ema_non_terminating_alpha_rounds_decimal128",
        "ema",
        {"window_bars": 2},
        "alpha=2/3 does not terminate: pins decimal128 HALF_EVEN rounding of every step.",
        ["1", "2", "4", "3", "7"],
    ),
    (
        "ema_gap_breaks_chain_never_recovers",
        "ema",
        {"window_bars": 2},
        "ts=10800 is a genuine gap: it and every later frame are missing, even once 2 fresh values follow.",
        ["10", "12", "11", None, "13", "14", "15"],
    ),
    (
        "ema_constant_series",
        "ema",
        {"window_bars": 3},
        "A constant series stays exactly at the constant once seeded.",
        ["100"] * 6,
    ),
    (
        "rolling_zscore_population_stdev",
        "rolling_zscore",
        {"window_bars": 4},
        "Population (ddof=0) stdev, same as the Bollinger templates; sample stdev would differ on every value.",
        ["10", "12", "11", "15", "13", "18", "16"],
    ),
    (
        "rolling_zscore_constant_window_is_missing",
        "rolling_zscore",
        {"window_bars": 3},
        "A window with stdev 0 is missing (never 0); values return once the window holds two distinct values.",
        ["5", "5", "5", "5", "6", "6", "7"],
    ),
    (
        "rolling_zscore_gap_until_window_slides_past",
        "rolling_zscore",
        {"window_bars": 3},
        "ts=7200 is a gap: every window containing it is missing; the window [3,4,5] after it is valid.",
        ["1", "2", None, "4", "6", "5", "9"],
    ),
    (
        "macd_line",
        "macd",
        {"fast": 3, "slow": 5, "signal": 3, "output": "line"},
        "line = ema3 - ema5, defined from f=4 (the slow seed).",
        _TREND,
    ),
    (
        "macd_signal",
        "macd",
        {"fast": 3, "slow": 5, "signal": 3, "output": "signal"},
        "signal = ema3 over the line series seeded from the first line value: defined from f=6.",
        _TREND,
    ),
    (
        "macd_hist",
        "macd",
        {"fast": 3, "slow": 5, "signal": 3, "output": "hist"},
        "hist = line - signal, defined from f=6.",
        _TREND,
    ),
    (
        "macd_signal_warmup_not_reached",
        "macd",
        {"fast": 2, "slow": 4, "signal": 3, "output": "signal"},
        "5 values: line exists from f=3 but its 3-bar signal never seeds -> all missing.",
        ["10", "11", "13", "12", "15"],
    ),
    (
        "macd_gap_breaks_chain_never_recovers",
        "macd",
        {"fast": 2, "slow": 3, "signal": 2, "output": "hist"},
        "A gap at ts=18000 kills line, signal and hist for every later frame.",
        ["10", "11", "13", "12", "15", None, "16", "18", "17", "19"],
    ),
    (
        "macd_constant_series_is_zero",
        "macd",
        {"fast": 2, "slow": 3, "signal": 2, "output": "line"},
        "A constant series has line 0 from the slow seed onward (0, not missing: macd has no stdev).",
        ["50"] * 6,
    ),
]


def build_fixture() -> dict:
    cases = []
    for name, primitive, params, description, values in CASE_SPECS:
        case = {
            "name": name,
            "primitive": primitive,
            "params": params,
            "description": description,
            "input": _rows(values),
        }
        case["expected"] = [
            {"ts": row["ts"], "value": value}
            for row, value in zip(case["input"], reference(case))
        ]
        cases.append(case)
    return {
        "schema": SCHEMA,
        "source_of_truth": (
            "JES21-B3b 二期 IMPL §3.3 / K1 (2026-10-25): StrategySpec v2 ema / macd / "
            "rolling_zscore; Server (S1) and Provider share this fixture verbatim"
        ),
        "note": NOTE,
        "sha256": canonical_json_sha256(cases),
        "cases": cases,
    }


if __name__ == "__main__":
    fixture = build_fixture()
    text = json.dumps(fixture, ensure_ascii=False, indent=2) + "\n"
    if "--write" in sys.argv:
        FIXTURE.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
