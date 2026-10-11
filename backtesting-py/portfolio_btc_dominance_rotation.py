"""48 BTC.D 切换：冻结的 CoinGlass 稀疏点位驱动现货山寨篮子。"""
from __future__ import annotations

import time
from bisect import insort
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, localcontext
from fractions import Fraction

from canonical_json import canonical_json_sha256
from portfolio_ledger import decimal_text
from portfolio_result_v4 import _coinglass_reference
from portfolio_rotation import BTC, DAY, MAX_BARS, RotationConfig, RotationError, parse_rotation
from portfolio_rotation_http import RUNNER, TOOL_SPEC as ROTATION_SPEC, rotation_response
from portfolio_runner import DailyBar, run_portfolio

TOOL_ID = 'local.backtesting_py.btc_dominance_rotation'
TOOL_SPEC = {
    'name': 'Local Spot BTC Dominance Rotation',
    'description': 'CoinGlass BTC.D 3-day points (not interpolated), point EMA and daily BTC range gate; '
                   'equal-weight frozen altcoin pool, next-open fills, all cash when BTC.D rises above EMA.',
    'strategy_family': 'portfolio_rotation', 'runner': RUNNER, 'is_default': False,
    'markets': ['spot'], 'timeframes': ['1d'],
    'param_schema_properties': {
        'coin_pool': deepcopy(ROTATION_SPEC['param_schema_properties']['coin_pool']),
        'direction': deepcopy(ROTATION_SPEC['param_schema_properties']['direction']),
        'btcd_ema_period': {'type': 'integer', 'default': 20, 'minimum': 5, 'maximum': 100,
                            'description': 'Measured in BTC.D points; one point every ~3 days, 20 points ~60 days.'},
        'btc_range_days': {'type': 'integer', 'default': 7, 'minimum': 1},
        'btc_range_pct': {'type': 'number', 'default': 3, 'minimum': 0},
    },
}


@dataclass(frozen=True)
class DominanceConfig(RotationConfig):
    btcd_ema_period: int
    btc_range_days: int
    btc_range_pct: Decimal


def parse_dominance(params, request):
    values = {}
    for key, schema in TOOL_SPEC['param_schema_properties'].items():
        if key in ('coin_pool', 'direction'):
            continue
        raw = params.get(key, schema['default'])
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise RotationError('INVALID_PARAMS', 'invalid_rotation_params', f'{key} must be a number')
        value = Decimal(str(raw))
        if not value.is_finite() or value < schema['minimum'] or (
            'maximum' in schema and value > schema['maximum']
        ) or (schema['type'] == 'integer' and value != value.to_integral_value()):
            raise RotationError('INVALID_PARAMS', 'invalid_rotation_params', f'{key} out of range')
        values[key] = int(value) if schema['type'] == 'integer' else value
    # C2 parsing/costs/budgets stay shared; this template buys the entire frozen pool.
    base = parse_rotation({**params, 'btc_risk_enabled': False, 'top_k': 1}, request)
    if BTC in base.symbols:
        raise RotationError('INVALID_PARAMS', 'invalid_pool', 'BTC benchmark cannot be in the altcoin basket')
    fetch_start = base.start - max(8, values['btc_range_days']) * DAY
    if fetch_start <= 0 or (base.end-fetch_start)//DAY * (len(base.symbols)+1) > MAX_BARS:
        raise RotationError('DATA_BUDGET_EXCEEDED', 'price_bar_budget', 'total price bars exceed 20000 including BTC range warmup')
    base = replace(base, top_k=len(base.symbols), fetch_start=fetch_start)
    return DominanceConfig(**base.__dict__, **values)


def validate_dominance_series(series, start, end):
    # Same ValueError -> ENGINE_ERROR mapping as #47's frozen metric input validation.
    return _coinglass_reference(series, start, end)


