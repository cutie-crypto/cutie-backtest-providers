"""P2/P5: pre-settlement funding-rate reversal (Binance USD-M perpetual, 15m only).

At every 8h settlement point S (00/08/16 UTC) the template enters ``lead_minutes`` before S, against
the sign of the PREVIOUS PERIOD's funding rate, and holds ``hold_minutes`` (or until the percent stop).

P5 data path: the rate comes from the central ``/metrics`` 1h series (symbol = base coin, exchange
Binance, metric ``funding_rate``, value already in percent). The rate of a period P (= S - 8h) is the
close of the 1h bar opened at P - 1h, i.e. the last hour BEFORE that period settled (a small, documented
deviation from the settled value). Look-ahead rule: the judgement at S reads the row ts = S - 8h - 1h,
never the row of S's own period (ts = S - 1h, available only at S). The fetch range ends before the
last settlement's own-period row, so it is never even requested.

The trading engine is the Q18 surprise engine (strategy_macro_events): ``actual`` = previous-period rate
in percent (used as stored, no rescale), ``expected`` = 0, threshold = ``rate_threshold_pct``;
``rate >= +threshold`` -> short, ``rate <= -threshold`` -> long. This module only plans the events,
validates the series (fail-closed) and reports the evidence; it never touches the network itself.
"""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from strategy_macro_events import FUNDING_KIND

SLOT_SECONDS = 8 * 3600
SERIES_STEP_SECONDS = 3600
GRID_MINUTES = 15
SOURCE = {"exchange": "binance", "market": "usdt_perpetual"}
# Central /metrics labels, passed through byte-exact (symbol is the base coin, see central_symbol()).
CENTRAL_EXCHANGE = "Binance"
CENTRAL_METRIC = "funding_rate"
CENTRAL_INTERVAL = "1h"
RATE_BASIS = "previous_period_pre_settlement_1h_close"
DATA_SOURCE = "central_metrics:coinglass_funding_rate_1h"
SERIES_SOURCE = "central_metrics /metrics Binance funding_rate 1h (CoinGlass)"

