"""E4（TokenBeep BACKLOG「回测 provider 存量模板没有 next() 级预热门槛」）：
`ema_cross`/`rsi_reversal`/`macd`/`cci_rsi` 四个存量模板补 `next()` 级预热门槛，
写法照抄 `e7bd867`（`ema_trend_rsi`）已验证过的补丁形态：`min_bars` 提成闭包变量，
入场判定前加 `if len(self.data) < min_bars: return`。

评估见 TokenBeep 仓 `docs/reviews/2026-09-22-orch-batch/E4-回测预热门槛-第一段评估.md`：
`ema_cross`/`macd` 是 EWM 指标从第 0 根就产生有限值，`rsi_reversal`/`cci_rsi` 的 RSI 腿
是 Wilder EWM 且 `fillna(50.0)`，均不受 `isfinite()` 保护；`bollinger_*`/`breakout`/`roc`
靠 rolling NaN 天然已挡住，不在本次改动范围。

四条用例的数据构造方式：先用一段能让指标出现"早熟但看起来有效"的行情（价格跳变 /
连续下跌）在 `min_bars` 之前就满足入场条件，断言真实成交（`EntryBar`）不早于
`min_bars`。改动前（无门槛）四条必须失败——本次实施已跑过 baseline 红，见任务回报。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
from backtesting import Backtest

from cutie_backtesting_provider import (
    _build_cci_rsi,
    _build_ema_cross,
    _build_macd,
    _build_rsi_reversal,
)


def _frame(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        dict(
            Open=closes,
            High=[v + 1 for v in closes],
            Low=[v - 1 for v in closes],
            Close=closes,
            Volume=[1] * len(closes),
        ),
        index=pd.date_range("2026-01-01", periods=len(closes), freq="h"),
    )


def test_ema_cross_no_entry_before_min_bars():
    """ema_fast=3/ema_slow=20 -> min_bars=21。`crossover()` 是边沿触发（只在真正
    穿越那一刻成立），所以不能只造一次早熟穿越——那样门槛一挡，后面永远不会再
    触发，`trades` 会变空而不是"首笔延后"，测试就失去意义。构造两次穿越：
    第一次在 bar5（预热期内，EMA 从第 0 根就有限，`crossover()` 拦不住早熟信号）；
    第二次在 bar43（预热期后，真实应该成交）。

    变异自证：临时删掉 `next()` 里 `len(self.data) < min_bars` 的门槛，本用例
    须由绿转红（首笔 EntryBar 从 44 提前到 6），恢复后转回绿。
    """
    prefix = [200.0, 180.0, 160.0, 140.0, 120.0]  # idx0-4：先跌，建立 fast<slow
    spike = [300.0] * 3  # idx5-7：跳涨造第一次穿越（up-cross ~bar5，预热期内）
    decline = [300.0 - 6 * i for i in range(1, 36)]  # idx8-42：连续下跌，不再穿越
    spike2 = [2000.0] * 20  # idx43+：再跳涨造第二次穿越（预热期后）
    closes = prefix + spike + decline + spike2
    built = _build_ema_cross(dict(ema_fast=3, ema_slow=20))
    assert built["min_bars"] == 21
    df = _frame(closes)
    result = Backtest(df, built["strategy"], cash=100000, finalize_trades=True).run()
    trades = result["_trades"]
    assert len(trades) >= 1
    assert all(bar >= built["min_bars"] for bar in trades["EntryBar"])


def test_rsi_reversal_no_entry_before_min_bars():
    """rsi_period=5 -> min_bars=16。持续下跌使 Wilder RSI（`fillna(50.0)`）在
    bar1 就跌到 0（远低于 oversold=30），bar2 即成交，远早于 min_bars=16
    ——RSI 的 NaN 填充值恒有限，没有 isfinite 之类的保护。

    变异自证：临时删掉门槛，本用例须由绿转红（EntryBar 从 16+ 提前到 2），
    恢复后转回绿。
    """
    closes = [100.0, 95.0, 90.0, 85.0, 80.0, 75.0] + [75.0] * 20
    built = _build_rsi_reversal(dict(rsi_period=5, oversold=30, overbought=70))
    assert built["min_bars"] == 16
    df = _frame(closes)
    result = Backtest(df, built["strategy"], cash=100000, finalize_trades=True).run()
    trades = result["_trades"]
    assert len(trades) >= 1
    assert all(bar >= built["min_bars"] for bar in trades["EntryBar"])


def test_macd_no_entry_before_min_bars():
    """fast=5/slow=10/signal=3 -> min_bars=34。`crossover()` 边沿触发，同 ema_cross
    的道理，需要两次穿越：第一次在 bar5（预热期内，双 EWM 差值从第 0 根就有限，
    `crossover()` 拦不住早熟信号）；第二次在 bar44（预热期后，真实应该成交）。

    变异自证：临时删掉门槛，本用例须由绿转红（首笔 EntryBar 从 44 提前到 6），
    恢复后转回绿。
    """
    prefix = [200.0, 180.0, 160.0, 140.0, 120.0]  # idx0-4
    spike = [300.0] * 3  # idx5-7：第一次穿越（预热期内）
    decline = [300.0 - 6 * i for i in range(1, 36)]  # idx8-42：连续下跌，不再穿越
    spike2 = [2000.0] * 20  # idx43+：第二次穿越（预热期后）
    closes = prefix + spike + decline + spike2
    built = _build_macd(dict(fast=5, slow=10, signal=3))
    assert built["min_bars"] == 34
    df = _frame(closes)
    result = Backtest(df, built["strategy"], cash=100000, finalize_trades=True).run()
    trades = result["_trades"]
    assert len(trades) >= 1
    assert all(bar >= built["min_bars"] for bar in trades["EntryBar"])


def test_cci_rsi_no_entry_before_min_bars():
    """cci_period=5/rsi_period=20（故意 rsi_period > cci_period）-> min_bars=61。
    入场判定是状态检查（非边沿触发），但同样需要两段满足条件的行情：第一段在
    bar10-11（预热期内）验证早熟信号被放行会误入场；中间用长时间回升到平价
    让 CCI/RSI 都恢复中性，排除"第一段仓位一直开着"的干扰；第二段在 bar65-66
    （预热期后）验证条件成立时仍能正常入场。

    第一段：长时间持平后单根巨幅下跌，CCI 在 bar10 就跌破 -100（-333.33），
    RSI（Wilder EWM + fillna(50)）同一根已跌到 0，两条件同时成立——现有
    `isfinite(cci) and isfinite(rsi)` 只挡住 CCI 自身的 rolling NaN 暖机期，
    挡不住 RSI 腿在 `rsi_period > cci_period` 时的早熟信号（见评估文档 §2）。

    变异自证：临时删掉门槛，本用例须由绿转红（首笔 EntryBar 从 66 提前到 12），
    恢复后转回绿。
    """
    flat1 = [100.0] * 10
    drop1 = [1.0] * 15  # bar10-11 条件成立（预热期内）
    recover = [100.0] * 40  # 回升持平，让 CCI/RSI 恢复中性、越过 min_bars=61
    drop2 = [1.0] * 15  # bar65-66 条件再次成立（预热期后）
    closes = flat1 + drop1 + recover + drop2
    built = _build_cci_rsi(
        dict(
            cci_period=5,
            rsi_period=20,
            cci_oversold=-100,
            cci_overbought=100,
            rsi_oversold=30,
            rsi_overbought=70,
        )
    )
    assert built["min_bars"] == 61
    df = _frame(closes)
    result = Backtest(df, built["strategy"], cash=100000, finalize_trades=True).run()
    trades = result["_trades"]
    assert len(trades) >= 1
    assert all(bar >= built["min_bars"] for bar in trades["EntryBar"])