def run_dominance(config, streams, request, *, metric_series=None, deadline=None):
    validate_dominance_series(metric_series, config.start, config.end-DAY)
    points = metric_series['points']
    # available_at need not be monotone in ts: a late archived older point cannot hide newer readable ones.
    arrivals = sorted(range(len(points)), key=lambda i: (points[i]['available_at'], points[i]['ts']))
    cursor, visible, seed, ema = 0, [], None, None
    btc_rows = streams[BTC][0]
    offset = (config.start-config.fetch_start)//DAY
    bars = [DailyBar(r['ts'], {s: Decimal(streams[s][0][j]['open']) for s in config.symbols},
                     {s: Decimal(streams[s][0][j]['close']) for s in config.symbols})
            for j, r in enumerate(btc_rows) if j >= offset]
    decisions, exit_times = [], set()
    with localcontext() as ctx:
        ctx.prec = 34
        ctx.rounding = ROUND_DOWN
        weight = (Decimal(1)/Decimal(len(config.symbols))).quantize(Decimal('0.000000000001'))

    def decide(i, quantities):
        nonlocal cursor, seed, ema
        if deadline is not None and time.monotonic() >= deadline:
            raise RotationError('EXECUTION_TIMEOUT', 'execution_deadline', '120 second execution deadline exceeded')
        j = offset+i
        decision_at = btc_rows[j]['ts'] + DAY
        changed = False
        while cursor < len(arrivals) and points[arrivals[cursor]]['available_at'] <= decision_at:
            insort(visible, arrivals[cursor])
            cursor += 1
            changed = True
        if changed:
            # Recompute in point-date order only when the readable set changes. No daily interpolation/EMA updates.
            with localcontext() as ctx:
                ctx.prec = 128
                alpha = Decimal(2)/Decimal(config.btcd_ema_period+1)
                seed = None
                for index in visible:
                    value = Decimal(points[index]['value'])
                    seed = value if seed is None else seed + alpha*(value-seed)
                ema = seed if len(visible) >= config.btcd_ema_period else None
        if ema is None:
            raise RotationError('INSUFFICIENT_DATA', 'btc_dominance_history_unavailable', 'insufficient readable BTC.D EMA warmup points')
        latest = points[visible[-1]]
        value = Decimal(latest['value'])
        held = any(q > 0 for q in quantities.values())
        change = abs(Fraction(btc_rows[j]['close']) / Fraction(btc_rows[j-config.btc_range_days]['close']) - 1)
        action = None
        if held and value > ema:
            action, weights = 'exit', {}
            exit_times.add(decision_at)
        elif not held and value < ema and change <= Fraction(config.btc_range_pct)/100:
            action, weights = 'entry', dict.fromkeys(config.symbols, weight)
        if action is None:
            return None
        decisions.append({'decision_ts': btc_rows[j]['ts'], 'decision_at': decision_at,
                          'value_ts': latest['ts'], 'available_at': latest['available_at'],
                          'value': latest['value'], 'ema': format(ema, 'f'), 'action': action,
                          'reason': 'btc_dominance_above_ema' if action == 'exit' else 'btc_dominance_below_ema_and_btc_in_range'})
        return weights

    manifests = [{'source': source, 'symbol': s, 'market': 'spot', 'timeframe': '1d',
                  'start_at': bars[0].ts, 'end_at': bars[-1].ts, 'kline_count': len(rows),
                  'checksum_algo': 'sha256', 'checksum': canonical_json_sha256(rows)}
                 for s, (rows, source, _, _) in streams.items()]
    output = run_portfolio(initial_cash=Decimal(str(request.get('initial_capital', '10000'))), specs=config.specs,
                           bars=bars, target_weights={}, decision_fn=decide,
                           fee_bps=Decimal(str(request.get('fee_bps', '10'))),
                           slippage_bps=Decimal(str(request.get('slippage_bps', '5'))),
                           btc_benchmark=[{'ts': r['ts'], 'close_price': r['close']} for r in btc_rows[offset:]],
                           price_manifests=manifests, metric_series=metric_series)
    for snapshot in output.result['snapshots']:
        if snapshot['ts'] in exit_times and any(p['qty'] != '0' for p in snapshot['positions']):
            raise RotationError('ENGINE_ERROR', 'liquidation_below_minimum', 'spot minimum order rules prevent complete BTC.D liquidation')
    if deadline is not None and time.monotonic() >= deadline:
        raise RotationError('EXECUTION_TIMEOUT', 'execution_deadline', '120 second execution deadline exceeded')
    date = datetime.fromtimestamp(config.selected_at, timezone.utc).date().isoformat()
    assumptions = {
        'coin_pool': f'币池按 {date} 选定，存在幸存者偏差',
        'execution': '每日收盘判定，available_at ≤ 决策时刻的点才可读，下一根开盘成交',
        'availability': '可用时间按点位日期 +1 天推定，非实际抓取时间',
        'history': '历史点以首次入库为准（上游修订不回写）',
        'granularity': 'BTC.D 是 3 天粒度，不插值、不前向填；EMA 只在新点可读时更新',
        'position_mode': '首版退出后空仓，不换回 BTC；做空镜像未开放',
        'initial_point': '首日为交易前本金；首日收盘信号最早在次日开盘成交',
        'equal_weight_precision': '1/K 向下保留12位小数，余数留现金',
        'btcd_ema_period': config.btcd_ema_period, 'btc_range_days': config.btc_range_days,
        'btc_range_pct': decimal_text(config.btc_range_pct),
    }
    return output, decisions, assumptions


def dominance_response(body, request, run_id, api):
    return rotation_response(body, request, run_id, api, tool_spec=TOOL_SPEC,
                             parser=parse_dominance, runner=run_dominance,
                             validate_series=validate_dominance_series)
