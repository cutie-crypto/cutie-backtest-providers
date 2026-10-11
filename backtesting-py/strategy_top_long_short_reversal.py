"""S3: Binance top-trader long/short ratio reversal (spot, long only, daily series).

The ratio comes from the central ``/metrics`` daily series (symbol = base coin, exchange ``Binance``,
metric ``top_long_short_ratio``, interval ``1d``). The series is the Binance top-trader ACCOUNT ratio,
independent of which exchange the candles come from.

Look-ahead rule: the value of day D (ts = D 00:00 UTC) has ``available_at = D+1 00:00 UTC``. A decision at
a bar close may read the newest row whose ``available_at <= bar close``; the earliest fill is therefore
the first open of D+1. Each daily value is judged at most once: on the first bar close at which it is
readable. Later (small-timeframe) bars of the same day do not re-judge it, and a stop-loss exit never
re-enters before the next daily value.

Rules (one position, long only):
* flat and ``ratio <= long_threshold``                          -> buy at the next open;
* in position and ``exit_band_low <= ratio <= exit_band_high``  -> sell at the next open (ratio_back_in_band);
* in position and ``ratio > exit_band_high``                    -> sell at the next open (ratio_above_band);
* percent stop-loss frozen at the actual fill, detected by candle low, filled at the next open (same
  mechanics as the funding template); the stop wins over a signal exit on the same bar.

This module never touches the network: the provider fetches the series and binds it.
"""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

TOOL_NAME = "top_long_short_reversal"
CENTRAL_EXCHANGE = "Binance"
CENTRAL_METRIC = "top_long_short_ratio"
CENTRAL_INTERVAL = "1d"
SERIES_STEP_SECONDS = 86400
TOP_LSR_EARLIEST_TS = 1589587200  # 2020-05-16 00:00:00 UTC (top-trader series actual start)
ASSUMPTION_KEY = "long_short_ratio_series"
GAP_REASON = "long_short_ratio_data_gap"
HISTORY_REASON = "long_short_ratio_history_unavailable"
EXIT_REASON_IN_BAND = "ratio_back_in_band"
EXIT_REASON_ABOVE_BAND = "ratio_above_band"

SCHEMA = {
    "long_threshold": {"type": "number", "default": 0.7, "minimum": 0.1, "maximum": 3},
    "exit_band_low": {"type": "number", "default": 1.0, "minimum": 0.1, "maximum": 5},
    "exit_band_high": {"type": "number", "default": 1.5, "minimum": 0.1, "maximum": 10},
    "stop_loss_pct": {"type": "number", "default": 3, "minimum": 0.1, "maximum": 20},
}
ORDER_RULE = "long_threshold < exit_band_low <= exit_band_high"


def utc_date(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%d")


def _require_number(params, key):
    value = params.get(key, SCHEMA[key]["default"])
    if type(value) not in (int, float) or value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"INVALID_PARAMS:{key} must be a number")
    spec = SCHEMA[key]
    if not spec["minimum"] <= value <= spec["maximum"]:
        raise ValueError(f"INVALID_PARAMS:{key} must be in [{spec['minimum']}, {spec['maximum']}]")
    return value


