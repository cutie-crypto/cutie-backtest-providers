"""Versioned seed budgets; minimum calculable windows remain builder-owned."""
import math

WARMUP_RULE_VERSION = 'template-warmup.v1'
SEED_RULE_VERSION = 'adjacent-first-observation.v1'
MAX_WARMUP_BARS = 20000
SEED_RESIDUAL = 1e-9


def wilder_bars(period, *, stages=1):
    # One first-observation seed, then m updates per stage. Cascades split the
    # residual budget and sum stage depths; this is a seed budget, not a proof
    # that threshold decisions or path-dependent Supertrend bands are invariant.
    if period <= 1:
        return stages
    updates = math.ceil(math.log(SEED_RESIDUAL / stages) / math.log1p(-1 / period))
    return stages * (updates + 1)


def template_required_warmup(name, params, built, schema):
    def value(key, default):
        return params.get(key, schema.get(key, {}).get('default', default))

    if any(key in built for key in ('range_config', 'calendar_config', 'event_window_config',
                                  'token_unlock_config', 'macro_config', 'top_lsr_config',
                                  'etf_flow_config', 'exchange_netflow_config')):
        return 0  # Dedicated time/series history gates own these prefixes.
    budget = max(int(built['min_bars']), int(built.get('warmup_bars', 0)),
                 int(built.get('ema_warmup_requested_bars', 0)))
    if name == 'keltner_breakout':
        budget = max(budget, 10 * value('ema_period', 20), wilder_bars(value('atr_period', 14)))
    if name == 'volume_breakout':
        budget = max(budget, 10 * value('exit_ema', 20))
    if name.startswith('macd'):
        # DIF is parallel fast/slow EMA; DEA is a second recursive stage.
        depth = 10 * max(value('fast', 12), value('slow', 26)) + 10 * value('signal', 9)
        budget = max(budget, depth + (2 * value('swing_n', 5) + value('max_gap_bars', 60)
                                    if 'divergence' in name else 0))
    if name in ('rsi_reversal', 'rsi_scale_in_out', 'cci_rsi', 'ema_rsi_pullback',
                'ema_trend_rsi', 'red_streak_rsi') or name.startswith('rsi_'):
        period = value('period', 14) if name in ('rsi_reversal', 'rsi_scale_in_out') else value('rsi_period', 14)
        depth = wilder_bars(period) + 1  # RSI starts with a close delta.
        budget = max(budget, depth + (2 * value('swing_n', 5) + value('max_gap_bars', 60)
                                    if 'divergence' in name else 0))
    if name == 'adx_di_cross':
        budget = max(budget, wilder_bars(value('adx_period', 14), stages=2))
    if name == 'supertrend':
        budget = max(budget, wilder_bars(value('atr_period', 10)))
    if name in ('bullish_doji_reversal', 'bearish_doji_reversal'):
        budget = max(budget, wilder_bars(14) + 1)
    if name in ('hammer_pin_bar', 'shooting_star') and value('position_filter', True):
        budget = max(budget, 600)
    if name == 'inside_bar_breakout' and value('trend_filter', False):
        budget = max(budget, 200)
    return budget
