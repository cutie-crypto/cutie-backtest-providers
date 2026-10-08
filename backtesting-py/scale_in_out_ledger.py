"""定额分批账本（Feature 132 §3.1，Owner 2026-10-08 拍板）。

现货、只做多、1 倍的定点 Decimal 账本，不走 backtesting.py 引擎（引擎单仓 + 只扣手续费
不扣滑点，见 IMPL §二）。账本与指标无关：信号函数 ``signal(bar_index) -> "buy" |
"sell" | "hold"`` 由调用方注入，v1 只有 RSI 一个注册信号（provider 侧）。

口径（IMPL 决策表）：
- D2 第 t 根收盘判信号，第 t+1 根**开盘价**成交。
- D4 买：成交额 + 手续费 + 滑点 > 现金就整笔跳过并计数。
- D3/D6 卖：有持仓时卖固定金额，持仓少于目标数量就全部卖完；无持仓不动、不做空；FIFO
  先买先卖、按批次拆分，一条 trade = 一个买入批次或其被一次卖出卖掉的部分。
- D5 最后一根不判新信号；期末仍持有的批次按最后一根**收盘价**平仓，closed_at = end_at。
  成交时刻不早于 end_at 的那次信号也不判（end_at 恰好落在最后一根开盘时，否则信号成交的
  closed_at 会等于 end_at，与强平批次撞上——D5 要求 closed_at == end_at 唯一识别强平）。
- D8 曲线：起点；持仓期间每根收盘的按市值点；成交时刻的点（现金 + 各批次按该时刻开盘价
  估值，同一 ts 成交点优先）；期末强平点 = 全部平仓后的现金。

定点量化（统领 2026-10-08 定，取代容差设计）：
- 全部运算在 ``PREC`` 位、**Inexact 设为陷阱**的上下文里做：加减乘一旦需要舍入就抛异常
  （转成 LedgerInvariantError），所以除下面两处量化外，账本里没有任何舍入，比较全部精确。
- 数量：每笔成交数量 = (金额 ÷ 成交价) 按 ``QTY_SCALE`` 向下取整。数量只由这一步产生，之后
  只做加减。买入的实际成交额 = 数量 × 成交价，比参数金额少不到 成交价 × 1e-12，这是交易所
  最小数量的正常口径（D9 的金额参数与规则句一致性不受影响）。数量取整为 0（金额不足成交价的
  1e-12）时不成交、不计入现金不足跳过数。卖出目标数量同样向下取整，卖 min(目标, 持仓)。
- 金额：成交额 = 数量 × 成交价（有限位相乘，精确）；手续费、滑点各自按 ``CASH_SCALE``
  四舍五入（ROUND_HALF_UP）。费用乘积用整数系数精确相乘后再量化（``round_cash_product``），
  不在 60 位上下文里先算乘积，免得合法费用在量化前被精度陷阱误杀。
- 卖出侧上界（统领 2026-10-08 定，是上界不是容差）：单笔卖出 fee + slippage 不得超过该笔
  成交额；fee 先取、slippage 取剩余，即 exit_fee = min(R(X·q·r_f), X·q)，
  exit_slippage = min(R(X·q·r_s), X·q − exit_fee)，所以现金永不为负。
- 开仓费用：买入时整批一次付清；批次被拆卖时按成交顺序分摊，**末笔承接全部未分摊额**（含此前
  各笔量化的累计差额），所以单笔分摊额与 E·qᵢ·r 的差可以超过半个 CASH_SCALE，但同批分摊之和
  精确等于实付。trade 的 fee / slippage = 分摊的开仓侧 + 本次卖出侧，pnl = (出场价 − 入场价)
  × 数量 − fee − slippage。

服务端复算公式（Codex 给出，与本实现逐项一致）：同一买入批次按成交顺序，R = ROUND_HALF_UP 到
1e-8，r = bps / 10000，E = 入场价，Q = Σqᵢ（该批买入数量），初始未分摊额 A = R(E·Q·r)；
非末笔 aᵢ = min(R(E·qᵢ·r), A)，末笔 aᵢ = A，每笔之后 A −= aᵢ；
feeᵢ = aᵢ(手续费) + min(R(Xᵢ·qᵢ·r_f), Xᵢ·qᵢ)；
slippageᵢ = aᵢ(滑点) + min(R(Xᵢ·qᵢ·r_s), Xᵢ·qᵢ − 本笔卖出侧手续费)。

五条不变量在每次成交后与每根收盘后**精确相等**断言，违反抛 ``LedgerInvariantError``：
① 现金 + Σ批次数量 × 当根价格 = 本金 + 已实现 + Σ[(当根价 − 入场价) × 数量 − 未分摊开仓费用]；
② 批次数量和 = 累计买入 − 累计卖出；③ FIFO：被卖出批次的 opened_at 单调不降；
④ 现金永不为负；⑤ 同根不得既买又卖（D11）。
"""
from __future__ import annotations