def need_range(open_times, bar_seconds):
    """[first, last] day-series ts the decisions on bars opening at ``open_times`` (unix s) consume."""
    first_close = open_times[0] + bar_seconds
    last_close = open_times[-1] + bar_seconds
    step = SERIES_STEP_SECONDS
    return (first_close // step) * step - step, (last_close // step) * step - step


class TopLongShortReversalConfig:
    def __init__(self, values):
        self.values = values
        self.rows = ()
        self.bar_seconds = None
        self.series_block = {}
        self.signal_block = dict(
            long_threshold=values["long_threshold"], exit_band_low=values["exit_band_low"],
            exit_band_high=values["exit_band_high"], stop_loss_pct=values["stop_loss_pct"],
            entry_rule="flat and ratio <= long_threshold: buy at the next open",
            exit_rule="in position and exit_band_low <= ratio <= exit_band_high: sell at the next open "
                      "(ratio_back_in_band); ratio > exit_band_high: sell at the next open (ratio_above_band)",
            stop_rule="stop_loss_pct frozen at the actual fill, detected by candle low, filled at the next "
                      "open (a gap fills at that open); the stop wins over a signal exit on the same bar",
            judgement_rule="each daily value is judged once, at the first bar close where it is readable",
            series_scope="Binance top-trader account ratio; independent of the candle exchange",
            judged_values=0, entry_signals=0, exit_signals_in_band=0, exit_signals_above_band=0,
            stop_loss_exits=0, values_consumed_by_stop=0, entries_blocked=0)
        # Shared by reference with the provider: filled at bind time, counters updated by the strategy.
        self.assumptions = {ASSUMPTION_KEY: self.series_block, TOOL_NAME: self.signal_block}

    @classmethod
    def parse(cls, params):
        values = {key: _require_number(params, key) for key in SCHEMA}
        if not values["long_threshold"] < values["exit_band_low"] <= values["exit_band_high"]:
            raise ValueError(f"INVALID_PARAMS:require {ORDER_RULE}")
        return cls(values)

    def bind(self, rows, *, symbol, bar_seconds):
        """Bind the fetched, fail-closed validated series (rows: ``_fetch_metric_series`` output)."""
        self.rows = tuple(sorted(rows, key=lambda row: row["ts"]))
        self.bar_seconds = bar_seconds
        first, last = self.rows[0], self.rows[-1]
        self.series_block.update(
            symbol=symbol, exchange=CENTRAL_EXCHANGE, metric=CENTRAL_METRIC, interval=CENTRAL_INTERVAL,
            first_ts=first["ts"], last_ts=last["ts"],
            first_date=utc_date(first["ts"]), last_date=utc_date(last["ts"]),
            count=len(self.rows), revision=first["revision"],
            earliest_available_date=utc_date(TOP_LSR_EARLIEST_TS),
            available_at_rule=f"available_at = ts + {SERIES_STEP_SECONDS}s; a bar-close decision reads the newest "
                              "row with available_at <= bar close, so day D first trades at the D+1 open",
            gap_policy="fail_closed")


def make_strategy(mixin, config, risk, initial_capital):
    from backtesting import Strategy
    import pandas as pd

    class TopLongShortReversalStrategy(mixin, Strategy):
        _risk = risk
        _initial_capital = initial_capital

        def init(self):
            self._risk_init()
            rows = config.rows
            bar_seconds = config.bar_seconds
            self._lsr_log = []
            self._lsr_readable = []
            self._lsr_judged = -1
            cursor = -1
            for stamp in self.data.index:
                close = int(pd.Timestamp(stamp).value // 10**9) + bar_seconds
                while cursor + 1 < len(rows) and rows[cursor + 1]["available_at"] <= close:
                    cursor += 1
                # Newest row readable at this bar's close (anti look-ahead: available_at <= close).
                self._lsr_readable.append(cursor)

        @property
        def top_lsr_report(self):
            return dict(decisions=list(self._lsr_log), counts={
                key: config.signal_block[key] for key in (
                    "judged_values", "entry_signals", "exit_signals_in_band", "exit_signals_above_band",
                    "stop_loss_exits", "values_consumed_by_stop", "entries_blocked")})

        def _sizing_template_stop(self, order):
            fill = Decimal(str(self._broker._adjusted_price(order.size, self.data.Open[-1])))
            return fill * (1 - Decimal(str(config.values["stop_loss_pct"])) / 100)

        def _log(self, bar, row, value, action, reason=None):
            self._lsr_log.append(dict(
                decision_bar_open=int(pd.Timestamp(self.data.index[bar]).value // 10**9),
                value_ts=row["ts"], value_date=utc_date(row["ts"]), value=str(value), action=action,
                reason=reason))

        def next(self):
            bar = len(self.data) - 1
            # A value is judged once: on the first next() call at which it is readable (the engine never
            # calls next() on bar 0, so a value readable already at bar 0 is judged at bar 1).
            readable = self._lsr_readable[bar]
            row_index = readable if readable >= 0 and readable != self._lsr_judged else None
            if row_index is not None:
                self._lsr_judged = readable
            signals = config.signal_block
            if self.position:
                self._risk_exit_reason = None
                if self._risk_check_exit():
                    # Stop first: a stop exit consumes this bar (no signal exit, no re-entry on the same bar).
                    signals["stop_loss_exits"] += 1
                    if row_index is not None:
                        signals["values_consumed_by_stop"] += 1
                        row = config.rows[row_index]
                        self._log(bar, row, row["value"], "stop_exit_consumed_value",
                                  self._risk_exit_reason or "stop_loss")
                    return
            if row_index is None:
                return
            row = config.rows[row_index]
            try:
                value = Decimal(str(row["value"]))
            except InvalidOperation:  # _fetch_metric_series already canonicalised; defensive only
                return
            signals["judged_values"] += 1
            values = config.values
            low, high = Decimal(str(values["exit_band_low"])), Decimal(str(values["exit_band_high"]))
            if self.position:
                if low <= value <= high:
                    signals["exit_signals_in_band"] += 1
                    self._log(bar, row, value, "exit", EXIT_REASON_IN_BAND)
                    self.position.close()
                elif value > high:
                    signals["exit_signals_above_band"] += 1
                    self._log(bar, row, value, "exit", EXIT_REASON_ABOVE_BAND)
                    self.position.close()
                return
            if self.orders:
                return
            if value <= Decimal(str(values["long_threshold"])):
                count = len(self.orders)
                self._risk_buy()
                if len(self.orders) == count:
                    signals["entries_blocked"] += 1
                    self._log(bar, row, value, "entry_blocked", "entry_gate")
                else:
                    signals["entry_signals"] += 1
                    self._log(bar, row, value, "entry", "ratio_at_or_below_threshold")

    return TopLongShortReversalStrategy


def schema():
    return deepcopy(SCHEMA)
