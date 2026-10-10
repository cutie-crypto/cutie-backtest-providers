"""S4: long-liquidation spike reversal (spot, long-only, daily-series signal on 1h/4h/1d candles).

Central ``/metrics`` series: symbol = base coin, exchange ``AGGREGATED``, metric ``liquidation_long``,
interval ``1d`` (USD, whole-market aggregate). The day-D value (ts = D 00:00 UTC) is available at
D+1 00:00 UTC; the entry judgement for day D is made ONCE, at the close of the first candle of D+1
(the first candle whose close is >= available_at), and the order fills at the next candle's open.
Later candles of D+1 never re-judge the same day, and a stop-out does not reopen that day.

Entry (flat, at the D+1 first-candle close):
  * ``v_D >= p``. ``p`` is the ``percentile`` of the L daily values D-L .. D-1 (L = ``lookback_days``,
    day D itself NOT included) with linear interpolation -- numpy ``percentile`` default ``linear``:
    for the sorted values a[0..L-1] and rank = percentile / 100 * (L - 1), lo = floor(rank),
    p = a[lo] + (a[lo + 1] - a[lo]) * (rank - lo). Computed exactly in Decimal (deterministic).
    In ``fixed`` mode p = ``fixed_threshold_usd`` and no look-back days are fetched.
  * the day-D drop = last close of UTC day D / last close of UTC day D-1 - 1 <= -``min_drop_pct`` %,
    aggregated from the template's own candles. Without a D-1 candle the day yields no signal (not a
    failure).
Exits reuse the funding-reversal rules (frozen at the actual fill, judged on candle High/Low, filled at the
next candle open -- a gap therefore also fills at the open): stop_loss_pct, then time expiry, then
take_profit_pct. Time expiry: the first candle close at or after entry open + ``hold_hours`` (= the N-th
candle with N = hold_hours * 3600 / candle seconds) queues the close, which fills at the next open.

This module only plans the days, validates the series (fail-closed) and holds the strategy; it never
touches the network itself.
"""
from bisect import bisect_left
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext

from strategy_macro_events import make_macro_strategy
from strategy_risk_overlay import RiskState
from strategy_time_layer import utc_datetime

KIND = "liquidation_reversal"
DAY_SECONDS = 86400
CENTRAL_EXCHANGE = "AGGREGATED"
CENTRAL_METRIC = "liquidation_long"
CENTRAL_INTERVAL = "1d"
EARLIEST_TS = 1577836800  # 2020-01-01 00:00:00 UTC; nothing earlier is used, look-back included
GAP_REASON = "liquidation_data_gap"
HISTORY_REASON = "liquidation_history_unavailable"
ASSUMPTION_KEY = "liquidation_series"
TIMEFRAME_SECONDS = {"1h": 3600, "4h": 14400, "1d": 86400}

SCHEMA = {
    "threshold_mode": {"type": "string", "default": "percentile", "enum": ["percentile", "fixed"]},
    "percentile": {"type": "number", "default": 99, "minimum": 50, "maximum": 100},
    "lookback_days": {"type": "integer", "default": 30, "minimum": 7, "maximum": 90},
    "fixed_threshold_usd": {"type": "number"},
    "min_drop_pct": {"type": "number", "default": 3, "minimum": 0.5, "maximum": 30},
    "hold_hours": {"type": "integer", "default": 24, "minimum": 1, "maximum": 240},
    "take_profit_pct": {"type": "number", "default": 3, "minimum": 0.1, "maximum": 50},
    "stop_loss_pct": {"type": "number", "default": 3, "minimum": 0.1, "maximum": 20},
}


class LiquiditySeriesError(Exception):
    """The liquidation evidence is missing/unusable. ``reason`` is the stable machine code."""

    def __init__(self, reason, message, details=None):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.details = details or {}