import functools
import math
from dataclasses import dataclass
from decimal import (
    ROUND_DOWN,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    Context,
    Decimal,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
    localcontext,
)
from typing import Any, Callable, Optional, Sequence

from canonical_json import canonical_decimal_str

BUY = "buy"
SELL = "sell"
HOLD = "hold"
_SIGNALS = frozenset({BUY, SELL, HOLD})
_BPS = Decimal(10000)

PREC = 60
QTY_SCALE = Decimal("1e-12")
CASH_SCALE = Decimal("1e-8")
_EXACT = Context(
    prec=PREC,
    rounding=ROUND_HALF_EVEN,
    traps=[InvalidOperation, DivisionByZero, Overflow, Inexact],
)

SignalFn = Callable[[int], str]


class LedgerInvariantError(RuntimeError):
    """账本不变量被破坏或出现了非精确运算：是实现缺陷，不是用户输入错误（provider 映射为 ENGINE_ERROR）。"""


def _exact(func):
    """在精确上下文里执行；任何需要舍入的运算都转成 LedgerInvariantError。"""
    @functools.wraps(func)
    def wrapped(*args, **kwargs):
        try:
            with localcontext(_EXACT):
                return func(*args, **kwargs)
        except Inexact as exc:
            raise LedgerInvariantError(f"inexact decimal arithmetic in {func.__name__}") from exc

    return wrapped


def floor_qty(amount: Decimal, price: Decimal) -> Decimal:
    """金额 ÷ 价格按 QTY_SCALE 向下取整（除法本身也按向下截断，避免两次舍入跨过边界）。"""
    with localcontext(Context(prec=PREC, rounding=ROUND_DOWN)):
        return (amount / price).quantize(QTY_SCALE, rounding=ROUND_DOWN)


def round_cash_product(*factors: Decimal) -> Decimal:
    """∏factors 按 CASH_SCALE 四舍五入（ROUND_HALF_UP）。

    用整数系数相乘（Python int 无精度上限），只在最后量化这一步舍入一次，结果用字符串构造
    （不经上下文），所以不受 PREC 限制、也不会触发精确上下文的 Inexact 陷阱。
    """
    coefficient = 1
    exponent = 0
    for factor in factors:
        if not factor.is_finite():
            raise ValueError("cash factors must be finite")
        sign, digits, exp = factor.as_tuple()
        value = int("".join(map(str, digits))) if digits else 0
        coefficient *= -value if sign else value
        exponent += exp
    shift = exponent - CASH_SCALE.as_tuple().exponent  # 以 1e-8 为单位的十进制位移
    if shift >= 0:
        units = coefficient * 10**shift
    else:
        divisor = 10 ** (-shift)
        whole, rest = divmod(abs(coefficient), divisor)
        whole += 1 if 2 * rest >= divisor else 0
        units = whole if coefficient >= 0 else -whole
    return Decimal(f"{units}E{CASH_SCALE.as_tuple().exponent}")


