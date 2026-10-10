"""P2: pre-settlement funding-rate reversal (Binance USD-M perpetual, 15m only).

At every 8h settlement point S (00/08/16 UTC) the template enters ``lead_minutes`` before S, against
the sign of the PREVIOUS PERIOD's settled funding rate, and holds ``hold_minutes`` (or until the
percent stop). Look-ahead rule: the period-S rate is only fixed at S, so the judgement reads the row
of S - 8h, never the row of S. The funding request never asks past the last previous-period row.

The trading engine is the Q18 surprise engine (strategy_macro_events): ``actual`` = previous settled
rate in percent, ``expected`` = 0, threshold = ``rate_threshold_pct``; ``rate >= +threshold`` -> short,
``rate <= -threshold`` -> long. This module only plans the events, validates the funding series
(fail-closed) and reports the evidence; it never touches the network itself.
"""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from strategy_macro_events import FUNDING_KIND

SLOT_SECONDS = 8 * 3600
SLOT_MS = SLOT_SECONDS * 1000
# A settlement row is stamped a few ms/s after the 8h boundary; anything further out is not an 8h cadence row.
JITTER_MS = 60_000
GRID_MINUTES = 15
SOURCE = {"exchange": "binance", "market": "usdt_perpetual"}

SCHEMA = {
    "lead_minutes": {"type": "integer", "default": 30, "minimum": 15, "maximum": 120},
    "hold_minutes": {"type": "integer", "default": 30, "minimum": 15, "maximum": 240},
    "rate_threshold_pct": {"type": "number", "default": 0.05, "minimum": 0.005, "maximum": 1},
    "stop_loss_pct": {"type": "number", "default": 1, "minimum": 0.1, "maximum": 10},
}
DEVIATION_NOTE = (
    "按上一期已结算费率判断，与实盘看到的预测费率有偏差 "
    "(judged on the previous period's settled funding rate; the live predicted rate can differ)"
)


class FundingSeriesError(Exception):
    """Funding evidence is missing/unusable. ``reason`` is the stable machine code."""

    def __init__(self, reason, message, details=None):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.details = details or {}


def _utc(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc)


def _iso(seconds):
    return _utc(seconds).isoformat()


def _require_integer(params, key):
    value = params.get(key, SCHEMA[key]["default"])
    if type(value) is not int:
        raise ValueError(f"INVALID_PARAMS:{key} must be an integer")
    spec = SCHEMA[key]
    if not spec["minimum"] <= value <= spec["maximum"]:
        raise ValueError(f"INVALID_PARAMS:{key} must be in [{spec['minimum']}, {spec['maximum']}]")
    if value % GRID_MINUTES:
        raise ValueError(f"INVALID_PARAMS:{key} must be a multiple of {GRID_MINUTES} (15m candle grid)")
    return value


def _require_number(params, key):
    value = params.get(key, SCHEMA[key]["default"])
    if type(value) not in (int, float) or value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"INVALID_PARAMS:{key} must be a number")
    spec = SCHEMA[key]
    if not spec["minimum"] <= value <= spec["maximum"]:
        raise ValueError(f"INVALID_PARAMS:{key} must be in [{spec['minimum']}, {spec['maximum']}]")
    return value


