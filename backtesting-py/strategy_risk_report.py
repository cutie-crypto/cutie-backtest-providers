"""F108 optional risk-cycle replay, separate from the frozen result.v2 schema."""

from decimal import Decimal

from canonical_json import canonical_decimal_str as dec
from canonical_json import canonical_json_sha256
from funding_history import fetch_history, funding_paid

SCHEMA = "cutie.strategy_risk_result.v1"


def build_risk_report(
    result_v2,
    policy,
    *,
    market,
    symbol,
    exchange,
    fee_bps,
    slippage_bps,
    history_loader=fetch_history
):
    if (
        set(policy) != {"schema", "direction", "leverage"}
        or policy["schema"] != "cutie.strategy_risk_policy.v1"
    ):
        raise ValueError("unsupported risk policy")
    direction, leverage = policy["direction"], policy["leverage"]
    if direction not in ("long", "short") or type(leverage) is not int:
        raise ValueError("invalid risk direction/leverage")
    if not 1 <= leverage <= (5 if direction == "long" else 3):
        raise ValueError("risk leverage exceeds direction cap")
    if market == "spot" and (direction != "long" or leverage != 1):
        raise ValueError("spot risk replay requires unleveraged long")
    if market not in ("spot", "futures") or (
        market == "futures" and exchange != "binance"
    ):
        raise ValueError("risk replay funding venue unsupported")
    trades = result_v2["trades"]
    previous_close = None
    for trade in trades:
        if trade["side"] != direction:
            raise ValueError("single-side replay requires a new single-side backtest")
        if trade["opened_at"] > trade["closed_at"] or (
            previous_close is not None and trade["opened_at"] < previous_close
        ):
            raise ValueError("risk replay requires non-overlapping full trades")
        previous_close = trade["closed_at"]
    history = None
    if market == "futures" and trades:
        history = history_loader(
            symbol.replace("/", "").split(":")[0],
            trades[0]["opened_at"] * 1000,
            trades[-1]["closed_at"] * 1000,
        )
    equity = peak = Decimal(1)
    losses = 0
    reason = None
    executed = []
    for trade in trades:
        qty, entry = Decimal(trade["qty"]), Decimal(trade["entry_price"])
        if not qty.is_finite() or not entry.is_finite() or qty <= 0 or entry <= 0:
            raise ValueError("invalid risk trade quantity/entry")
        funding = (
            Decimal(0)
            if history is None
            else funding_paid(
                history,
                [
                    {
                        "entry_ms": trade["opened_at"] * 1000,
                        "exit_ms": trade["closed_at"] * 1000,
                        "quantity": trade["qty"],
                    }
                ],
                direction,
            )
        )
        net_pnl = Decimal(trade["pnl"]) - funding
        net_return = net_pnl / (qty * entry / leverage)
        if not net_return.is_finite():
            raise ValueError("non-finite risk return")
        losses = losses + 1 if net_return < 0 else (0 if net_return > 0 else losses)
        equity = max(Decimal(0), equity * (1 + net_return))
        peak = max(peak, equity)
        drawdown = (peak - equity) / peak
        reason = (
            "net_drawdown"
            if drawdown >= Decimal(".20")
            else ("consecutive_losses" if losses >= 5 else None)
        )
        executed.append(
            {
                "source_seq": trade["seq"],
                "funding_paid": dec(funding),
                "net_return": dec(net_return),
                "equity": dec(equity),
                "drawdown": dec(drawdown),
                "consecutive_losses": losses,
                "warn_author": losses == 3 and net_return < 0,
            }
        )
        if reason:
            break
    payload = {
        "schema": SCHEMA,
        "base_result_sha256": canonical_json_sha256(result_v2),
        "policy": {
            **policy,
            "loss_limit": 5,
            "warning_losses": 3,
            "drawdown_limit": "0.2",
            "sizing": "cycle_equity_margin",
            "restore": "explicit_new_cycle",
            "funding_order": "entry_exclusive_exit_inclusive",
        },
        "costs": {
            "fee_bps": dec(fee_bps),
            "slippage_bps": dec(slippage_bps),
            "exchange": exchange,
            "market": market,
        },
        "funding_evidence": (
            None
            if history is None
            else {k: v for k, v in history.items() if k != "fetched_at_ms"}
        ),
        "trades": executed,
        "pause_reason": reason,
        "skipped_trade_count": len(trades) - len(executed),
        "final_equity": dec(equity),
    }
    return {"payload": payload, "sha256": canonical_json_sha256(payload)}