def utc_date(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%d")


def _iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def _dec(value):
    return format(value.normalize(), "f")


def percentile_linear(values, pct):
    """Exact Decimal version of numpy ``percentile(values, pct)`` (default ``linear`` interpolation)."""
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile of an empty window")
    with localcontext() as ctx:
        ctx.prec = 60
        rank = Decimal(str(pct)) * (len(ordered) - 1) / 100
        low = int(rank)  # rank >= 0
        frac = rank - low
        if low + 1 >= len(ordered):
            return ordered[-1]
        return ordered[low] + (ordered[low + 1] - ordered[low]) * frac


def _require(params, key):
    value = params.get(key, SCHEMA[key].get("default"))
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"INVALID_PARAMS:{key} must be a number")
    spec = SCHEMA[key]
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"INVALID_PARAMS:{key} must be a finite number")
    if "minimum" in spec and not spec["minimum"] <= value <= spec["maximum"]:
        raise ValueError(f"INVALID_PARAMS:{key} must be in [{spec['minimum']}, {spec['maximum']}]")
    if spec["type"] == "integer" and not float(value).is_integer():
        raise ValueError(f"INVALID_PARAMS:{key} must be an integer")
    return value


class LiquidationReversalConfig:
    """Duck-types MacroConfig for make_macro_strategy; ``days`` is bound after the series fetch."""

    kind = KIND

    def __init__(self, values):
        self.values = values
        self.events = ()  # the base macro engine plans nothing; the subclass plans ``days`` itself
        self.days = ()
        self.assumptions = {}
        self.candle_seconds = None

    @classmethod
    def parse(cls, params):
        mode = params.get("threshold_mode", SCHEMA["threshold_mode"]["default"])
        if mode not in SCHEMA["threshold_mode"]["enum"]:
            raise ValueError("INVALID_PARAMS:threshold_mode must be one of ['percentile', 'fixed']")
        fixed = params.get("fixed_threshold_usd")
        if mode == "fixed":
            if fixed is None:
                raise ValueError("INVALID_PARAMS:fixed_threshold_usd is required when threshold_mode=fixed")
            if isinstance(fixed, bool) or not isinstance(fixed, (int, float)) or fixed != fixed \
                    or fixed in (float("inf"), float("-inf")) or fixed <= 0:
                raise ValueError("INVALID_PARAMS:fixed_threshold_usd must be a number > 0")
        elif fixed is not None:
            raise ValueError("INVALID_PARAMS:fixed_threshold_usd requires threshold_mode=fixed")
        values = dict(
            threshold_mode=mode,
            percentile=_require(params, "percentile"),
            lookback_days=_require(params, "lookback_days"),
            fixed_threshold_usd=fixed,
            min_drop_pct=_require(params, "min_drop_pct"),
            hold_hours=_require(params, "hold_hours"),
            take_profit_pct=_require(params, "take_profit_pct"),
            stop_loss_pct=_require(params, "stop_loss_pct"),
        )
        return cls(values)

    @property
    def hold_minutes(self):
        return self.values["hold_hours"] * 60

    @property
    def lookback(self):
        return self.values["lookback_days"] if self.values["threshold_mode"] == "percentile" else 0

    def bind_timeframe(self, timeframe):
        seconds = TIMEFRAME_SECONDS.get(timeframe)
        if seconds is None:
            raise ValueError(f"INVALID_PARAMS:unsupported timeframe {timeframe}")
        if (self.values["hold_hours"] * 3600) % seconds:
            raise ValueError(
                f"INVALID_PARAMS:hold_hours must be a multiple of the candle length ({seconds // 3600}h for {timeframe})")
        self.candle_seconds = seconds

    @property
    def hold_bars(self):
        return self.values["hold_hours"] * 3600 // self.candle_seconds

    # ---- planning -------------------------------------------------------------------------------
    @staticmethod
    def judged_days(start_at, end_at):
        """(first, last) judged day D (unix s at 00:00 UTC) or None.

        D needs both its own and D-1's candles inside the window (first D = start day + 1), and the
        first candle of D+1 must open before ``end_at``.
        """
        first = (start_at // DAY_SECONDS) * DAY_SECONDS + DAY_SECONDS
        last = ((end_at - 1) // DAY_SECONDS) * DAY_SECONDS - DAY_SECONDS
        return (first, last) if first <= last else None

    def required_range(self, first_day, last_day):
        """(first, last) series timestamps the run reads: look-back before the first day, last judged day."""
        return first_day - self.lookback * DAY_SECONDS, last_day

    def history_unavailable(self, need_first):
        return need_first < EARLIEST_TS

    def gap_details(self, need_first, need_last):
        return {"series_metric": CENTRAL_METRIC, "required_first_date": utc_date(need_first),
                "required_last_date": utc_date(need_last)}

    # ---- binding --------------------------------------------------------------------------------
    def bind(self, first_day, last_day, series):
        """Turn the fetched daily series into per-day records; fail closed on any missing day."""
        need_first, need_last = self.required_range(first_day, last_day)
        by_ts = {}
        for row in series.get("rows", ()):
            try:
                value = Decimal(str(row["value"]))
            except (InvalidOperation, KeyError):
                value = None
            if value is None or not value.is_finite() or value < 0:
                raise LiquiditySeriesError(
                    GAP_REASON, f"liquidation row for {utc_date(row.get('ts', 0))} is unreadable",
                    {**self.gap_details(need_first, need_last), "missing_date": utc_date(row.get("ts", 0))})
            by_ts[row["ts"]] = value
        days = []
        for ts in range(need_first, need_last + 1, DAY_SECONDS):
            if ts not in by_ts:
                raise LiquiditySeriesError(
                    GAP_REASON,
                    f"liquidation_long series has no row for {utc_date(ts)}; this template does not run on a "
                    "partial series",
                    {**self.gap_details(need_first, need_last), "missing_date": utc_date(ts)})
        mode = self.values["threshold_mode"]
        for day in range(first_day, last_day + 1, DAY_SECONDS):
            if mode == "percentile":
                window = [by_ts[day - k * DAY_SECONDS] for k in range(self.lookback, 0, -1)]
                threshold = percentile_linear(window, self.values["percentile"])
            else:
                threshold = Decimal(str(self.values["fixed_threshold_usd"]))
            days.append(dict(
                day=day, value=by_ts[day], threshold=threshold,
                ts_utc=_iso(day + DAY_SECONDS), label=f"liquidation_long_{utc_date(day)}"))
        self.days = tuple(days)
        rows = series.get("rows", ())
        used = [row for row in rows if need_first <= row["ts"] <= need_last]
        self._assume(used, series, first_day, last_day, need_first, need_last, len(days))

    def _assume(self, used, series, first_day, last_day, need_first, need_last, count):
        self._common_assumptions()
        self.assumptions[ASSUMPTION_KEY] = dict(
            symbol=series.get("central_symbol"),
            exchange=CENTRAL_EXCHANGE,
            metric=CENTRAL_METRIC,
            interval=CENTRAL_INTERVAL,
            unit="usd",
            first_ts=used[0]["ts"],
            last_ts=used[-1]["ts"],
            first_date=utc_date(used[0]["ts"]),
            last_date=utc_date(used[-1]["ts"]),
            count=len(used),
            revision=used[0]["revision"] if used else None,
            earliest_available_date=utc_date(EARLIEST_TS),
            available_at_rule=f"available_at = ts + {DAY_SECONDS}s; day D is judged once, at the close of the "
                              "first candle of D+1, and fills at the next candle open",
            gap_policy="fail_closed",
            lookback_days=self.lookback,
            judged_days=dict(count=count, first_date=utc_date(first_day), last_date=utc_date(last_day)),
            fetched_at_ms=series.get("fetched_at_ms"),
        )

    def unbound_assumptions(self):
        """Window with no judged day: nothing fetched, no days."""
        self._common_assumptions()
        self.assumptions[ASSUMPTION_KEY] = dict(
            exchange=CENTRAL_EXCHANGE, metric=CENTRAL_METRIC, interval=CENTRAL_INTERVAL, unit="usd", count=0,
            earliest_available_date=utc_date(EARLIEST_TS), gap_policy="fail_closed",
            judged_days=dict(count=0))

    def _common_assumptions(self):
        values = self.values
        self.assumptions.update(
            threshold_mode=values["threshold_mode"],
            percentile=values["percentile"] if values["threshold_mode"] == "percentile" else None,
            lookback_days=self.lookback,
            fixed_threshold_usd=values["fixed_threshold_usd"],
            min_drop_pct=values["min_drop_pct"],
            hold_hours=values["hold_hours"],
            hold_bars=self.hold_bars if self.candle_seconds else None,
            take_profit_pct=values["take_profit_pct"],
            stop_loss_pct=values["stop_loss_pct"],
            warmup_bars=0,
            threshold_rule="p = linear-interpolation percentile (numpy default) of the L days D-L..D-1, day D "
                           "excluded; fixed mode p = fixed_threshold_usd; entry needs value(D) >= p",
            drop_rule="last close of UTC day D / last close of UTC day D-1 - 1 <= -min_drop_pct%; "
                      "no D-1 candle -> no signal for that day",
            timing="day D is judged once at the close of the first D+1 candle; market buy at the next open; "
                   "later candles of the day never re-judge; only when flat",
            exit="stop_loss_pct / take_profit_pct frozen at the actual fill, detected by candle High/Low and "
                 "filled at the next candle open (a gap fills at the open too); priority stop_loss, "
                 "time expiry, take_profit; time expiry = first candle close at or after entry open + "
                 "hold_hours (the N-th candle), close fills at the next open",
            daily_granularity="daily liquidation series; deviates from the original hourly definition",
            data_source="central_metrics:coinglass_aggregated_liquidation_long_1d",
            signal_counts={},
            entry_thresholds=[],
        )


def schema():
    return deepcopy(SCHEMA)


def make_liquidation_strategy(mixin, config, risk, initial_capital):
    base = make_macro_strategy(mixin, config, risk, initial_capital)

    class LiquidationReversalStrategy(base):
        def init(self):
            super().init()
            self._day_close = {}  # day start (unix s) -> (bar index of the day's last candle, close)
            for index, opened in enumerate(self._opens):
                self._day_close[int(opened.timestamp()) // DAY_SECONDS * DAY_SECONDS] = (
                    index, float(self.data.Close[index]))
            self._liq_by_bar = {}
            self._liq_entries = []
            self._liq_counts = {}
            config.assumptions["entry_thresholds"] = self._liq_entries
            config.assumptions["signal_counts"] = self._liq_counts
            for item in config.days:
                record = dict(ts_utc=item["ts_utc"], label=item["label"], day_utc=_iso(item["day"]),
                              liquidation_long=_dec(item["value"]), threshold=_dec(item["threshold"]),
                              threshold_mode=config.values["threshold_mode"])
                self.macro_events.append(record)
                available = datetime.fromtimestamp(item["day"] + DAY_SECONDS, timezone.utc)
                decision = bisect_left(self._opens, available)
                if decision >= len(self._opens):
                    self._skip(record, "event_after_data_end", "skipped_out_of_range")
                    self._count(record)
                    continue
                if self._opens[decision] >= available + timedelta(days=1):
                    self._skip(record, "event_in_data_gap", "skipped_out_of_range")
                    self._count(record)
                    continue
                record["decision_bar_utc"] = self._opens[decision].isoformat()
                self._liq_by_bar.setdefault(decision, []).append((record, item))

        def _risk_layer_check_exit(self):
            trade = self.trades[-1]
            if self._risk_trade is not trade:
                self._risk_trade = trade
                entry = Decimal(str(trade.entry_price))
                stop = self._stop(trade.is_long, entry)
                take = entry * (1 + Decimal(str(config.values["take_profit_pct"])) / 100)
                self._risk_state = RiskState(entry, stop, take, abs(entry - stop), "long")
                self._entry_record.update(stop_price=str(stop), take_price=str(take))
            return mixin._risk_layer_check_exit(self)

        def _count(self, record):
            key = record.get("reason") or record["status"]
            self._liq_counts[key] = self._liq_counts.get(key, 0) + 1

        def _drop_pct(self, day, bar):
            today, before = self._day_close.get(day), self._day_close.get(day - DAY_SECONDS)
            if today is None or before is None or today[0] >= bar or before[0] >= bar:
                return None
            if before[1] <= 0:
                return None
            return (Decimal(str(today[1])) / Decimal(str(before[1])) - 1) * 100

        def next(self):
            super().next()  # exits first: a close queued on this bar blocks a same-bar entry
            bar = len(self.data) - 1
            for record, item in self._liq_by_bar.get(bar, ()):
                self._judge(record, item, bar)
                self._count(record)

        def _judge(self, record, item, bar):
            if item["value"] < item["threshold"]:
                self._skip(record, "liquidation_below_threshold")
                return
            drop = self._drop_pct(item["day"], bar)
            if drop is None:
                self._skip(record, "no_prior_day_candle")
                return
            record["drop_pct"] = _dec(drop)
            if drop > -Decimal(str(config.values["min_drop_pct"])):
                self._skip(record, "drop_below_min")
                return
            self._submit(record, "long", bar)
            if record.get("status") == "submitted":
                self._liq_entries.append(dict(
                    day_utc=record["day_utc"], threshold=record["threshold"],
                    liquidation_long=record["liquidation_long"], decision_bar_utc=record["decision_bar_utc"]))

    return LiquidationReversalStrategy
