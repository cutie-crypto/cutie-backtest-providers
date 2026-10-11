"""C52: daily preaggregated exchange netflow; no I/O and no future fills in next()."""
from bisect import bisect_right
from decimal import Decimal, InvalidOperation

DAY = 86400
CENTRAL_EXCHANGE = "AGGREGATED"
CENTRAL_METRIC = "exchange_netflow"
GAP_REASON = "exchange_netflow_data_gap"
HISTORY_REASON = "exchange_netflow_history_unavailable"
SCHEMA = {
    "outflow_days": {"type": "integer", "default": 3, "minimum": 1, "maximum": 30},
    "inflow_exit_days": {"type": "integer", "default": 2, "minimum": 1, "maximum": 30},
    "stop_loss_pct": {"type": "number", "default": 3, "minimum": 0.1, "maximum": 50},
}


class NetflowCoverageError(ValueError):
    pass


class ExchangeNetflowConfig:
    def __init__(self, values):
        self.values = values
        self.rows = ()
        self.available_at = ()
        self.assumptions = {"exchange_netflow": {
            "balance_snapshots": "余额快照为上游日终值、按日期 +1 天可读；缺所当日不计入合计",
            "exchange": CENTRAL_EXCHANGE, "metric": CENTRAL_METRIC, "interval": "1d", "unit": "coin",
            "decision_time": "bar_close", "fill": "next_bar_open", "direction": "long",
            **values,
        }}

    @classmethod
    def parse(cls, params):
        values = {}
        for key, spec in SCHEMA.items():
            raw = params.get(key, spec["default"])
            try:
                value = Decimal(str(raw))
                if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not value.is_finite():
                    raise ValueError
                if not Decimal(str(spec["minimum"])) <= value <= Decimal(str(spec["maximum"])):
                    raise ValueError
                if spec["type"] == "integer":
                    if value != value.to_integral_value():
                        raise ValueError
                    values[key] = int(value)
                else:
                    values[key] = float(value)
            except (InvalidOperation, ValueError, TypeError):
                raise ValueError(f"INVALID_PARAMS:{key} must be a finite {spec['type']} in [{spec['minimum']}, {spec['maximum']}]")
        return cls(values)

    def required_timestamps(self, opens):
        # backtesting.py first calls next() on bar 1; the final bar has no next open.
        first = opens[1] - (max(self.values["outflow_days"], self.values["inflow_exit_days"]) - 1) * DAY
        return tuple(range(first, opens[-2] + DAY, DAY))

    def bind(self, rows, *, symbol, required, ignored_rows_count=0):
        try:
            if tuple(row["ts"] for row in rows) != required:
                raise NetflowCoverageError("required daily netflow dates are incomplete")
            bound = []
            for row in rows:
                value = Decimal(str(row["value"]))
                if not value.is_finite():
                    raise NetflowCoverageError("unreadable netflow value")
                # The upstream fetched_at/available_at never overrides this causal clock.
                bound.append({"ts": row["ts"], "value": value, "available_at": row["ts"] + DAY})
        except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
            raise NetflowCoverageError("unreadable netflow history") from exc
        self.rows = tuple(bound)
        self.available_at = tuple(row["available_at"] for row in bound)
        self.assumptions["exchange_netflow"].update(symbol=symbol, ignored_rows_count=ignored_rows_count)

    def window(self, decision_at, count):
        end = bisect_right(self.available_at, decision_at)
        if end < count:
            return ()
        return self.rows[end - count:end]

    def report(self, trades, decisions):
        # Only settled result.v2 rows supply fill/exit facts; next() records decisions alone.
        entries = {d["decision_at"]: d for d in decisions if d["action"] == "entry"}
        exits = {d["decision_at"]: d for d in decisions if d["action"] == "exit"}
        executions = []
        for trade in trades:
            entry = entries.get(trade["opened_at"])
            exit_decision = exits.get(trade["closed_at"])
            executions.append({"seq": trade["seq"], "opened_at": trade["opened_at"],
                "entry_price": trade["entry_price"], "closed_at": trade["closed_at"],
                "exit_price": trade["exit_price"], "signal_date": entry["series_date"] if entry else None,
                "stop_price": format(Decimal(trade["entry_price"]) *
                                     (1 - Decimal(str(self.values["stop_loss_pct"])) / 100), "f"),
                "exit_reason": exit_decision["reason"] if exit_decision else "final_close"})
        return {"decisions": decisions, "executions": executions}


def make_strategy(mixin, config, risk, initial_capital):
    from backtesting import Strategy

    class ExchangeNetflowStrategy(mixin, Strategy):
        _risk = risk
        _initial_capital = initial_capital

        def init(self):
            self.netflow_decisions = []
            self._risk_init()

        def _sizing_template_stop(self, order):
            price = Decimal(str(self._broker._adjusted_price(order.size, self.data.Open[-1])))
            return price * (1 - Decimal(str(config.values["stop_loss_pct"])) / 100)

        def next(self):
            if self._at_final_bar():
                return
            # D closes at D+1 00:00; only rows whose available_at <= that boundary may be read.
            decision_at = int(self.data.index[-1].timestamp()) + DAY
            if self.position:
                self._risk_exit_reason = None
                if self._risk_check_exit():
                    self.netflow_decisions.append({"decision_at": decision_at, "action": "exit",
                        "reason": self._risk_exit_reason or "risk_exit"})
                    return
                window = config.window(decision_at, config.values["inflow_exit_days"])
                if window and all(row["value"] > 0 for row in window):
                    self.position.close()
                    self.netflow_decisions.append({"decision_at": decision_at, "action": "exit",
                                                  "reason": "netflow_inflow"})
            elif not self.orders:
                window = config.window(decision_at, config.values["outflow_days"])
                if window and all(row["value"] < 0 for row in window):
                    if self._risk_open(True) is None:
                        self.netflow_decisions.append({"decision_at": decision_at, "action": "entry",
                                                      "series_date": window[-1]["ts"]})

    return ExchangeNetflowStrategy
