"""Explicit signal replay evidence; never relabel the legacy forced-close result."""

from dataclasses import asdict
from decimal import Decimal

from canonical_json import canonical_json_sha256, normalize_numbers_for_hash
from funding_history import fetch_history
from signal_lifecycle_kernel import Candle
from strategy_entry_evaluators import Bar
from strategy_execution_policy import requested_signal_execution
from strategy_intent_replay import generate_intents
from strategy_cycle_replay import replay_cycle
from strategy_signal_replay import managed_cycle_options


def build_signal_report(
    *,
    request,
    risk_policy,
    tool_id,
    params,
    market,
    symbol,
    exchange,
    timeframe,
    step,
    start_at,
    end_at,
    fee_bps,
    slippage_bps,
    fetch_ohlcv,
    history_loader=fetch_history
):
    normalized = requested_signal_execution(request, risk_policy)
    if normalized is None:
        raise ValueError("signal execution request is required")
    policy, limits = normalized["execution_policy"], normalized["cycle_limits"]
    if (
        not isinstance(risk_policy, dict)
        or set(risk_policy) != {"schema", "direction", "leverage"}
        or risk_policy["schema"] != "cutie.strategy_risk_policy.v1"
    ):
        raise ValueError("signal execution requires explicit risk policy")
    evaluator = tool_id.removeprefix("local.backtesting_py.")
    if tool_id != "local.backtesting_py." + evaluator or evaluator not in {
        "ema_cross",
        "cci_rsi",
        "breakout",
        "rsi_reversal",
        "macd",
        "bollinger_reversal",
        "bollinger_breakout",
    }:
        raise ValueError("signal evaluator is not supported")
    if market == "futures" and exchange != "binance":
        raise ValueError("signal replay funding venue unsupported")
    if type(step) is not int or step < 300 or step % 300:
        raise ValueError("signal interval must align to 5m observations")
    if any(not Decimal(str(v)).is_finite() or Decimal(str(v)) < 0 for v in (fee_bps, slippage_bps)):
        raise ValueError("signal costs must be finite and nonnegative")
    # Fetch prehistory as part of the evidence. An incomplete final indicator bar
    # cannot emit an intent before the requested replay end.
    first_open = (start_at // step) * step - policy["indicator_history_bars"] * step
    indicator_df = fetch_ohlcv(exchange, market, symbol, timeframe, first_open, end_at)
    bars = [
        Bar(
            int(t.timestamp()),
            int(t.timestamp()) + step,
            float(r.Open),
            float(r.High),
            float(r.Low),
            float(r.Close),
        )
        for t, r in indicator_df.iterrows()
        if int(t.timestamp()) + step + 30 <= end_at
    ]
    observation_start = (start_at // 300) * 300
    observation_df = fetch_ohlcv(exchange, market, symbol, "5m", observation_start, end_at)
    candles = [
        Candle(
            int(t.timestamp()),
            int(t.timestamp()) + 299,
            *(Decimal(str(r[k])) for k in ("Open", "High", "Low", "Close"))
        )
        for t, r in observation_df.iterrows()
        if observation_start <= int(t.timestamp()) and int(t.timestamp()) + 300 <= end_at
    ]
    if not bars or bars[-1].close_time != ((end_at - 30) // step) * step:
        raise ValueError("indicator evidence does not cover replay end")
    if (
        not candles
        or candles[0].open_time != observation_start
        or candles[-1].close_time + 1 != (end_at // 300) * 300
    ):
        raise ValueError("observation evidence does not cover replay range")
    history = None
    if market == "futures":
        history = history_loader(
            symbol.replace("/", "").split(":")[0], observation_start * 1000, end_at * 1000
        )
    intents = generate_intents(
        bars=bars,
        evaluator=evaluator,
        params=params,
        direction=risk_policy["direction"],
        leverage=risk_policy["leverage"],
        symbol=symbol,
        execution_policy=policy,
        start_at=start_at,
    )
    # Ignore no portion of the requested intent sequence: coverage validation in
    # replay_cycle rejects a missing final observation instead of truncating it.
    replay = replay_cycle(
        intents=intents,
        candles=candles,
        market=market,
        symbol=symbol,
        direction=risk_policy["direction"],
        leverage=risk_policy["leverage"],
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
        funding_history=history,
        **limits,
        **managed_cycle_options(policy, candles, step)
    )
    evidence = {
        "schema": "cutie.strategy_signal_result.v1",
        "execution_policy": policy,
        "risk_policy": risk_policy,
        "cycle_limits": limits,
        "tool_id": tool_id,
        "params": params,
        "market": market,
        "symbol": symbol,
        "exchange": exchange,
        "timeframe": timeframe,
        "start_at": start_at,
        "end_at": end_at,
        "fee_bps": str(fee_bps),
        "slippage_bps": str(slippage_bps),
        "indicator_bars": [asdict(b) for b in bars],
        "observation_candles": [asdict(c) for c in candles],
        "funding_evidence": history,
        "intents": intents,
        "replay": replay,
        "author_quota_scope": "isolated_strategy_no_external_entries",
        "verification": "provider_reported",
    }
    evidence = normalize_numbers_for_hash(evidence)
    return {**evidence, "sha256": canonical_json_sha256(evidence)}
