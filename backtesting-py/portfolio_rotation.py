"""47 固定池现货轮动；独立取数预算、周一决策和 BTC 日收盘风险覆盖。"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, localcontext
from fractions import Fraction

from canonical_json import canonical_json_sha256
from portfolio_ledger import PortfolioLedger, SpotSpec, decimal_value, timestamp
from portfolio_runner import DailyBar, run_portfolio

DAY = 86400
BTC = 'BTCUSDT'
EMA_PERIOD = 200
RISK_WARMUP = 10 * EMA_PERIOD
MAX_BARS = 20000
FETCH_SECONDS = 60
EXECUTION_SECONDS = 120
# 所有组合 run 共用并发与请求起步限频；不触碰旧单币取数。
_FETCH_SLOTS = threading.BoundedSemaphore(4)
_RATE_LOCK = threading.Lock()
_NEXT_FETCH = 0.0


class RotationError(ValueError):
    def __init__(self, code, reason, message):
        super().__init__(message)
        self.code, self.reason = code, reason


@dataclass(frozen=True)
class RotationConfig:
    symbols: tuple[str, ...]
    specs: dict[str, SpotSpec]
    selected_at: int
    top_k: int
    risk_enabled: bool
    start: int
    end: int  # exclusive
    fetch_start: int


def parse_rotation(params, request):
    if request.get('market', 'spot') != 'spot' or request.get('timeframe') != '1d':
        raise RotationError('INVALID_PARAMS', 'spot_daily_only', 'rotation requires spot / 1d')
    if params.get('direction', 'long') != 'long' or request.get('direction', 'long') != 'long' or request.get('leverage', 1) != 1:
        raise RotationError('INVALID_PARAMS', 'spot_long_only', 'rotation requires long without leverage')
    pool = params.get('coin_pool')
    keys = {'selected_at', 'source', 'size', 'symbols', 'excluded', 'version', 'hash'}
    if not isinstance(pool, dict) or set(pool) != keys:
        raise RotationError('INVALID_PARAMS', 'invalid_pool', 'frozen C2 coin_pool required')
    if pool['version'] != 'coin_pool.v1' or pool['source'] != 'coins.coingecko.market_cap_rank':
        raise RotationError('INVALID_PARAMS', 'invalid_pool', 'coin_pool identity invalid')
    entries = pool['symbols']
    if type(pool['size']) is not int or not 1 <= pool['size'] <= 30 or not isinstance(entries, list) or len(entries) != pool['size']:
        raise RotationError('INVALID_PARAMS', 'invalid_pool', 'coin_pool size invalid')
    if any(not isinstance(e, dict) or set(e) != {'symbol', 'rank'} or not isinstance(e['symbol'], str)
           or not e['symbol'].endswith('USDT') or type(e['rank']) is not int or e['rank'] <= 0 for e in entries):
        raise RotationError('INVALID_PARAMS', 'invalid_pool', 'coin_pool symbol/rank invalid')
    symbols = tuple(e['symbol'] for e in entries)
    if len(set(symbols)) != len(symbols) or type(pool['selected_at']) is not int or pool['selected_at'] <= 0:
        raise RotationError('INVALID_PARAMS', 'invalid_pool', 'coin_pool duplicate symbol/selection date invalid')
    try:
        timestamp(pool['selected_at'])
        datetime.fromtimestamp(pool['selected_at'], timezone.utc)
    except (ValueError, OverflowError, OSError) as e:
        raise RotationError('INVALID_PARAMS', 'invalid_pool', 'coin_pool selection date out of range') from e
    excluded = pool['excluded']
    if not isinstance(excluded, list) or len(excluded) > pool['size'] or any(
        not isinstance(e, dict) or set(e) != {'symbol', 'rank', 'reason'} or not isinstance(e['symbol'], str)
        or type(e['rank']) is not int or e['rank'] <= 0 or e['reason'] not in {'stable_coin', 'not_tradeable', 'no_spot_rules', 'btc_benchmark'} for e in excluded
    ) or len({e['symbol'] for e in excluded}) != len(excluded) or set(symbols) & {e['symbol'] for e in excluded}:
        raise RotationError('INVALID_PARAMS', 'invalid_pool', 'coin_pool exclusions invalid')
    if pool['hash'] != canonical_json_sha256({k: v for k, v in pool.items() if k != 'hash'}):
        raise RotationError('INVALID_PARAMS', 'invalid_pool_hash', 'coin_pool canonical hash mismatch')
    rules = request.get('instrument_rules')
    if not isinstance(rules, dict) or set(rules) != set(symbols):
        raise RotationError('INVALID_PARAMS', 'invalid_specs', 'instrument_rules must cover frozen pool exactly')
    try:
        specs = {}
        for s in symbols:
            r = rules[s]
            if not isinstance(r, dict) or set(r) != {'symbol', 'price_tick', 'qty_step', 'min_qty', 'min_notional'} or r['symbol'] != s:
                raise ValueError('invalid rule identity')
            if any(not isinstance(r[k], str) for k in ('price_tick', 'qty_step', 'min_qty', 'min_notional')):
                raise ValueError('rules require Decimal strings')
            decimal_value(Decimal(r['price_tick']), 'price_tick', positive=True)
            specs[s] = SpotSpec(*(Decimal(r[k]) for k in ('qty_step', 'min_qty', 'min_notional')))
        PortfolioLedger(Decimal(str(request.get('initial_capital', '10000'))), specs,
                        Decimal(str(request.get('fee_bps', '10'))), Decimal(str(request.get('slippage_bps', '5'))))
    except (ValueError, ArithmeticError, TypeError) as e:
        raise RotationError('INVALID_PARAMS', 'invalid_specs_or_cost', str(e)) from e
    k, risk = params.get('top_k', 2), params.get('btc_risk_enabled', True)
    if type(k) is not int or not 1 <= k <= min(30, len(symbols)) or type(risk) is not bool:
        raise RotationError('INVALID_PARAMS', 'invalid_rotation_params', 'top_k must be 1..pool size (max 30); btc_risk_enabled boolean')
    start, end = request.get('start_at'), request.get('end_at')
    if type(start) is not int or type(end) is not int or not 0 < start < end <= 253402214400 or start % DAY or end % DAY:
        raise RotationError('INVALID_PARAMS', 'invalid_interval', 'interval must use consecutive UTC daily boundaries, end exclusive')
    warmup = RISK_WARMUP if risk else 8
    fetch_start = start - warmup * DAY
    count = (end - fetch_start) // DAY
    if fetch_start <= 0 or (end-start)//DAY > 5000 or count * len(set(symbols) | {BTC}) > MAX_BARS:
        raise RotationError('DATA_BUDGET_EXCEEDED', 'price_bar_budget', 'total price bars exceed 20000 including shared warmup and BTC')
    return RotationConfig(symbols, specs, pool['selected_at'], k, risk, start, end, fetch_start)


def fetch_rotation(config, fetcher, *, deadline=None):
    """必要流全取齐；截止时间包含等待共享槽位，失败不缩池。"""
    deadline = min(deadline if deadline is not None else float('inf'), time.monotonic() + FETCH_SECONDS)
    symbols = tuple(dict.fromkeys((*config.symbols, BTC)))

    def one(symbol):
        global _NEXT_FETCH
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not _FETCH_SLOTS.acquire(timeout=max(0, remaining)):
            raise RotationError('DATA_FETCH_TIMEOUT', 'fetch_deadline', '60 second fetch deadline exceeded')
        try:
            with _RATE_LOCK:
                delay = max(0, _NEXT_FETCH - time.monotonic())
                if delay >= deadline - time.monotonic():
                    raise RotationError('DATA_FETCH_TIMEOUT', 'fetch_deadline', 'shared rate-limit wait exceeded deadline')
                if delay:
                    threading.Event().wait(delay)
                _NEXT_FETCH = time.monotonic() + 0.05
            if time.monotonic() >= deadline:
                raise RotationError('DATA_FETCH_TIMEOUT', 'fetch_deadline', 'fetch deadline exceeded')
            return fetcher(symbol, config.fetch_start, config.end)
        finally:
            _FETCH_SLOTS.release()

    executor = ThreadPoolExecutor(max_workers=4)
    futures = {s: executor.submit(one, s) for s in symbols}
    try:
        _, pending = wait(futures.values(), timeout=max(0, deadline-time.monotonic()))
        if pending or time.monotonic() >= deadline:
            raise RotationError('DATA_FETCH_TIMEOUT', 'fetch_deadline', '60 second fetch deadline exceeded; pool unchanged')
        streams = {}
        total = 0
        for symbol, future in futures.items():
            try:
                rows, source, central, cached = future.result()
            except RotationError:
                raise
            except Exception as e:
                raise RotationError('INSUFFICIENT_DATA', 'required_stream_missing', f'required stream {symbol} failed: {e}') from e
            total += len(rows)
            if total > MAX_BARS:
                raise RotationError('DATA_BUDGET_EXCEEDED', 'price_bar_budget', 'actual price bars exceed 20000')
            expected = range(config.fetch_start, config.end, DAY)
            if len(rows) != len(expected) or any(r.get('ts') != ts for r, ts in zip(rows, expected)):
                raise RotationError('INSUFFICIENT_DATA', 'required_stream_missing', f'{symbol}: full interval and {RISK_WARMUP if config.risk_enabled else 8} warmup bars required')
            try:
                for row in rows:
                    for key in ('open', 'close'):
                        decimal_value(Decimal(row[key]), f'{symbol} {key}', positive=True)
            except (KeyError, ValueError, ArithmeticError, TypeError) as e:
                raise RotationError('INSUFFICIENT_DATA', 'invalid_stream_price', f'{symbol}: invalid price') from e
            streams[symbol] = (rows, source, central, cached)
        return streams
    finally:
        # 已运行的同步 fetch 不可强杀；返回失败后仍占共享槽位，直到结束，避免突破并发上限。
        executor.shutdown(wait=False, cancel_futures=True)


def ema200(closes):
    """adjust=False，首个有效值递推，满 200 根输出；128 位抑制递推舍入。"""
    result, seed = [], None
    with localcontext() as ctx:
        ctx.prec = 128
        alpha = Decimal(2) / Decimal(EMA_PERIOD + 1)
        for i, close in enumerate(closes):
            seed = close if seed is None else seed + alpha * (close-seed)
            result.append(seed if i+1 >= EMA_PERIOD else None)
    return result


def run_rotation(config, streams, request, *, metric_series=None, deadline=None):
    btc_rows = streams[BTC][0]
    offset = (config.start-config.fetch_start)//DAY
    if config.risk_enabled and offset < RISK_WARMUP:
        raise RotationError('INSUFFICIENT_DATA', 'btc_ema_warmup', 'BTC needs 2000 completed daily bars')
    ema = ema200([Decimal(r['close']) for r in btc_rows]) if config.risk_enabled else None
    targets, decisions, bars, blocked_times = {}, [], [], set()
    # Equal allocation rounded down only when 1/K repeats; dust stays cash.
    with localcontext() as ctx:
        ctx.prec = 34
        ctx.rounding = ROUND_DOWN
        weight = (Decimal(1)/Decimal(config.top_k)).quantize(Decimal('0.000000000001'))
    for j in range(offset, len(btc_rows)):
        if deadline is not None and time.monotonic() >= deadline:
            raise RotationError('EXECUTION_TIMEOUT', 'execution_deadline', '120 second execution deadline exceeded')
        ts = btc_rows[j]['ts']
        bars.append(DailyBar(ts, {s: Decimal(streams[s][0][j]['open']) for s in config.symbols},
                             {s: Decimal(streams[s][0][j]['close']) for s in config.symbols}))
        # 00:00 的决策只用前一日 close；提交 D 的前一日标签，成交于本日原始 open。
        if j == offset:
            continue  # v4 首点必须是交易前本金。
        decision_label = ts - DAY
        blocked = config.risk_enabled and Decimal(btc_rows[j-1]['close']) < ema[j-1]
        monday = datetime.fromtimestamp(ts, timezone.utc).weekday() == 0
        if blocked:
            targets[decision_label] = {}
            blocked_times.add(ts)
        elif monday:
            scores = {s: (Fraction(streams[s][0][j-1]['close']) / Fraction(btc_rows[j-1]['close'])) /
                      (Fraction(streams[s][0][j-8]['close']) / Fraction(btc_rows[j-8]['close'])) - 1 for s in config.symbols}
            ranked = sorted(config.symbols, key=lambda s: -scores[s])
            chosen = ranked[:config.top_k]
            targets[decision_label] = dict.fromkeys(chosen, weight)
            decisions.append({'ts': ts, 'ranking': ranked, 'selected': chosen})
    manifests = [{'source': source, 'symbol': s, 'market': 'spot', 'timeframe': '1d',
                  'start_at': bars[0].ts, 'end_at': bars[-1].ts, 'kline_count': len(rows),
                  'checksum_algo': 'sha256', 'checksum': canonical_json_sha256(rows)}
                 for s, (rows, source, _, _) in streams.items()]
    output = run_portfolio(initial_cash=Decimal(str(request.get('initial_capital', '10000'))), specs=config.specs,
                           bars=bars, target_weights=targets, fee_bps=Decimal(str(request.get('fee_bps', '10'))),
                           slippage_bps=Decimal(str(request.get('slippage_bps', '5'))),
                           btc_benchmark=[{'ts': r['ts'], 'close_price': r['close']} for r in btc_rows[offset:]],
                           price_manifests=manifests, metric_series=metric_series)
    for snapshot in output.result['snapshots']:
        if snapshot['ts'] in blocked_times and any(p['qty'] != '0' for p in snapshot['positions']):
            raise RotationError('ENGINE_ERROR', 'liquidation_below_minimum', 'spot minimum order rules prevent complete BTC risk liquidation')
    if deadline is not None and time.monotonic() >= deadline:
        raise RotationError('EXECUTION_TIMEOUT', 'execution_deadline', '120 second execution deadline exceeded')
    date = datetime.fromtimestamp(config.selected_at, timezone.utc).date().isoformat()
    assumptions = {'coin_pool': f'币池按 {date} 选定，存在幸存者偏差',
                   'execution': '决策用周一 UTC 00:00 前已收盘数据，下一根开盘成交',
                   'btc_risk_enabled': config.risk_enabled,
                   'initial_point': '首日为交易前本金；首日决策不执行',
                   'equal_weight_precision': '1/K 向下保留12位小数，余数留现金'}
    if config.risk_enabled:
        assumptions['btc_risk'] = 'BTC 收盘低于 EMA200 时次日开盘清仓并暂停买入；恢复后等下一个周一'
    return output, decisions, assumptions
