"""定额分批账本（Feature 132 §3.1，Owner 2026-10-08 拍板）。

现货、只做多、1 倍的 Decimal 账本，不走 backtesting.py 引擎（引擎单仓 + 只扣手续费
不扣滑点，见 IMPL §二）。账本与指标无关：信号函数 ``signal(bar_index) -> "buy" |
"sell" | "hold"`` 由调用方注入，v1 只有 RSI 一个注册信号（provider 侧）。

口径（IMPL 决策表）：
- D2 第 t 根收盘判信号，第 t+1 根**开盘价**成交；数量 = 金额 ÷ 成交开盘价，Decimal 不取整。
- D4 买：现金扣掉手续费和滑点后不够买满一笔就跳过并计数。
- D3/D6 卖：有持仓时卖固定金额，持仓不足一笔就全部卖完；无持仓不动、不做空；FIFO
  先买先卖、按批次拆分，一条 trade = 一个买入批次或其被一次卖出卖掉的部分。
- D5 最后一根不判新信号；期末仍持有的批次按最后一根**收盘价**平仓，closed_at = end_at。
  成交时刻不早于 end_at 的那次信号也不判（end_at 恰好落在最后一根开盘时，否则信号成交的
  closed_at 会等于 end_at，与强平批次撞上——D5 要求 closed_at == end_at 唯一识别强平）。
- D8 曲线：起点；持仓期间每根收盘的按市值点；成交时刻的点（已实现累计 + 剩余批次按
  该时刻开盘价估值，同一 ts 成交点优先）；期末强平点 = 全部平仓后的已实现权益。
- 费用与 result.v2 旧模板同一公式：fee = (entry + exit) × qty × fee_bps / 10000，
  slippage 同理；成交价本身不含滑点（复核按 K 线高低区间核成交价）。

五条不变量在每次成交后与每根收盘后断言，违反抛 ``LedgerInvariantError``：
① 现金 + Σ批次数量 × 当根价格 = 权益（权益另按「本金 + 已实现 + 各批次浮动净值」独立算）；
② 批次数量和 = 累计买入 − 累计卖出；③ FIFO：被卖出批次的 opened_at 单调不降；
④ 现金永不为负；⑤ 同根不得既买又卖（D11）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Optional, Sequence

from canonical_json import canonical_decimal_str

BUY = "buy"
SELL = "sell"
HOLD = "hold"
_SIGNALS = frozenset({BUY, SELL, HOLD})
_BPS = Decimal(10000)
# 账本走 Decimal 默认上下文（28 位有效数字，与 result.v2 旧路径同一上下文），单次运算相对
# 舍入误差约 5e-28。误差界一律按**相对量**给：交易判断（拆批边界、是否清仓）乘本次目标数量，
# 不变量 ① 乘金额规模、② 乘数量规模，各自留约 1e9 倍余量给累积舍入；不用固定绝对容差
# （大额会误报、小额会把真实差额吞掉，见 Codex review 四条 P2）。
RELATIVE_ERROR_BOUND = Decimal("1e-18")

SignalFn = Callable[[int], str]


class LedgerInvariantError(RuntimeError):
    """账本不变量被破坏：是实现缺陷，不是用户输入错误（provider 映射为 ENGINE_ERROR）。"""


@dataclass(frozen=True)
class LedgerBar:
    open_time: int
    close_time: int
    open: Decimal
    close: Decimal


@dataclass
class _Lot:
    opened_at: int
    entry_price: Decimal
    qty: Decimal


@dataclass(frozen=True)
class BarSnapshot:
    """每根收盘推进后的账本状态，供用例逐根核不变量。"""

    index: int
    ts: int
    price: Decimal
    cash: Decimal
    lot_qty_sum: Decimal
    cum_bought: Decimal
    cum_sold: Decimal
    equity_by_cash: Decimal
    equity_by_pnl: Decimal
    money_tolerance: Decimal
    qty_tolerance: Decimal
    buys_this_bar: int
    sells_this_bar: int


@dataclass(frozen=True)
class LedgerResult:
    trades: list[dict[str, Any]]
    equity_points: list[tuple[int, Decimal]]
    fill_ts: frozenset[int]
    snapshots: list[BarSnapshot]
    buy_fills: int
    sell_fills: int
    skipped_buys_insufficient_cash: int
    final_cash: Decimal
    realized_pnl: Decimal


def threshold_signal(values: Sequence[float], *, buy_below: float, sell_above: float) -> SignalFn:
    """阈值类指标的信号：值 < buy_below 买，值 > sell_above 卖，否则不动（NaN 不动）。

    指标无关：RSI / CCI / ROC 这类阈值指标都能用。两个条件同时成立就是 D11 说的买卖
    冲突，参数校验（buy_below < sell_above）本应排除，这里仍断言。
    """
    def signal(index: int) -> str:
        value = float(values[index])
        if math.isnan(value):
            return HOLD
        wants_buy = value < buy_below
        wants_sell = value > sell_above
        if wants_buy and wants_sell:
            raise LedgerInvariantError(
                f"bar {index}: buy and sell conditions both hold (value={value})"
            )
        if wants_buy:
            return BUY
        if wants_sell:
            return SELL
        return HOLD

    return signal


class ScaleInOutLedger:
    """逐根推进的定额分批账本；一般经 ``run_scale_in_out`` 使用。"""

    def __init__(
        self,
        *,
        initial_capital: Decimal,
        buy_notional: Decimal,
        sell_notional: Decimal,
        fee_bps: Decimal,
        slippage_bps: Decimal,
    ) -> None:
        if initial_capital <= 0 or buy_notional <= 0 or sell_notional <= 0:
            raise ValueError("initial_capital / buy_notional / sell_notional must be positive")
        if fee_bps < 0 or slippage_bps < 0 or fee_bps + slippage_bps >= _BPS:
            raise ValueError("fee_bps and slippage_bps must be >= 0 and sum below 10000")
        self.initial_capital = initial_capital
        self.buy_notional = buy_notional
        self.sell_notional = sell_notional
        self.fee_rate = fee_bps / _BPS
        self.slippage_rate = slippage_bps / _BPS
        self.cost_rate = self.fee_rate + self.slippage_rate
        self.cash = initial_capital
        self.lots: list[_Lot] = []
        self.trades: list[dict[str, Any]] = []
        self.realized_pnl = Decimal(0)
        self.cum_bought = Decimal(0)
        self.cum_sold = Decimal(0)
        self.cum_traded_value = Decimal(0)  # 累计成交额（金额规模，供 ① 的误差界）
        self.buy_fills = 0
        self.sell_fills = 0
        self.skipped_buys = 0
        self.buys_this_bar = 0
        self.sells_this_bar = 0
        self.last_sold_opened_at: Optional[int] = None

    # -- 估值 --------------------------------------------------------------
    def lot_qty_sum(self) -> Decimal:
        return sum((lot.qty for lot in self.lots), Decimal(0))

    def equity_by_cash(self, price: Decimal) -> Decimal:
        return self.cash + sum((lot.qty * price for lot in self.lots), Decimal(0))

    def equity_by_pnl(self, price: Decimal) -> Decimal:
        """本金 + 已实现 + 各批次浮动净值（只扣已成交的开仓侧费用），与现金无关的另一条算法。"""
        unrealized = Decimal(0)
        for lot in self.lots:
            unrealized += (price - lot.entry_price) * lot.qty - lot.entry_price * lot.qty * self.cost_rate
        return self.initial_capital + self.realized_pnl + unrealized

    # -- 成交 --------------------------------------------------------------
    def begin_bar(self) -> None:
        self.buys_this_bar = 0
        self.sells_this_bar = 0

    def buy(self, ts: int, price: Decimal) -> bool:
        # 现金门槛与扣款按原始金额算；数量独立算，不用除法舍入后的数量反推（否则恰好足额会被
        # price*(notional/price) 的 1e-25 尾数判成不足）。
        notional = self.buy_notional
        charge = notional + notional * self.fee_rate + notional * self.slippage_rate
        if self.cash < charge:
            self.skipped_buys += 1
            return False
        qty = notional / price
        self.cash -= charge
        self.cum_traded_value += notional
        self.lots.append(_Lot(opened_at=ts, entry_price=price, qty=qty))
        self.cum_bought += qty
        self.buy_fills += 1
        self.buys_this_bar += 1
        return True

    def sell(self, ts: int, price: Decimal, *, sell_all: bool = False) -> bool:
        holding = self.lot_qty_sum()
        if holding <= 0:
            return False
        target = self.sell_notional / price
        # 本次运算误差界（数量量纲，按目标数量相对给）：只吸收 Decimal 舍入尾数，不改变交易决策。
        eps = target * RELATIVE_ERROR_BOUND
        # 持仓不足一笔就整批全部平掉，不靠减法凑零。
        remaining: Optional[Decimal] = None if (sell_all or holding <= target + eps) else target
        while self.lots:
            lot = self.lots[0]
            if remaining is None:
                portion = lot.qty
            else:
                if remaining <= eps:
                    break
                # 拆批边界：批次与剩余目标在误差界内相等就整批归入当前批，不为尾数跨到下一批。
                portion = lot.qty if lot.qty <= remaining + eps else remaining
                remaining -= portion
            self._close_portion(lot, portion, ts, price)
            self.cum_sold += portion
            if portion == lot.qty:
                self.lots.pop(0)
            else:
                lot.qty -= portion
        if not sell_all:
            self.sell_fills += 1
            self.sells_this_bar += 1
        return True

    def _close_portion(self, lot: _Lot, qty: Decimal, ts: int, price: Decimal) -> None:
        if self.last_sold_opened_at is not None and lot.opened_at < self.last_sold_opened_at:
            raise LedgerInvariantError(
                f"FIFO broken: sold lot opened_at={lot.opened_at} after {self.last_sold_opened_at}"
            )
        self.last_sold_opened_at = lot.opened_at
        entry = lot.entry_price
        fee = (entry + price) * qty * self.fee_rate
        slippage = (entry + price) * qty * self.slippage_rate
        pnl = (price - entry) * qty - fee - slippage
        exit_gross = price * qty
        self.cash += exit_gross - exit_gross * self.fee_rate - exit_gross * self.slippage_rate
        self.cum_traded_value += exit_gross
        self.realized_pnl += pnl
        self.trades.append({
            "opened_at": lot.opened_at,
            "closed_at": ts,
            "side": "long",
            "qty": qty,
            "entry_price": entry,
            "exit_price": price,
            "fee": fee,
            "slippage": slippage,
            "pnl": pnl,
        })

    # -- 不变量 ------------------------------------------------------------
    def money_tolerance(self, price: Decimal) -> Decimal:
        """① 的误差界：本金 + 累计成交额 + 当前持仓市值，乘相对误差界。"""
        scale = self.initial_capital + self.cum_traded_value + self.lot_qty_sum() * price
        return scale * RELATIVE_ERROR_BOUND

    def qty_tolerance(self) -> Decimal:
        """② 的误差界：累计买入数量乘相对误差界。"""
        return self.cum_bought * RELATIVE_ERROR_BOUND

    def check_invariants(self, price: Decimal, where: str) -> None:
        by_cash = self.equity_by_cash(price)
        by_pnl = self.equity_by_pnl(price)
        if abs(by_cash - by_pnl) > self.money_tolerance(price):
            raise LedgerInvariantError(f"{where}: cash+holdings {by_cash} != equity {by_pnl}")
        lot_sum = self.lot_qty_sum()
        if abs(lot_sum - (self.cum_bought - self.cum_sold)) > self.qty_tolerance():
            raise LedgerInvariantError(
                f"{where}: lot qty {lot_sum} != bought {self.cum_bought} - sold {self.cum_sold}"
            )
        if self.cash < 0:
            raise LedgerInvariantError(f"{where}: cash went negative ({self.cash})")
        if self.buys_this_bar and self.sells_this_bar:
            raise LedgerInvariantError(f"{where}: bought and sold on the same bar")
        # ③ 在每次拆批时（_close_portion）已即时断言。


def run_scale_in_out(
    bars: Sequence[LedgerBar],
    signal: SignalFn,
    *,
    initial_capital: Decimal,
    buy_notional: Decimal,
    sell_notional: Decimal,
    fee_bps: Decimal,
    slippage_bps: Decimal,
    start_at: int,
    end_at: int,
) -> LedgerResult:
    ledger = ScaleInOutLedger(
        initial_capital=initial_capital,
        buy_notional=buy_notional,
        sell_notional=sell_notional,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
    )
    points: dict[int, Decimal] = {}
    fill_ts: set[int] = set()
    snapshots: list[BarSnapshot] = []
    pending = HOLD
    last = len(bars) - 1
    for index, bar in enumerate(bars):
        ledger.begin_bar()
        filled = False
        if pending == BUY:
            filled = ledger.buy(bar.open_time, bar.open)
        elif pending == SELL:
            filled = ledger.sell(bar.open_time, bar.open)
        if filled:
            ledger.check_invariants(bar.open, f"bar {index} fill")
            fill_ts.add(bar.open_time)
            if bar.open_time > start_at:
                points[bar.open_time] = ledger.equity_by_pnl(bar.open)
        ledger.check_invariants(bar.close, f"bar {index} close")
        snapshots.append(BarSnapshot(
            index=index,
            ts=bar.close_time,
            price=bar.close,
            cash=ledger.cash,
            lot_qty_sum=ledger.lot_qty_sum(),
            cum_bought=ledger.cum_bought,
            cum_sold=ledger.cum_sold,
            equity_by_cash=ledger.equity_by_cash(bar.close),
            equity_by_pnl=ledger.equity_by_pnl(bar.close),
            money_tolerance=ledger.money_tolerance(bar.close),
            qty_tolerance=ledger.qty_tolerance(),
            buys_this_bar=ledger.buys_this_bar,
            sells_this_bar=ledger.sells_this_bar,
        ))
        if ledger.lots and start_at < bar.close_time < end_at:
            points[bar.close_time] = ledger.equity_by_pnl(bar.close)
        if index < last and bars[index + 1].open_time < end_at:
            pending = signal(index)
            if pending not in _SIGNALS:
                raise LedgerInvariantError(f"bar {index}: unknown signal {pending!r}")
        else:
            pending = HOLD  # D5：最后一根（及成交会落在 end_at 上的那根）不判新信号

    if ledger.lots and bars:
        ledger.sell(end_at, bars[-1].close, sell_all=True)
        fill_ts.add(end_at)
        points[end_at] = ledger.initial_capital + ledger.realized_pnl
        ledger.check_invariants(bars[-1].close, "end liquidation")
    if ledger.lots:
        raise LedgerInvariantError("lots remain after end liquidation")

    curve = [(start_at, initial_capital)]
    curve.extend((ts, points[ts]) for ts in sorted(points) if ts > start_at)
    return LedgerResult(
        trades=list(ledger.trades),
        equity_points=curve,
        fill_ts=frozenset(fill_ts),
        snapshots=snapshots,
        buy_fills=ledger.buy_fills,
        sell_fills=ledger.sell_fills,
        skipped_buys_insufficient_cash=ledger.skipped_buys,
        final_cash=ledger.cash,
        realized_pnl=ledger.realized_pnl,
    )


def result_v2_trades(trades: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """账本 trades → result.v2 冻结 10 键（seq 从 1 连续；按 closed_at、opened_at 稳定排序）。"""
    ordered = sorted(trades, key=lambda t: (t["closed_at"], t["opened_at"]))
    return [
        {
            "seq": seq,
            "opened_at": t["opened_at"],
            "closed_at": t["closed_at"],
            "side": t["side"],
            "qty": canonical_decimal_str(t["qty"]),
            "entry_price": canonical_decimal_str(t["entry_price"]),
            "exit_price": canonical_decimal_str(t["exit_price"]),
            "fee": canonical_decimal_str(t["fee"]),
            "slippage": canonical_decimal_str(t["slippage"]),
            "pnl": canonical_decimal_str(t["pnl"]),
        }
        for seq, t in enumerate(ordered, start=1)
    ]


def result_v2_equity_curve(points: Sequence[tuple[int, Decimal]]) -> list[dict[str, Any]]:
    return [{"ts": ts, "equity": canonical_decimal_str(equity)} for ts, equity in points]