SCHEMA = {
    "lead_minutes": {"type": "integer", "default": 30, "minimum": 15, "maximum": 120},
    "hold_minutes": {"type": "integer", "default": 30, "minimum": 15, "maximum": 240},
    "rate_threshold_pct": {"type": "number", "default": 0.05, "minimum": 0.005, "maximum": 1},
    "stop_loss_pct": {"type": "number", "default": 1, "minimum": 0.1, "maximum": 10},
}
DEVIATION_NOTE_ZH = (
    "费率来源：CoinGlass 结算前 1 小时收盘费率（与币安结算值有微小偏差，90 期样本最大绝对偏差 0.0017 pct）；"
    "仅覆盖中心库 1h 序列范围（当前自 2026-01-08 起）"
)
DEVIATION_NOTE_EN = (
    "Rate source: CoinGlass funding rate, close of the last 1h bar before settlement (a small deviation "
    "from the Binance settled value; maximum absolute deviation 0.0017 pct over a 90-period sample); "
    "only covers the central 1h series range (currently from 2026-01-08)"
)
DEVIATION_NOTE = f"{DEVIATION_NOTE_ZH} ({DEVIATION_NOTE_EN})"


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

    def fetch_range(self, points):
        """Half-open ``[start_at, end_at)`` seconds covering only the previous-period rows of ``points``.

        The first needed row is ``points[0] - 8h - 1h``; the last is ``points[-1] - 8h - 1h``. The range
        ends right after that row (``points[-1] - 8h``), so the last settlement's own-period row
        (``points[-1] - 1h``) is never requested.
        """
        return (points[0] - SLOT_SECONDS - SERIES_STEP_SECONDS, points[-1] - SLOT_SECONDS)

    @staticmethod
    def iso(seconds):
        return _iso(seconds)

    @staticmethod
    def required_period_details(points):
        return {
            "required_first_period_utc": _iso(points[0] - SLOT_SECONDS),
            "required_last_period_utc": _iso(points[-1] - SLOT_SECONDS),
        }

    # ---- binding --------------------------------------------------------------------------------
    def bind(self, points, series):
        """Turn the fetched central series into engine events; fail closed on any gap.

        Only the row ``ts = S - 8h - 1h`` of each point is ever read; the series also holds the 1h rows
        in between (including other points' own-period rows) and they are ignored.
        ``series``: symbol (Binance perp symbol), central_symbol, rows (``_fetch_metric_series`` rows),
        fetched_at_ms.
        """
        symbol = series.get("symbol")
        by_ts = {row["ts"]: row for row in series.get("rows", ())}
        events, used = [], []
        for settlement in points:
            period = settlement - SLOT_SECONDS
            row = by_ts.get(period - SERIES_STEP_SECONDS)
            if row is None:
                raise FundingSeriesError(
                    "funding_data_gap",
                    f"funding series has no pre-settlement 1h rate for the period starting "
                    f"{_iso(period)} (needed for the {_iso(settlement)} settlement); "
                    "this template does not run on a partial series",
                    {
                        "symbol": symbol,
                        "missing_period_utc": _iso(period),
                        "settlement_utc": _iso(settlement),
                        "required_first_period_utc": _iso(points[0] - SLOT_SECONDS),
                        "required_last_period_utc": _iso(points[-1] - SLOT_SECONDS),
                    },
                )
            try:
                rate_pct = Decimal(str(row["value"]))
            except (InvalidOperation, KeyError):
                rate_pct = None
            if rate_pct is None or not rate_pct.is_finite():
                raise FundingSeriesError(
                    "funding_data_gap", f"funding row for the period {_iso(period)} is unreadable",
                    {"symbol": symbol, "period_utc": _iso(period)})
            lead = self.values["lead_minutes"] * 60
            entry_ts = _utc(settlement - lead)
            events.append((
                entry_ts,
                f"funding_settlement_{_iso(settlement)}",
                "0",
                str(row["value"]),
                dict(
                    settlement_utc=_iso(settlement),
                    judged_rate_period_utc=_iso(period),
                    judged_rate_row_ts=row["ts"],
                    judged_rate_row_utc=_iso(row["ts"]),
                    judged_rate_time_ms=row["ts"] * 1000,
                    judged_rate=format(rate_pct.scaleb(-2), "f"),
                    judged_rate_pct=str(row["value"]),
                    rate_basis=RATE_BASIS,
                ),
            ))
            used.append(row)
        self.events = tuple(events)
        first_period = points[0] - SLOT_SECONDS
        last_period = points[-1] - SLOT_SECONDS
        rows = series.get("rows", ())
        self.assumptions.update(
            lead_minutes=self.values["lead_minutes"],
            hold_minutes=self.values["hold_minutes"],
            rate_threshold_pct=self.values["rate_threshold_pct"],
            stop_loss_pct=self.values["stop_loss_pct"],
            rate_basis=RATE_BASIS,
            rate_rule="previous-period pre-settlement 1h-close rate (pct) >= +rate_threshold_pct -> short; "
                      "<= -rate_threshold_pct -> long; in between no trade; boundary values trade",
            judgement=DEVIATION_NOTE,
            deviation_note=DEVIATION_NOTE,
            entry="open of the 15m candle that starts lead_minutes before the settlement "
                  "(market order queued at the previous candle close)",
            exit="stop_loss_pct by candle high/low, or the first candle close at or after "
                 "entry + hold_minutes then the next open; one trade per settlement",
            data_source=DATA_SOURCE,
            warmup_bars=0,
            funding_series=dict(
                **SOURCE,
                symbol=symbol,
                central_symbol=series.get("central_symbol"),
                central_exchange=CENTRAL_EXCHANGE,
                central_metric=CENTRAL_METRIC,
                source=SERIES_SOURCE,
                unit="pct",
                series_interval=CENTRAL_INTERVAL,
                series_interval_hours=1,
                settlement_interval_hours=SLOT_SECONDS // 3600,
                settlement_cadence_verified=False,
                row_rule="row ts = judged period start - 1h (1h bar close)",
                first_period_utc=_iso(first_period),
                last_period_utc=_iso(last_period),
                first_ts=used[0]["ts"],
                last_ts=used[-1]["ts"],
                count=len(used),
                rows_fetched=len(rows),
                rows_sha256=rows[0]["revision"] if rows else None,
                fetched_at_ms=series.get("fetched_at_ms"),
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
            rate_basis=RATE_BASIS,
            judgement=DEVIATION_NOTE,
            deviation_note=DEVIATION_NOTE,
            data_source=DATA_SOURCE,
            warmup_bars=0,
            funding_series=dict(**SOURCE, count=0, gap_policy="fail_closed", current_period_rate_used=False),
            settlement_points=dict(count=0),
        )


def schema():
    return deepcopy(SCHEMA)