def round_cash(amount: Decimal) -> Decimal:
    return round_cash_product(amount)


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
    fee_left: Decimal
    slippage_left: Decimal


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

    @_exact
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
        self.fee_rate = fee_bps / _BPS  # 有限小数除以 10^4，精确
        self.slippage_rate = slippage_bps / _BPS
        self.cash = initial_capital
        self.lots: list[_Lot] = []
        self.trades: list[dict[str, Any]] = []
        self.realized_pnl = Decimal(0)
        self.cum_bought = Decimal(0)
        self.cum_sold = Decimal(0)
        self.buy_fills = 0
        self.sell_fills = 0
        self.skipped_buys = 0
        self.buys_this_bar = 0
        self.sells_this_bar = 0
        self.last_sold_opened_at: Optional[int] = None

    # -- 估值 --------------------------------------------------------------
    @_exact
    def lot_qty_sum(self) -> Decimal:
        return sum((lot.qty for lot in self.lots), Decimal(0))

    @_exact
    def equity_by_cash(self, price: Decimal) -> Decimal:
        return self.cash + sum((lot.qty * price for lot in self.lots), Decimal(0))

    @_exact
    def equity_by_pnl(self, price: Decimal) -> Decimal:
        """本金 + 已实现 + 各批次浮动净值（扣未分摊的开仓费用），与现金无关的另一条算法。"""
        unrealized = Decimal(0)
        for lot in self.lots:
            unrealized += (price - lot.entry_price) * lot.qty - lot.fee_left - lot.slippage_left
        return self.initial_capital + self.realized_pnl + unrealized

    # -- 成交 --------------------------------------------------------------
    def begin_bar(self) -> None:
        self.buys_this_bar = 0
        self.sells_this_bar = 0

    @_exact
    def buy(self, ts: int, price: Decimal) -> bool:
        qty = floor_qty(self.buy_notional, price)
        if qty == 0:
            return False
        gross = qty * price
        fee = round_cash_product(qty, price, self.fee_rate)
        slippage = round_cash_product(qty, price, self.slippage_rate)
        if gross + fee + slippage > self.cash:
            self.skipped_buys += 1
            return False
        self.cash -= gross + fee + slippage
        self.lots.append(_Lot(opened_at=ts, entry_price=price, qty=qty, fee_left=fee, slippage_left=slippage))
        self.cum_bought += qty
        self.buy_fills += 1
        self.buys_this_bar += 1
        return True

    @_exact
    def sell(self, ts: int, price: Decimal, *, sell_all: bool = False) -> bool:
        holding = self.lot_qty_sum()
        if holding == 0:
            return False
        remaining = holding if sell_all else min(floor_qty(self.sell_notional, price), holding)
        if remaining == 0:
            return False
        while remaining > 0:
            lot = self.lots[0]
            portion = min(lot.qty, remaining)
            self._close_portion(lot, portion, ts, price)
            remaining -= portion
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
        if qty == lot.qty:
            entry_fee, entry_slippage = lot.fee_left, lot.slippage_left
        else:
            entry_fee = min(round_cash_product(entry, qty, self.fee_rate), lot.fee_left)
            entry_slippage = min(round_cash_product(entry, qty, self.slippage_rate), lot.slippage_left)
        lot.fee_left -= entry_fee
        lot.slippage_left -= entry_slippage
        exit_gross = price * qty
        # 卖出侧上界：fee + slippage 不超过成交额，fee 先取、slippage 取剩余（现金永不为负）。
        exit_fee = min(round_cash_product(price, qty, self.fee_rate), exit_gross)
        exit_slippage = min(round_cash_product(price, qty, self.slippage_rate), exit_gross - exit_fee)
        self.cash += exit_gross - exit_fee - exit_slippage
        fee = entry_fee + exit_fee
        slippage = entry_slippage + exit_slippage
        pnl = (price - entry) * qty - fee - slippage
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

    # -- 不变量（全部精确相等） ------------------------------------------------
    @_exact
    def check_invariants(self, price: Decimal, where: str) -> None:
        by_cash = self.equity_by_cash(price)
        by_pnl = self.equity_by_pnl(price)
        if by_cash != by_pnl:
            raise LedgerInvariantError(f"{where}: cash+holdings {by_cash} != equity {by_pnl}")
        lot_sum = self.lot_qty_sum()
        if lot_sum != self.cum_bought - self.cum_sold:
            raise LedgerInvariantError(
                f"{where}: lot qty {lot_sum} != bought {self.cum_bought} - sold {self.cum_sold}"
            )
        if self.cash < 0:
            raise LedgerInvariantError(f"{where}: cash went negative ({self.cash})")
        if self.buys_this_bar and self.sells_this_bar:
            raise LedgerInvariantError(f"{where}: bought and sold on the same bar")
        # ③ 在每次拆批时（_close_portion）已即时断言。


@_exact
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
                points[bar.open_time] = ledger.equity_by_cash(bar.open)
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
            buys_this_bar=ledger.buys_this_bar,
            sells_this_bar=ledger.sells_this_bar,
        ))
        if ledger.lots and start_at < bar.close_time < end_at:
            points[bar.close_time] = ledger.equity_by_cash(bar.close)
        if index < last and bars[index + 1].open_time < end_at:
            pending = signal(index)
            if pending not in _SIGNALS:
                raise LedgerInvariantError(f"bar {index}: unknown signal {pending!r}")
        else:
            pending = HOLD  # D5：最后一根（及成交会落在 end_at 上的那根）不判新信号

    if ledger.lots and bars:
        ledger.sell(end_at, bars[-1].close, sell_all=True)
        fill_ts.add(end_at)
        points[end_at] = ledger.cash
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


@_exact
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


@_exact
def result_v2_equity_curve(points: Sequence[tuple[int, Decimal]]) -> list[dict[str, Any]]:
    return [{"ts": ts, "equity": canonical_decimal_str(equity)} for ts, equity in points]
