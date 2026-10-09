"""D 段现货组合账本：共享现金、FIFO 分币批次、差额交易与无容差核账。

财务状态使用 Decimal128 exact；数量下取在整数步数上精确截断。
受信规格由调用方提供。本模块不选币、不取数、不注册工具。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from decimal import Context, Decimal, DivisionByZero, Inexact, InvalidOperation, Overflow, ROUND_HALF_EVEN, localcontext
from fractions import Fraction
from typing import Mapping, Sequence

ZERO = Decimal(0)
BPS = Decimal(10000)
EXACT = Context(prec=34, rounding=ROUND_HALF_EVEN, Emin=-6143, Emax=6144, clamp=1,
                traps=[InvalidOperation, DivisionByZero, Overflow, Inexact])


class PortfolioInvariantError(RuntimeError):
    """非精确财务运算或独立账本不相等；禁止带容差继续运行。"""


def decimal_value(value: Decimal, name: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0 or (positive and value == 0):
        raise ValueError(f"{name} must be a finite {'positive' if positive else 'nonnegative'} Decimal")
    if len(value.as_tuple().digits) > 34 or len(format(value, 'f')) > 128:
        raise ValueError(f"{name} exceeds result.v4 decimal bounds")
    with localcontext(EXACT):
        return +value


def decimal_text(value: Decimal) -> str:
    # 不使用默认上下文的 normalize()，避免 28 位调用方上下文截断 34 位金额。
    if not value.is_finite():
        raise ValueError("nonfinite decimal")
    if value == 0:
        return "0"
    raw = format(value, "f")
    raw = raw.rstrip("0").rstrip(".") if "." in raw else raw
    if len(Decimal(raw).as_tuple().digits) > 34 or len(raw) > 128:
        raise ValueError("result.v4 decimal bounds exceeded")
    return raw


def timestamp(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 9007199254740991:
        raise ValueError("timestamp must be a positive safe integer")
    return value


def _floor_steps(amount: Decimal, unit_cost: Decimal, step: Decimal) -> Decimal:
    # Decimal 的无限循环除法不得先舍入再下取；直接对精确整数比取整。
    an, ad = amount.as_integer_ratio()
    un, ud = (unit_cost * step).as_integer_ratio()
    units = (an * ud) // (ad * un)
    return Decimal(units) * step


@dataclass(frozen=True)
class SpotSpec:
    qty_step: Decimal
    min_qty: Decimal
    min_notional: Decimal

    def __post_init__(self):
        decimal_value(self.qty_step, "qty_step", positive=True)
        decimal_value(self.min_qty, "min_qty")
        decimal_value(self.min_notional, "min_notional")


@dataclass(frozen=True)
class OpenPrices:
    ts: int
    prices: Mapping[str, Decimal]


@dataclass
class Lot:
    opened_at: int
    qty: Decimal
    price: Decimal
    unit_cost: Decimal  # 原始价格 + 每币开仓手续费、滑点；拆批不需舍入成本。


@dataclass(frozen=True)
class Mark:
    cash: Decimal
    positions: Mapping[str, Decimal]
    close_prices: Mapping[str, Decimal]
    equity: Decimal

    def snapshot(self, ts: int) -> dict:
        return {"ts": timestamp(ts), "cash": decimal_text(self.cash), "positions": [
            {"symbol": s, "qty": decimal_text(self.positions[s]), "close_price": decimal_text(self.close_prices[s])}
            for s in sorted(self.positions)
        ]}


def reconcile_snapshots(initial_cash: Decimal, snapshots: Sequence[dict], equity_curve: Sequence[dict],
                        fills: Sequence[dict]) -> None:
    """算法 A Decimal128 估值；算法 B 独立整数有理数流水重放，逐点比现金/数量/NAV。"""
    if not snapshots or len(snapshots) != len(equity_curve):
        raise PortfolioInvariantError("snapshot/curve length mismatch")
    cash = Fraction(initial_cash)
    symbols = [p["symbol"] for p in snapshots[0]["positions"]]
    quantities = dict.fromkeys(symbols, Fraction(0))
    index = 0
    with localcontext(EXACT):
        for snap, point in zip(snapshots, equity_curve):
            if snap["ts"] != point["ts"] or [p["symbol"] for p in snap["positions"]] != symbols:
                raise PortfolioInvariantError("snapshot timeline/symbol mismatch")
            while index < len(fills) and fills[index]["ts"] <= snap["ts"]:
                fill = fills[index]
                sign = 1 if fill["side"] == "buy" else -1
                cash -= sign * Fraction(fill["qty"]) * Fraction(fill["price"]) + Fraction(fill["fee"]) + Fraction(fill["slippage"])
                quantities[fill["symbol"]] += sign * Fraction(fill["qty"])
                if cash < 0 or quantities[fill["symbol"]] < 0:
                    raise PortfolioInvariantError("replay cannot borrow cash or quantity")
                index += 1
            nav = Decimal(snap["cash"]) + sum((Decimal(p["qty"]) * Decimal(p["close_price"]) for p in snap["positions"]), ZERO)
            replay_nav = cash + sum((quantities[p["symbol"]] * Fraction(p["close_price"]) for p in snap["positions"]), Fraction(0))
            if nav != Decimal(point["equity"]) or replay_nav != Fraction(point["equity"]):
                raise PortfolioInvariantError("equity reconciliation mismatch")
            if cash != Fraction(snap["cash"]) or any(quantities[p["symbol"]] != Fraction(p["qty"]) for p in snap["positions"]):
                raise PortfolioInvariantError("cash/quantity reconciliation mismatch")
        if index != len(fills):
            raise PortfolioInvariantError("unconsumed fills")


class PortfolioLedger:
    def __init__(self, initial_cash: Decimal, specs: Mapping[str, SpotSpec], fee_bps: Decimal,
                 slippage_bps: Decimal = ZERO):
        self.initial_cash = decimal_value(initial_cash, "initial_cash", positive=True)
        if not 1 <= len(specs) <= 30 or any(not isinstance(s, str) or not s for s in specs):
            raise ValueError("specs need 1..30 unique symbols")
        if any(not isinstance(spec, SpotSpec) for spec in specs.values()):
            raise ValueError("trusted SpotSpec required")
        with localcontext(EXACT):
            self.fee_rate = decimal_value(fee_bps, "fee_bps") / BPS
            self.slippage_rate = decimal_value(slippage_bps, "slippage_bps") / BPS
            if self.fee_rate + self.slippage_rate >= 1:
                raise ValueError("fee_bps + slippage_bps must be below 10000")
        self.specs = dict(specs)  # 冻结币池输入顺序用于同侧成交；输出仓位仍按 symbol 排序。
        self.cash = self.initial_cash
        self.lots = {s: [] for s in specs}
        self.fills = []
        self.rejections = []
        self._last_decision = 0
        self._last_fill_ts = 0

    def quantities(self) -> dict[str, Decimal]:
        with localcontext(EXACT):
            return {s: sum((lot.qty for lot in lots), ZERO) for s, lots in self.lots.items()}

    def cost_basis(self) -> dict[str, Decimal]:
        with localcontext(EXACT):
            return {s: sum((lot.qty * lot.unit_cost for lot in lots), ZERO) for s, lots in self.lots.items()}

    def _prices(self, prices: Mapping[str, Decimal]) -> dict[str, Decimal]:
        if set(prices) != set(self.specs):
            raise ValueError("prices must exactly cover trusted symbols")
        return {s: decimal_value(prices[s], f"price {s}", positive=True) for s in self.specs}

    def rebalance(self, decision_ts: int, target_weights: Mapping[str, Decimal], next_open_prices: OpenPrices) -> list[dict]:
        """固定成交前开盘 NAV 算目标；缺省币权重为零，先卖后按币池顺序买。

        买入先按含费用单价计算可付数量，再按 qty_step 下取；不重新分配下取余数。
        一期原子提交，输入/精度错误不留部分成交。未成交原因在 rejections。
        """
        timestamp(decision_ts)
        timestamp(next_open_prices.ts)
        if decision_ts <= self._last_decision or next_open_prices.ts <= decision_ts or decision_ts < self._last_fill_ts:
            raise ValueError("decision/open times must advance; execution requires next original open")
        if set(target_weights) - set(self.specs):
            raise ValueError("unknown target symbol")
        with localcontext(EXACT):
            weights = {s: decimal_value(target_weights.get(s, ZERO), f"weight {s}") for s in self.specs}
            if sum(weights.values(), ZERO) > 1:
                raise ValueError("target weights sum exceeds one")
            prices = self._prices(next_open_prices.prices)
            staged = deepcopy(self)
            start = len(staged.fills)
            staged._execute(decision_ts, weights, next_open_prices.ts, prices)
            staged.mark(prices)
            staged._last_decision = decision_ts
            staged._last_fill_ts = next_open_prices.ts
            self.__dict__.update(staged.__dict__)
            return deepcopy(self.fills[start:])

    def _execute(self, decision_ts, weights, ts, prices):
        holdings = self.quantities()
        nav = self.cash + sum((holdings[s] * prices[s] for s in self.specs), ZERO)
        targets = {s: nav * weights[s] for s in self.specs}
        for side in ("sell", "buy"):
            for symbol, spec in self.specs.items():
                held = self.quantities()[symbol]
                gap = targets[symbol] - held * prices[symbol]
                if (side == "buy" and gap <= 0) or (side == "sell" and gap >= 0):
                    continue
                qty = _floor_steps(abs(gap), prices[symbol], spec.qty_step)
                if side == "buy":
                    unit_cost = prices[symbol] * (1 + self.fee_rate + self.slippage_rate)
                    qty = min(qty, _floor_steps(self.cash, unit_cost, spec.qty_step))
                reason = None
                if qty == 0:
                    reason = "insufficient_cash" if side == "buy" and self.cash < prices[symbol] * spec.qty_step * (1 + self.fee_rate + self.slippage_rate) else "below_qty_step"
                elif qty < spec.min_qty:
                    reason = "below_min_qty"
                elif qty * prices[symbol] < spec.min_notional:
                    reason = "below_min_notional"
                if reason:
                    self.rejections.append({"decision_ts": decision_ts, "ts": ts, "symbol": symbol, "side": side, "reason": reason})
                    continue
                gross = qty * prices[symbol]
                fee, slip = gross * self.fee_rate, gross * self.slippage_rate
                if side == "buy":
                    self.cash -= gross + fee + slip
                    self.lots[symbol].append(Lot(ts, qty, prices[symbol], unit_cost))
                else:
                    self.cash += gross - fee - slip
                    left = qty
                    for lot in self.lots[symbol]:
                        take = min(left, lot.qty)
                        lot.qty -= take
                        left -= take
                        if left == 0:
                            break
                    self.lots[symbol] = [lot for lot in self.lots[symbol] if lot.qty]
                    if left:
                        raise PortfolioInvariantError("uncovered sale")
                if self.cash < 0 or any(q < 0 for q in self.quantities().values()):
                    raise PortfolioInvariantError("negative ledger balance")
                self.fills.append({"seq": len(self.fills) + 1, "ts": ts, "symbol": symbol, "side": side,
                                   "qty": decimal_text(qty), "price": decimal_text(prices[symbol]),
                                   "fee": decimal_text(fee), "slippage": decimal_text(slip)})

    def mark(self, close_prices: Mapping[str, Decimal]) -> Mark:
        with localcontext(EXACT):
            prices = self._prices(close_prices)
            quantities = self.quantities()
            equity = self.cash + sum((quantities[s] * prices[s] for s in self.specs), ZERO)
            mark = Mark(self.cash, quantities, prices, equity)
            ts = self.fills[-1]["ts"] if self.fills else 1
            reconcile_snapshots(self.initial_cash, [mark.snapshot(ts)], [{"ts": ts, "equity": decimal_text(equity)}], self.fills)
            return mark
