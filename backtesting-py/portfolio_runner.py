"""最小注入式每日 portfolio runner；无取数、选池、风险层或目录注册。"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Sequence

from portfolio_ledger import OpenPrices, PortfolioLedger, SpotSpec, decimal_text, timestamp
from portfolio_result_v4 import assemble_result_v4


@dataclass(frozen=True)
class DailyBar:
    ts: int  # 原始开盘 Unix 秒 / v4 UTC 日标签；close 按所属日标签快照。
    open_prices: Mapping[str, Decimal]
    close_prices: Mapping[str, Decimal]


@dataclass(frozen=True)
class PortfolioRun:
    result: dict
    rejections: list[dict]  # 诊断放外层，不能给 v4 闭键集添字段。
    remaining_cost_basis: Mapping[str, Decimal]


def run_portfolio(*, initial_cash: Decimal, specs: Mapping[str, SpotSpec], bars: Sequence[DailyBar],
                  target_weights: Mapping[int, Mapping[str, Decimal]], fee_bps: Decimal,
                  slippage_bps: Decimal = Decimal(0), btc_benchmark: Sequence[dict],
                  price_manifests: Sequence[dict], metric_series: Mapping | None = None,
                  coin_pool: Mapping | None = None) -> PortfolioRun:
    """首点为交易前本金；第 i 日收盘决策只在第 i+1 根原始开盘成交。

    每日 NAV 用该日 close，以 UTC 日标签报送。BTC 基准注入 {ts,close_price}。
    若传 C2 币池，只读 symbols 原顺序，不自行选池/重排；末点决策无下一开盘则记原因。
    """
    if not bars:
        raise ValueError("daily bars required")
    if coin_pool is not None:
        symbols = [entry["symbol"] for entry in coin_pool["symbols"]]
        if len(set(symbols)) != len(symbols) or set(symbols) != set(specs):
            raise ValueError("frozen pool must exactly cover trusted specs")
        specs = {s: specs[s] for s in symbols}
    times = []
    for i, bar in enumerate(bars):
        timestamp(bar.ts)
        if bar.ts % 86400 or (i and bar.ts != bars[i-1].ts + 86400):
            raise ValueError("bars must have consecutive UTC daily labels")
        times.append(bar.ts)
    if set(target_weights) - set(times):
        raise ValueError("decision table contains unknown timestamps")
    ledger = PortfolioLedger(initial_cash, specs, fee_bps, slippage_bps)
    snapshots, curve = [], []
    for i, bar in enumerate(bars):
        ledger._prices(bar.open_prices)  # 即使首根不交易也拒绝无效注入行情。
        if i and bars[i-1].ts in target_weights:
            ledger.rebalance(bars[i-1].ts, target_weights[bars[i-1].ts], OpenPrices(bar.ts, bar.open_prices))
        mark = ledger.mark(bar.close_prices)
        snapshots.append(mark.snapshot(bar.ts))
        curve.append({"ts": bar.ts, "equity": decimal_text(mark.equity)})
    if bars[-1].ts in target_weights:
        ledger.rejections.append({"decision_ts": bars[-1].ts, "reason": "no_next_open"})
    result = assemble_result_v4(initial_cash=initial_cash, snapshots=snapshots, equity_curve=curve,
                               fills=ledger.fills, btc_benchmark=btc_benchmark, price_manifests=price_manifests,
                               metric_series=metric_series)
    return PortfolioRun(result, ledger.rejections, ledger.cost_basis())