class FundingReversalConfig:
    """Duck-types MacroConfig for make_macro_strategy; ``events`` is bound after the funding fetch."""

    kind = FUNDING_KIND

    def __init__(self, values):
        self.values = values
        self.events = ()
        # Filled in place by bind(); the provider hands this same dict to assumptions at build time.
        self.assumptions = {}

    @classmethod
    def parse(cls, params):
        lead = _require_integer(params, "lead_minutes")
        hold = _require_integer(params, "hold_minutes")
        threshold = _require_number(params, "rate_threshold_pct")
        stop = _require_number(params, "stop_loss_pct")
        values = dict(
            lead_minutes=lead,
            hold_minutes=hold,
            rate_threshold_pct=threshold,
            stop_loss_pct=stop,
            # Engine view of the same parameters (surprise engine keys).
            surprise_threshold=threshold,
            direction_map=dict(below="long", above="short"),
        )
        return cls(values)

    @property
    def hold_minutes(self):
        return self.values["hold_minutes"]

    # ---- planning -------------------------------------------------------------------------------
    def settlement_points(self, start_at, end_at):
        """Settlement instants S (unix s) whose entry ``S - lead`` lies in [start_at, end_at)."""
        lead = self.values["lead_minutes"] * 60
        first = -(-(start_at + lead) // SLOT_SECONDS) * SLOT_SECONDS
        points = []
        settlement = first
        while settlement - lead < end_at:
            points.append(settlement)
            settlement += SLOT_SECONDS
        return points

    def fetch_range_ms(self, points, now_ms):
        """Inclusive [start_ms, end_ms] covering only the PREVIOUS-period rows of ``points``."""
        first_previous = points[0] - SLOT_SECONDS
        last_previous = points[-1] - SLOT_SECONDS
        return first_previous * 1000, min(last_previous * 1000 + JITTER_MS - 1, now_ms)

    # ---- binding --------------------------------------------------------------------------------
    def bind(self, points, history):
        """Turn the fetched history into engine events; fail closed on any gap.

        Only rows whose slot is a previous period of some point are ever read; a row of the
        settlement's own period is ignored even when the caller's history contains it.
        """
        symbol = history.get("symbol")
        regular = {}
        special_ignored = 0
        for row in history.get("rows", ()):
            if row.get("rate_type") != "Regular":
                special_ignored += 1
                continue
            time_ms = row["time_ms"]
            slot = time_ms // SLOT_MS
            if time_ms - slot * SLOT_MS >= JITTER_MS:
                raise FundingSeriesError(
                    "funding_interval_unsupported",
                    f"funding row at {_iso(time_ms // 1000)} is not on the 8h settlement cadence "
                    f"(00/08/16 UTC); this template only supports 8h funding symbols",
                    {"row_time_utc": _iso(time_ms // 1000), "symbol": symbol},
                )
            if slot in regular:
                raise FundingSeriesError(
                    "funding_interval_unsupported",
                    f"two funding rows in the 8h period starting {_iso(slot * SLOT_SECONDS)}",
                    {"period_utc": _iso(slot * SLOT_SECONDS), "symbol": symbol},
                )
            regular[slot] = row
        events, used = [], []
        for settlement in points:
            slot = settlement // SLOT_SECONDS - 1
            row = regular.get(slot)
            if row is None:
                raise FundingSeriesError(
                    "funding_data_gap",
                    f"funding series has no settled rate for the period starting "
                    f"{_iso(slot * SLOT_SECONDS)} (needed for the {_iso(settlement)} settlement); "
                    "this template does not run on a partial series",
                    {
                        "symbol": symbol,
                        "missing_period_utc": _iso(slot * SLOT_SECONDS),
                        "settlement_utc": _iso(settlement),
                        "required_first_period_utc": _iso(points[0] - SLOT_SECONDS),
                        "required_last_period_utc": _iso(points[-1] - SLOT_SECONDS),
                    },
                )
            try:
                rate = Decimal(str(row["rate"]))
            except (InvalidOperation, KeyError):
                rate = None
            if rate is None or not rate.is_finite():
                raise FundingSeriesError(
                    "funding_data_gap", f"funding row for the period {_iso(slot * SLOT_SECONDS)} is unreadable",
                    {"symbol": symbol, "period_utc": _iso(slot * SLOT_SECONDS)})
            rate_pct = rate * 100
            lead = self.values["lead_minutes"] * 60
            entry_ts = _utc(settlement - lead)
            events.append((
                entry_ts,
                f"funding_settlement_{_iso(settlement)}",
                "0",
                str(rate_pct),
                dict(
                    settlement_utc=_iso(settlement),
                    judged_rate_period_utc=_iso(slot * SLOT_SECONDS),
                    judged_rate_time_ms=row["time_ms"],
                    judged_rate=str(rate),
                    judged_rate_pct=str(rate_pct),
                    rate_basis="previous_settled_rate",
                ),
            ))
            used.append(row)
        self.events = tuple(events)
        first_period = points[0] - SLOT_SECONDS
        last_period = points[-1] - SLOT_SECONDS
        self.assumptions.update(
            lead_minutes=self.values["lead_minutes"],
            hold_minutes=self.values["hold_minutes"],
            rate_threshold_pct=self.values["rate_threshold_pct"],
            stop_loss_pct=self.values["stop_loss_pct"],
            rate_basis="previous_settled_rate",
            rate_rule="previous settled rate >= +rate_threshold_pct -> short; <= -rate_threshold_pct -> long; "
                      "in between no trade; boundary values trade",
            judgement=DEVIATION_NOTE,
            deviation_note=DEVIATION_NOTE,
            entry="open of the 15m candle that starts lead_minutes before the settlement "
                  "(market order queued at the previous candle close)",
            exit="stop_loss_pct by candle high/low, or the first candle close at or after "
                 "entry + hold_minutes then the next open; one trade per settlement",
            data_source="binance_usdm_public_funding_history",
            warmup_bars=0,
            funding_series=dict(
                **SOURCE,
                symbol=symbol,
                source=history.get("source"),
                interval_hours=8,
                first_period_utc=_iso(first_period),
                last_period_utc=_iso(last_period),
                first_ts=first_period,
                last_ts=last_period,
                count=len(used),
                rows_sha256=history.get("rows_sha256"),
                fetched_at_ms=history.get("fetched_at_ms"),
                special_rows_ignored=special_ignored,
                gap_policy="fail_closed",
                current_period_rate_used=False,
            ),
            settlement_points=dict(
                count=len(points), first_utc=_iso(points[0]), last_utc=_iso(points[-1])),
        )

    def unbound_assumptions(self):
        """Assumptions for a window with no settlement point (nothing fetched, no events)."""
        self.assumptions.update(
            lead_minutes=self.values["lead_minutes"],
            hold_minutes=self.values["hold_minutes"],
            rate_threshold_pct=self.values["rate_threshold_pct"],
            stop_loss_pct=self.values["stop_loss_pct"],
            rate_basis="previous_settled_rate",
            judgement=DEVIATION_NOTE,
            deviation_note=DEVIATION_NOTE,
            data_source="binance_usdm_public_funding_history",
            warmup_bars=0,
            funding_series=dict(**SOURCE, count=0, gap_policy="fail_closed", current_period_rate_used=False),
            settlement_points=dict(count=0),
        )


def schema():
    return deepcopy(SCHEMA)
