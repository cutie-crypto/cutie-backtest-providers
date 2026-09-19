"""Feature 50 三期 3b：策略布防入场判定器。

以 provider 源码为准手写移植（纯 Python，禁止引入 pandas/numpy——server 无此依赖）：
`cutie-backtest-providers/backtesting-py/cutie_backtesting_provider.py`
（2026-07-02 快照，函数行号见各判定器注释）。

判定器输入是已收盘 K 线序列，输出当前 bar 的条件结果；watcher 负责两轨边沿追踪，
process_trigger 负责持仓状态机。CCI+RSI 入场和出场使用独立判定器，EMA 出场沿用反向交叉。

已知与 provider 的映射关系（golden fixtures 见 tests/test_strategy_entry_evaluators.py）：
- ma_cross：provider `EmaCrossStrategy` 只做多（`crossover(fast, slow)` 开多，
  `crossover(slow, fast)` 平仓，从不开空）。本判定器把两个 crossover 事件都视为方向性
  入场信号——direction=long 用 `crossover(fast, slow)`，direction=short 用
  `crossover(slow, fast)`（趋势反转做空是标准解读，provider 只是回测口径长期以来只做多）。
- cci_rsi：provider `CciRsiStrategy` 多空双向对称，direction=long/short 直接对应
  provider `next()` 里的开多/开空条件表达式，无歧义。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class Bar:
    """已收盘 K 线（watcher 只传已收盘的 bar，见 tasks/strategy_entry_watcher.py）。"""

    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class TriggerResult:
    """判定器结果：显式动作、目标持仓方向、bar close_time 和审计指标。"""

    direction: str  # "long" | "short"
    trigger_bar_ts: int
    indicators: dict[str, float]
    action: str = "entry"  # entry | exit; exit.direction identifies the position to close


# ---------------------------------------------------------------------------
# 纯函数指标计算（对齐 provider _rsi_series / _cci_series / ewm span EMA）
# ---------------------------------------------------------------------------


def ema_series(values: list[float], span: int) -> list[float]:
    """EMA，等价于 pandas `Series(values).ewm(span=span, adjust=False).mean()`。

    实测验证（adjust=False 递推公式）：alpha=2/(span+1)；y[0]=x[0]（无前值用首个观测做种）；
    y[i]=alpha*x[i]+(1-alpha)*y[i-1]。对应 provider 第 452/457 行
    `pd.Series(x).ewm(span=self._ema_fast/_slow, adjust=False).mean()`。
    """
    if not values:
        return []
    alpha = 2.0 / (span + 1)
    out = [values[0]]
    prev = values[0]
    for v in values[1:]:
        prev = alpha * v + (1 - alpha) * prev
        out.append(prev)
    return out


def _wilder_smooth(values: list[Optional[float]], period: int) -> list[Optional[float]]:
    """Wilder 平滑（ewm alpha=1/period, adjust=False），None 表示 pandas 里的 NaN。

    实测验证：种子取序列里第一个非 None 值本身（不参与递推公式），此前的位置保持 None；
    此后每个非 None 值按 alpha*x+(1-alpha)*prev 递推。对应 provider 第 421-422 行
    `delta.clip(...).ewm(alpha=1.0/period, adjust=False).mean()`。
    """
    alpha = 1.0 / period
    out: list[Optional[float]] = [None] * len(values)
    prev: Optional[float] = None
    for i, v in enumerate(values):
        if v is None:
            out[i] = prev
            continue
        prev = v if prev is None else (alpha * v + (1 - alpha) * prev)
        out[i] = prev
    return out


def rsi_series(closes: list[float], period: int) -> list[float]:
    """Wilder RSI，对齐 provider `_rsi_series`（第 417-426 行）。

    NaN（首根 diff 无值 / gain=loss=0 的 0/0）一律 fill 50.0；loss=0 且 gain>0 → 100.0
    （inf 极限），与 provider 注释"loss==0 & gain>0 -> inf -> RSI 100; 0/0 -> NaN -> filled 50"
    完全一致。返回值永不为 None——RSI 序列全程可比较，watcher 侧不需要额外判空。
    """
    n = len(closes)
    if n == 0:
        return []
    deltas: list[Optional[float]] = [None] + [closes[i] - closes[i - 1] for i in range(1, n)]
    gains_raw = [None if d is None else max(d, 0.0) for d in deltas]
    losses_raw = [None if d is None else max(-d, 0.0) for d in deltas]
    gain = _wilder_smooth(gains_raw, period)
    loss = _wilder_smooth(losses_raw, period)

    result: list[float] = []
    for g, l in zip(gain, loss):
        if g is None or l is None:
            result.append(50.0)
            continue
        if l == 0:
            result.append(100.0 if g > 0 else 50.0)
            continue
        rs = g / l
        result.append(100 - (100 / (1 + rs)))
    return result


def cci_series(highs: list[float], lows: list[float], closes: list[float], period: int) -> list[Optional[float]]:
    """CCI，对齐 provider `_cci_series`（第 695-705 行）。

    provider 是**两段** rolling：`sma = tp.rolling(period).mean()` 先产生 period-1 根
    NaN，`mad = (tp - sma).abs().rolling(period).mean()` 是对"已经带 NaN 前缀"的序列
    再滚动一次——pandas rolling 窗口内只要有一个 NaN，整个窗口结果就是 NaN，所以 mad 的
    NaN 前缀会累加到 2*(period-1)，不是 sma 的 period-1（这是本模块唯一一处踩过坑的地方，
    调试详见 golden fixture `test_cci_series_matches_provider_rolling_cci`：直接用 tp 原始
    窗口算 mad 会在 index=period-1..2*period-3 给出错误的非 None 值）。mad=0（窗口内所有
    (tp-sma) 值相等且为 0，即 tp[i]==sma[i]）视为 0/0 → None，provider 靠 `math.isfinite`
    兜底跳过，本判定器直接返回 None 语义等价。
    """
    n = len(highs)
    if n == 0:
        return []
    tp = [(highs[i] + lows[i] + closes[i]) / 3.0 for i in range(n)]

    sma: list[Optional[float]] = [None] * n
    for i in range(period - 1, n):
        window = tp[i - period + 1 : i + 1]
        sma[i] = sum(window) / period

    dev: list[Optional[float]] = [None if sma[i] is None else abs(tp[i] - sma[i]) for i in range(n)]

    mad: list[Optional[float]] = [None] * n
    for i in range(period - 1, n):
        window = dev[i - period + 1 : i + 1]
        if any(w is None for w in window):
            continue
        mad[i] = sum(window) / period

    out: list[Optional[float]] = [None] * n
    for i in range(n):
        if sma[i] is None or mad[i] is None or mad[i] == 0:
            continue
        out[i] = (tp[i] - sma[i]) / (0.015 * mad[i])
    return out


# ---------------------------------------------------------------------------
# 判定器：纯函数 evaluate(params, bars, direction) -> TriggerResult | None
# ---------------------------------------------------------------------------


def evaluate_ma_cross(params: dict[str, Any], bars: list[Bar], direction: str) -> Optional[TriggerResult]:
    """对齐 provider `_build_ema_cross`（第 429-472 行）。

    参数名对齐 provider：`ema_fast`（默认 20）/ `ema_slow`（默认 60）——不是本地
    backtest_engine.py 原型的 fast_ma/slow_ma（review 三家实证：本地原型是 SMA，
    provider 是 EMA，参数名也不同，不可混用）。
    """
    ema_fast_n = int(params.get("ema_fast", 20) or 20)
    ema_slow_n = int(params.get("ema_slow", 60) or 60)
    if len(bars) < 2:
        return None
    closes = [b.close for b in bars]
    fast = ema_series(closes, ema_fast_n)
    slow = ema_series(closes, ema_slow_n)
    i = len(bars) - 1

    # backtesting.lib.crossover(series1, series2): series1[-2] < series2[-2] and
    # series1[-1] > series2[-1]（严格 < / >，见 provider 依赖库源码）。
    if direction == "long":
        crossed = fast[i - 1] < slow[i - 1] and fast[i] > slow[i]
    elif direction == "short":
        crossed = slow[i - 1] < fast[i - 1] and slow[i] > fast[i]
    else:
        return None
    if not crossed:
        return None
    return TriggerResult(
        direction=direction,
        trigger_bar_ts=bars[i].close_time,
        indicators={"ema_fast": fast[i], "ema_slow": slow[i]},
    )


def evaluate_cci_rsi(params: dict[str, Any], bars: list[Bar], direction: str) -> Optional[TriggerResult]:
    """对齐 provider `_build_cci_rsi`（第 708-764 行）。多空对称，direction 直接映射
    provider `next()` 里的开多/开空条件（第 748-752 行），不涉及 crossover 边沿判定
    （本身就是阈值条件），但 watcher 仍按边沿触发调用方语义使用（上一根不满足才算新触发，
    由 evaluate 只看当前 bar 是否满足条件，边沿判断交给调用方对比 last_evaluated_satisfied）。
    """
    cci_period = int(params.get("cci_period", 20) or 20)
    rsi_period = int(params.get("rsi_period", 14) or 14)
    cci_oversold = float(params.get("cci_oversold", -100))
    cci_overbought = float(params.get("cci_overbought", 100))
    rsi_oversold = float(params.get("rsi_oversold", 30))
    rsi_overbought = float(params.get("rsi_overbought", 70))
    if not bars:
        return None

    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    closes = [b.close for b in bars]
    cci = cci_series(highs, lows, closes, cci_period)
    rsi = rsi_series(closes, rsi_period)
    i = len(bars) - 1
    cci_v, rsi_v = cci[i], rsi[i]
    if cci_v is None or rsi_v is None or not math.isfinite(cci_v) or not math.isfinite(rsi_v):
        return None

    if direction == "long":
        satisfied = cci_v < cci_oversold and rsi_v < rsi_oversold
    elif direction == "short":
        satisfied = cci_v > cci_overbought and rsi_v > rsi_overbought
    else:
        return None
    if not satisfied:
        return None
    return TriggerResult(
        direction=direction,
        trigger_bar_ts=bars[i].close_time,
        indicators={"cci": cci_v, "rsi": rsi_v},
    )


def evaluate_cci_rsi_exit(params: dict[str, Any], bars: list[Bar], position_direction: str) -> Optional[TriggerResult]:
    """provider 持仓出场：多仓 CCI > 0 或 RSI > 50，空仓对称；watcher 负责边沿。"""
    if not bars:
        return None
    cci_period = int(params.get("cci_period", 20) or 20)
    rsi_period = int(params.get("rsi_period", 14) or 14)
    closes = [bar.close for bar in bars]
    cci_v = cci_series([bar.high for bar in bars], [bar.low for bar in bars], closes, cci_period)[-1]
    rsi_v = rsi_series(closes, rsi_period)[-1]
    if cci_v is None or not math.isfinite(cci_v) or not math.isfinite(rsi_v):
        return None
    # 固定阈值与 backtest_tool_router 的模板出场校验一致，不读取入场阈值参数。
    if position_direction == "long":
        satisfied = cci_v > 0 or rsi_v > 50
    elif position_direction == "short":
        satisfied = cci_v < 0 or rsi_v < 50
    else:
        return None
    if not satisfied:
        return None
    return TriggerResult(position_direction, bars[-1].close_time, {"cci": cci_v, "rsi": rsi_v}, action="exit")


def breakout_windows(params: dict[str, Any]) -> Optional[tuple[int, int]]:
    """Donchian 两个窗口的**唯一**解析与校验点。

    准入闸门与判定器必须共用它：两边各写一份看起来一样的校验，改一处漏一处就会出现
    「闸门放行、判定器按另一套口径跑」的分叉（Cursor lane 0909 P3）。None = 参数不可用。
    """
    lookback = params.get("lookback", 20)
    exit_lookback = params.get("exit_lookback", 10)
    if type(lookback) is not int or type(exit_lookback) is not int or lookback < 2 or exit_lookback < 1:
        return None
    return lookback, exit_lookback


def _donchian_indicators(
    params: dict[str, Any], bars: list[Bar], direction: str = "long"
) -> Optional[dict[str, float]]:
    windows = breakout_windows(params)
    if windows is None or len(bars) < max(windows) + 1:
        return None
    lookback, exit_lookback = windows
    if direction == "short":
        lookback, exit_lookback = exit_lookback, lookback
    return {
        "donchian_high": max(bar.high for bar in bars[-1 - lookback : -1]),
        "donchian_low": min(bar.low for bar in bars[-1 - exit_lookback : -1]),
        "close": bars[-1].close,
    }


def evaluate_breakout(params: dict[str, Any], bars: list[Bar], direction: str) -> Optional[TriggerResult]:
    """Mirror the prior-N entry channel for the explicitly backtested side."""
    if direction not in ("long", "short") or params.get("direction", "long") != direction:
        return None
    indicators = _donchian_indicators(params, bars, direction)
    if indicators is None:
        return None
    hit = (
        indicators["close"] > indicators["donchian_high"]
        if direction == "long"
        else indicators["close"] < indicators["donchian_low"]
    )
    return TriggerResult(direction, bars[-1].close_time, indicators) if hit else None


def evaluate_breakout_exit(params: dict[str, Any], bars: list[Bar], position_direction: str) -> Optional[TriggerResult]:
    """The prior-M opposite channel closes the current single-side position."""
    if position_direction not in ("long", "short") or params.get("direction", "long") != position_direction:
        return None
    indicators = _donchian_indicators(params, bars, position_direction)
    if indicators is None:
        return None
    hit = (
        indicators["close"] < indicators["donchian_low"]
        if position_direction == "long"
        else indicators["close"] > indicators["donchian_high"]
    )
    return TriggerResult(position_direction, bars[-1].close_time, indicators, action="exit") if hit else None


def rsi_reversal_settings(params: dict[str, Any]) -> Optional[tuple[int, float, float]]:
    """RSI 反转参数的**唯一**解析与校验点（周期、超卖线、超买线），准入与判定器共用。

    None = 参数不可用（非整数周期 / 周期 < 2 / 阈值不是数字 / 不满足 0 < 超卖 < 超买 < 100）。
    """
    period = params.get("rsi_period", 14)
    oversold = params.get("oversold", 30)
    overbought = params.get("overbought", 70)
    if type(period) is not int or period < 2:
        return None
    if type(oversold) not in (int, float) or type(overbought) not in (int, float):
        return None
    if not 0 < oversold < overbought < 100:
        return None
    return period, oversold, overbought


def _rsi_reversal_indicators(params: dict[str, Any], bars: list[Bar]) -> Optional[dict[str, float]]:
    settings = rsi_reversal_settings(params)
    if settings is None or len(bars) < 3 * settings[0] + 1:
        return None
    return {"rsi": rsi_series([bar.close for bar in bars], settings[0])[-1], "close": bars[-1].close}


def evaluate_rsi_reversal(params: dict[str, Any], bars: list[Bar], direction: str) -> Optional[TriggerResult]:
    """provider _build_rsi_reversal：无仓 RSI < oversold 开多；做空留待四期。"""
    if direction != "long":
        return None
    settings = rsi_reversal_settings(params)
    indicators = _rsi_reversal_indicators(params, bars)
    if settings is None or indicators is None or not indicators["rsi"] < settings[1]:
        return None
    return TriggerResult("long", bars[-1].close_time, indicators)


def evaluate_rsi_reversal_exit(
    params: dict[str, Any], bars: list[Bar], position_direction: str
) -> Optional[TriggerResult]:
    """provider _build_rsi_reversal：持多仓 RSI > overbought 平仓。"""
    if position_direction != "long":
        return None
    settings = rsi_reversal_settings(params)
    indicators = _rsi_reversal_indicators(params, bars)
    if settings is None or indicators is None or not indicators["rsi"] > settings[2]:
        return None
    return TriggerResult(position_direction, bars[-1].close_time, indicators, action="exit")


# watcher 每 tick 为一组布防拉取的补扫窗口（根）。放在这里而不是 watcher 里，是因为
# 「一条布防要多少根 K 线」是判定器的属性：准入侧要用 warmup + 这个补扫量去核对
# 「这条布防的取数需求装得下吗」，而准入层不该反向依赖 tasks 层。
MAX_CATCHUP_BARS = 200


def macd_settings(params: dict[str, Any]) -> Optional[tuple[int, int, int]]:
    fast, slow, signal = (params.get(key, default) for key, default in (("fast", 12), ("slow", 26), ("signal", 9)))
    if any(type(value) is not int for value in (fast, slow, signal)) or not (2 <= fast < slow and signal >= 1):
        return None
    return fast, slow, signal


def bollinger_settings(params: dict[str, Any]) -> Optional[tuple[int, float]]:
    period, multiplier = params.get("bb_period", 20), params.get("bb_std", 2.0)
    if type(period) is not int or period < 2 or type(multiplier) not in (int, float):
        return None
    if not math.isfinite(multiplier) or multiplier <= 0:
        return None
    return period, multiplier


def _macd_trigger(params, bars, direction, *, exiting=False):
    settings = macd_settings(params)
    if direction != "long" or settings is None:
        return None
    fast, slow, signal_period = settings
    if len(bars) < slow * 3 + signal_period + 1:
        return None
    closes = [bar.close for bar in bars]
    macd = [a - b for a, b in zip(ema_series(closes, fast), ema_series(closes, slow))]
    signal = ema_series(macd, signal_period)
    if not all(math.isfinite(v) for v in macd[-2:] + signal[-2:]):
        return None
    # backtesting.lib.crossover uses strict comparison on both samples.
    hit = (
        (macd[-2] > signal[-2] and macd[-1] < signal[-1])
        if exiting
        else (macd[-2] < signal[-2] and macd[-1] > signal[-1])
    )
    indicators = {"macd": macd[-1], "signal": signal[-1], "close": closes[-1]}
    return TriggerResult(direction, bars[-1].close_time, indicators, "exit" if exiting else "entry") if hit else None


def evaluate_macd(params, bars, direction):
    return _macd_trigger(params, bars, direction)


def evaluate_macd_exit(params, bars, position_direction):
    return _macd_trigger(params, bars, position_direction, exiting=True)


def _bollinger_trigger(params, bars, direction, *, breakout=False, exiting=False):
    settings = bollinger_settings(params)
    if direction != "long" or settings is None or len(bars) < settings[0] + 1:
        return None
    period, multiplier = settings
    closes = [bar.close for bar in bars[-period:]]
    if not all(math.isfinite(value) for value in closes):
        return None
    mid = sum(closes) / period
    sd = math.sqrt(sum((value - mid) ** 2 for value in closes) / period)
    lower, upper = mid - multiplier * sd, mid + multiplier * sd
    price = closes[-1]
    if exiting:
        hit = price < mid if breakout else price >= mid
    else:
        hit = price > upper if breakout else price < lower
    indicators = {"close": price, "bb_mid": mid, "bb_lower": lower, "bb_upper": upper}
    return TriggerResult(direction, bars[-1].close_time, indicators, "exit" if exiting else "entry") if hit else None


def evaluate_bollinger_reversal(params, bars, direction):
    return _bollinger_trigger(params, bars, direction)


def evaluate_bollinger_reversal_exit(params, bars, position_direction):
    return _bollinger_trigger(params, bars, position_direction, exiting=True)


def evaluate_bollinger_breakout(params, bars, direction):
    return _bollinger_trigger(params, bars, direction, breakout=True)


def evaluate_bollinger_breakout_exit(params, bars, position_direction):
    return _bollinger_trigger(params, bars, position_direction, breakout=True, exiting=True)


def roc_settings(params: dict[str, Any]) -> Optional[tuple[int, float, float]]:
    """动量 ROC 阈值参数的**唯一**解析与校验点（周期、入场阈值、出场阈值），准入与判定器共用。

    None = 参数不可用（非整数周期 / 周期 < 2 / 阈值不是数字 / exit_threshold > entry_threshold）。
    对齐 provider `_build_roc`（0919 ROC-契约.md §约束）。
    """
    period = params.get("roc_period", 12)
    entry_threshold = params.get("entry_threshold", 5)
    exit_threshold = params.get("exit_threshold", 0)
    if type(period) is not int or period < 2:
        return None
    if type(entry_threshold) not in (int, float) or type(exit_threshold) not in (int, float):
        return None
    if exit_threshold > entry_threshold:
        return None
    return period, entry_threshold, exit_threshold


def _roc_trigger(params, bars, direction, *, exiting=False):
    settings = roc_settings(params)
    if direction != "long" or settings is None:
        return None
    period, entry_threshold, exit_threshold = settings
    if len(bars) < period + 1:
        return None
    closes = [bar.close for bar in bars]
    # provider `_build_roc`: (close / close.shift(n) - 1) * 100, only closed bars.
    roc = (closes[-1] / closes[-1 - period] - 1) * 100
    if not math.isfinite(roc):
        return None
    # Strict comparisons -- threshold state, not a crossover event.
    hit = roc < exit_threshold if exiting else roc > entry_threshold
    indicators = {"roc": roc, "close": closes[-1]}
    return TriggerResult(direction, bars[-1].close_time, indicators, "exit" if exiting else "entry") if hit else None


def evaluate_roc(params, bars, direction):
    return _roc_trigger(params, bars, direction)


def evaluate_roc_exit(params, bars, position_direction):
    return _roc_trigger(params, bars, position_direction, exiting=True)


Evaluator = Callable[[dict[str, Any], list[Bar], str], Optional[TriggerResult]]


@dataclass(frozen=True)
class Evaluators:
    entry: Evaluator
    exit: Optional[Evaluator] = None


# 新增策略同时维护 required_warmup_bars；EMA 不登记 exit，沿用反向入场判定。
EVALUATOR_REGISTRY: dict[str, Evaluators] = {
    "macd": Evaluators(evaluate_macd, evaluate_macd_exit),
    "bollinger_reversal": Evaluators(evaluate_bollinger_reversal, evaluate_bollinger_reversal_exit),
    "bollinger_breakout": Evaluators(evaluate_bollinger_breakout, evaluate_bollinger_breakout_exit),
    "ma_cross": Evaluators(evaluate_ma_cross),
    "ema_cross": Evaluators(evaluate_ma_cross),
    "cci_rsi": Evaluators(evaluate_cci_rsi, evaluate_cci_rsi_exit),
    "breakout": Evaluators(evaluate_breakout, evaluate_breakout_exit),
    "rsi_reversal": Evaluators(evaluate_rsi_reversal, evaluate_rsi_reversal_exit),
    "roc": Evaluators(evaluate_roc, evaluate_roc_exit),
}


def evaluate_exit(
    strategy_type: str, params: dict[str, Any], bars: list[Bar], position_direction: str
) -> Optional[TriggerResult]:
    evaluators = EVALUATOR_REGISTRY.get(strategy_type)
    if evaluators is None:
        return None
    if position_direction not in ("long", "short"):
        return None
    if evaluators.exit is not None:
        result = evaluators.exit(params, bars, position_direction=position_direction)
    else:
        result = evaluators.entry(params, bars, "short" if position_direction == "long" else "long")
    return replace(result, action="exit", direction=position_direction) if result is not None else None


def required_warmup_bars(strategy_type: str, params: dict[str, Any]) -> Optional[int]:
    """判定器需要的最小 bar 数（对齐 provider 各工具的 `min_bars`）；strategy_type 未注册返回 None。"""
    if strategy_type == "macd":
        settings = macd_settings(params)
        return settings[1] * 3 + settings[2] + 1 if settings else None
    if strategy_type in {"bollinger_reversal", "bollinger_breakout"}:
        settings = bollinger_settings(params)
        return settings[0] + 1 if settings else None
    if strategy_type in ("ma_cross", "ema_cross"):
        ema_fast_n = int(params.get("ema_fast", 20) or 20)
        ema_slow_n = int(params.get("ema_slow", 60) or 60)
        return max(ema_fast_n, ema_slow_n) + 1
    if strategy_type == "rsi_reversal":
        settings = rsi_reversal_settings(params)
        # Wilder EWM 收敛 buffer，与 cci_rsi / provider min_bars 一致。
        return 3 * settings[0] + 1 if settings is not None else None
    if strategy_type == "breakout":
        windows = breakout_windows(params)
        return max(windows) + 1 if windows is not None else None
    if strategy_type == "cci_rsi":
        cci_period = int(params.get("cci_period", 20) or 20)
        rsi_period = int(params.get("rsi_period", 14) or 14)
        # provider min_bars = max(cci_period, rsi_period) * 3 + 1（Wilder EWM 收敛所需 buffer）。
        return max(cci_period, rsi_period) * 3 + 1
    if strategy_type == "roc":
        settings = roc_settings(params)
        # provider min_bars = roc_period + 1。
        return settings[0] + 1 if settings is not None else None
    return None


def is_supported_strategy_type(strategy_type: str) -> bool:
    return strategy_type in EVALUATOR_REGISTRY


def evaluate_entry(
    strategy_type: str,
    params: dict[str, Any],
    bars: list[Bar],
    direction: str,
) -> Optional[TriggerResult]:
    """按 strategy_type 分派到对应判定器；未注册类型返回 None（调用方应已在准入校验时拦截）。"""
    evaluator = EVALUATOR_REGISTRY.get(strategy_type)
    if evaluator is None:
        return None
    return evaluator.entry(params, bars, direction)
