"""10E 手算表，初始1000、费用/滑点0、整数步长；三币 SOL/ETH/BNB 池序。
UTC 2025-01-06: 七日强弱 .2/.1/0，SOL>ETH>BNB，买 SOL50@10 / ETH25@20；现金0，NAV1150。
UTC 2025-01-13: 强弱 0/.5/1，BNB>ETH>SOL，开盘NAV1350；先卖SOL50@12、ETH2@30，
               买BNB8@80，现金20，ETH23/BNB8，收盘NAV=20+23*33+8*80=1419。
UTC 2025-01-20: 强弱 1/0/0，SOL>ETH>BNB（同分池序）；开盘NAV1419，
               卖ETH1@33、BNB8@80，现金693；SOL目标709.5，目标下取29但现金只够28，
               买SOL28@24，现金21，SOL28/ETH22，收盘NAV=21+28*25+22*34=1469。
首点1000；第二次决策前周日NAV=50*12+25*33=1425，次日1419，回撤6/1425=0.00421053。
BTC全期100，基准1000。全部期望值独立手写，非生产输出生成。
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
import types
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as api
import portfolio_rotation as rotation
from canonical_json import canonical_json_sha256
from strategy_entry_filters import FILTER_PARAM_SCHEMA_PROPERTIES
from portfolio_rotation import RotationError, parse_rotation, fetch_rotation, run_rotation, ema200
from portfolio_rotation_http import TOOL_ID
from strategy_entry_filters import PATTERN_CONFIRM_PARAM_SCHEMA_PROPERTIES
from strategy_entry_filters import FILTER_PARAM_SCHEMA_PROPERTIES

D = Decimal
DAY = 86400
T0 = 1736035200  # Sunday Jan 5
SYMBOLS = ('SOLUSDT', 'ETHUSDT', 'BNBUSDT')


def request(risk=False, symbols=SYMBOLS, days=22, k=2):
    pool = {'selected_at': T0, 'source': 'coins.coingecko.market_cap_rank', 'size': len(symbols),
            'symbols': [{'symbol': s, 'rank': i+2} for i, s in enumerate(symbols)],
            'excluded': [], 'version': 'coin_pool.v1'}
    pool['hash'] = canonical_json_sha256(pool)
    return {'run_id': 'rotation10e', 'provider_tool_id': TOOL_ID, 'market': 'spot', 'timeframe': '1d',
            'symbol': symbols[0], 'start_at': T0, 'end_at': T0+days*DAY,
            'initial_capital': '1000', 'fee_bps': '0', 'slippage_bps': '0',
            'provider_params': {'coin_pool': pool, 'top_k': k, 'btc_risk_enabled': risk},
            'instrument_rules': {s: {'symbol': s, 'price_tick': '1', 'qty_step': '1', 'min_qty': '0', 'min_notional': '0'} for s in symbols}}


def streams(config, *, golden=True):
    out = {}
    for s in dict.fromkeys((*config.symbols, 'BTCUSDT')):
        rows = []
        for ts in range(config.fetch_start, config.end, DAY):
            i = (ts-T0)//DAY
            base = {'SOLUSDT': '10', 'ETHUSDT': '20', 'BNBUSDT': '40', 'BTCUSDT': '100'}.get(s, '10')
            op = cl = base
            if golden and i >= 0 and s != 'BTCUSDT':
                epoch = 0 if i < 7 else 1 if i < 14 else 2 if i < 15 else 3
                cl = {'SOLUSDT': ('12', '12', '24', '25'), 'ETHUSDT': ('22', '33', '33', '34'),
                      'BNBUSDT': ('40', '80', '80', '80')}[s][epoch]
                op = cl
                if i == 1:
                    op = {'SOLUSDT': '10', 'ETHUSDT': '20', 'BNBUSDT': '40'}[s]
                elif i == 8:
                    op = {'SOLUSDT': '12', 'ETHUSDT': '30', 'BNBUSDT': '80'}[s]
                elif i == 15:
                    op = {'SOLUSDT': '24', 'ETHUSDT': '33', 'BNBUSDT': '80'}[s]
            rows.append({'ts': ts, 'open': op, 'close': cl})
        out[s] = (rows, 'handwritten.fixture', False, False)
    return out


def replay(req=None, data=None):
    req = request() if req is None else req
    cfg = parse_rotation(req['provider_params'], req)
    return run_rotation(cfg, streams(cfg) if data is None else data, req)


def test_three_week_handwritten_cash_nav_fills():
    output, decisions, assumptions = replay()
    assert [(d['ranking'], d['selected']) for d in decisions] == [
        (['SOLUSDT', 'ETHUSDT', 'BNBUSDT'], ['SOLUSDT', 'ETHUSDT']),
        (['BNBUSDT', 'ETHUSDT', 'SOLUSDT'], ['BNBUSDT', 'ETHUSDT']),
        (['SOLUSDT', 'ETHUSDT', 'BNBUSDT'], ['SOLUSDT', 'ETHUSDT'])]
    assert [(f['ts'], f['symbol'], f['side'], f['qty'], f['price'], f['fee'], f['slippage']) for f in output.result['fills']] == [
        (T0+DAY, 'SOLUSDT', 'buy', '50', '10', '0', '0'),
        (T0+DAY, 'ETHUSDT', 'buy', '25', '20', '0', '0'),
        (T0+8*DAY, 'SOLUSDT', 'sell', '50', '12', '0', '0'),
        (T0+8*DAY, 'ETHUSDT', 'sell', '2', '30', '0', '0'),
        (T0+8*DAY, 'BNBUSDT', 'buy', '8', '80', '0', '0'),
        (T0+15*DAY, 'ETHUSDT', 'sell', '1', '33', '0', '0'),
        (T0+15*DAY, 'BNBUSDT', 'sell', '8', '80', '0', '0'),
        (T0+15*DAY, 'SOLUSDT', 'buy', '28', '24', '0', '0')]
    for i, cash, nav in [(0, '1000', '1000'), (1, '0', '1150'), (8, '20', '1419'), (15, '21', '1469'), (21, '21', '1469')]:
        assert output.result['snapshots'][i]['cash'] == cash
        assert output.result['equity_curve'][i]['equity'] == nav
    assert output.result['metrics'] == {'total_return': '0.469', 'max_drawdown': '0.00421053', 'fill_count': '8'}
    assert all(p['equity'] == '1000' for p in output.result['btc_benchmark'])
    assert '存在幸存者偏差' in assumptions['coin_pool']
    assert output.result['input_manifests']['metric_series'] is None


def test_tie_uses_pool_order():
    req = request(symbols=('SOLUSDT', 'BNBUSDT', 'ETHUSDT'), k=1)
    cfg = parse_rotation(req['provider_params'], req)
    _, decisions, _ = replay(req, streams(cfg, golden=False))
    assert decisions[0]['ranking'] == ['SOLUSDT', 'BNBUSDT', 'ETHUSDT']
    assert decisions[0]['selected'] == ['SOLUSDT']


def test_monday_unclosed_close_never_changes_current_ranking():
    req = request()
    cfg = parse_rotation(req['provider_params'], req)
    data = streams(cfg)
    before = replay(req, data)[1][0]
    data['BNBUSDT'][0][(T0+DAY-cfg.fetch_start)//DAY]['close'] = '999999'
    assert replay(req, data)[1][0] == before


def test_risk_exit_pause_and_recovery_waits_for_monday():
    req = request(risk=True)
    cfg = parse_rotation(req['provider_params'], req)
    data = streams(cfg, golden=False)
    for row in data['BTCUSDT'][0]:
        i = (row['ts']-T0)//DAY
        if 2 <= i < 10:
            row['close'] = '50'
        elif i >= 10:
            row['close'] = '200'
    output, decisions, _ = replay(req, data)
    assert [(f['ts'], f['side'], f['qty']) for f in output.result['fills']] == [
        (T0+DAY, 'buy', '50'), (T0+DAY, 'buy', '25'),
        (T0+3*DAY, 'sell', '50'), (T0+3*DAY, 'sell', '25'),
        (T0+15*DAY, 'buy', '50'), (T0+15*DAY, 'buy', '25')]
    assert [d['ts'] for d in decisions] == [T0+DAY, T0+15*DAY]
    assert output.result['snapshots'][11]['cash'] == '1000'
    assert output.result['snapshots'][8]['cash'] == '1000'


def test_ema_seed_min_period_and_strict_boundary():
    assert ema200([D('100')]*199) == [None]*199
    assert ema200([D('100')]*200)[-1] == D('100')
    expected = D('100') + D(2)/D(201)*D('-50')
    assert abs(ema200([D('100')]*200+[D('50')])[-1]-expected) < D('1e-25')
    req = request(risk=True)
    cfg = parse_rotation(req['provider_params'], req)
    output, decisions, _ = replay(req, streams(cfg, golden=False))
    assert len(output.result['fills']) == 2  # BTC==EMA200 does not flatten
    assert len(decisions) == 3


def test_btc_less_than_2000_warmup_fails():
    req = request(risk=True)
    cfg = parse_rotation(req['provider_params'], req)
    assert cfg.start-cfg.fetch_start == 2000*DAY
    data = streams(cfg, golden=False)
    # only 1999 BTC warmup bars, other streams remain complete
    data['BTCUSDT'] = (data['BTCUSDT'][0][1:], *data['BTCUSDT'][1:])
    with pytest.raises(RotationError) as error:
        fetch_rotation(cfg, lambda s, a, b: data[s])
    assert error.value.code == 'INSUFFICIENT_DATA'


def test_risk_disabled_is_byte_identical_when_btc_crosses_ema():
    req = request(risk=False)
    cfg = parse_rotation(req['provider_params'], req)
    data = streams(cfg)
    baseline = replay(req, data)
    # For disabled risk, even a substituted EMA implementation must have no effect.
    original = rotation.ema200
    try:
        rotation.ema200 = lambda _: (_ for _ in ()).throw(AssertionError('disabled risk invoked EMA'))
        assert json.dumps(replay(req, data)[0].result, sort_keys=True) == json.dumps(baseline[0].result, sort_keys=True)
    finally:
        rotation.ema200 = original


def test_budget_exceeds_20000_before_fetch():
    req = request(risk=True, symbols=tuple(f'C{i}USDT' for i in range(10)))
    with pytest.raises(RotationError) as error:
        parse_rotation(req['provider_params'], req)
    assert error.value.code == 'DATA_BUDGET_EXCEEDED'


def test_fetch_deadline_no_partial_pool(monkeypatch):
    cfg = parse_rotation(request()['provider_params'], request())
    monkeypatch.setattr(rotation, 'FETCH_SECONDS', 0.03)
    release = threading.Event()
    called = threading.Event()
    def slow(s, a, b):
        called.set()
        release.wait(1)
        return streams(cfg)[s]
    start = time.monotonic()
    try:
        with pytest.raises(RotationError) as error:
            fetch_rotation(cfg, slow)
        assert error.value.code == 'DATA_FETCH_TIMEOUT'
        assert time.monotonic()-start < 0.5
    finally:
        release.set()


def test_missing_stream_never_shrinks_pool():
    cfg = parse_rotation(request()['provider_params'], request())
    data = streams(cfg)
    data['ETHUSDT'] = ([], *data['ETHUSDT'][1:])
    with pytest.raises(RotationError) as error:
        fetch_rotation(cfg, lambda s, a, b: data[s])
    assert error.value.code == 'INSUFFICIENT_DATA'
    assert cfg.symbols == SYMBOLS


def test_fetch_concurrency_shared_four_and_btc_deduplicated():
    req = request(symbols=(*SYMBOLS, 'BTCUSDT', 'XRPUSDT', 'ADAUSDT'))
    cfg = parse_rotation(req['provider_params'], req)
    data = streams(cfg, golden=False)
    lock, release = threading.Lock(), threading.Event()
    active, peak, calls = 0, 0, []
    def fake(s, a, b):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            calls.append((s, a, b))
            if active == 4:
                release.set()
        release.wait(2)
        with lock:
            active -= 1
        return data[s]
    result = fetch_rotation(cfg, fake)
    assert peak == 4
    assert len(calls) == len(result) == 6
    assert sum(s == 'BTCUSDT' for s, _, _ in calls) == 1
    assert {(a, b) for _, a, b in calls} == {(cfg.fetch_start, cfg.end)}


def http(req, metric_series=None):
    body = {'backtest': req}
    if metric_series is not None:
        body['metric_series'] = metric_series
    class Request:
        async def json(self):
            return body
    return json.loads(asyncio.run(api.run_backtest(Request(), authorization=None)).body)


@pytest.mark.parametrize('field,value', [('market', 'futures'), ('direction', 'short'), ('direction', 'both'), ('leverage', 2), ('timeframe', '4h')])
def test_http_invalid_modes_before_fetch(monkeypatch, field, value):
    monkeypatch.setattr(api, '_verify_bearer', lambda _: None)
    monkeypatch.setattr(api, '_fetch_ohlcv_raw', lambda *a: pytest.fail('invalid params reached fetch'))
    req = request()
    req[field] = value
    assert http(req)['error_type'] == 'INVALID_PARAMS'


def test_registered_http_runner_and_optional_metric(monkeypatch):
    monkeypatch.setattr(api, '_verify_bearer', lambda _: None)
    req = request()
    cfg = parse_rotation(req['provider_params'], req)
    data = streams(cfg)
    def raw(exchange, market, symbol, timeframe, start, end):
        assert (market, timeframe, start, end) == ('spot', '1d', cfg.fetch_start, cfg.end)
        return [[r['ts']*1000, r['open'], r['close'], r['open'], r['close'], '1'] for r in data[symbol][0]], 'fixture', False, True
    monkeypatch.setattr(api, '_fetch_ohlcv_raw', raw)
    response = http(req)
    assert response['result_status'] == 'success'
    assert response['schema_version'] == 'cutie.backtest_result.v4'
    assert response['equity_curve'][-1]['equity'] == '1469'
    metric = {'metric': 'btc_dominance', 'source': 'coingecko', 'unit': 'percent',
              'points': [{'ts': t, 'available_at': t+DAY, 'value': '50'} for t in range(T0-20*DAY, cfg.end, DAY)]}
    metric['hash'] = canonical_json_sha256(metric)
    response2 = http(req, metric)
    assert response2['input_manifests']['metric_series']['hash'] == metric['hash']
    assert response['fills'] == response2['fills']


PATCONF2A_WIRED = set("""adx_di_cross bias_reversion bollinger_breakout bollinger_reversal bollinger_squeeze_breakout
breakout cci_rsi ema_cross ema_pullback ema_rsi_pullback ema_trend_rsi ema_triple_alignment
ichimoku_cloud_breakout keltner_breakout macd macd_above_zero parabolic_sar roc rsi_reversal
stoch_oversold_cross supertrend volume_breakout""".split()) | set("""
bullish_engulfing hammer_pin_bar morning_star three_white_soldiers bullish_doji_reversal
inside_bar_breakout bearish_engulfing shooting_star evening_star three_black_crows bearish_doji_reversal
double_bottom inverse_head_shoulders double_top head_shoulders""".split()) | set("""
macd_bullish_divergence rsi_bullish_divergence macd_bearish_divergence rsi_bearish_divergence
chan_3buy chan_3sell fibonacci_retracement red_streak_rsi vwap_reversion""".split())  # P-PATCONF-2b1 / 2b2


def test_catalog_existing_entries_byte_identical():
    root = Path(__file__).resolve().parents[2]
    # 集成 D：基线由 10-D 头 b0e150a（基于旧 main 09bd963）改为集成起点 main 32ae030；
    # 10-D 不改 provider，32ae030 即「轮动注册前」，旧基线会把 main 自 09bd963 起的 catalog 变化误判为轮动改动。
    source = subprocess.check_output(['git', 'show', '3fe906a:backtesting-py/cutie_backtesting_provider.py'], cwd=root, text=True)
    baseline = types.ModuleType('rotation_catalog_baseline')
    baseline.__file__ = str(root/'backtesting-py/cutie_backtesting_provider.py')
    sys.modules[baseline.__name__] = baseline
    exec(compile(source, baseline.__file__, 'exec'), baseline.__dict__)
    symbols = ['BTCUSDT', 'ETHUSDT']
    for tool_id, spec in baseline.TOOL_SPECS.items():
        old = json.dumps(baseline._catalog_tool(tool_id, spec, symbols), ensure_ascii=False, separators=(',', ':')).encode()
        # _catalog_tool 的 param_schema.properties 是 spec["param_schema_properties"] 本身（不拷贝）；下面会 pop 定仓键，
        # 不深拷贝 spec 就会把全局 TOOL_SPECS 改掉，污染同进程后跑的 10b* / plow1 用例（not in tool param_schema）。
        entry = api._catalog_tool(tool_id, deepcopy(api.TOOL_SPECS[tool_id]), symbols)
        properties = entry['param_schema']['properties']
        if tool_id in api.POSITION_SIZING_TEMPLATE_STOP_TOOLS or (
                tool_id in getattr(baseline, 'POSITION_SIZING_PENDING_TOOLS', ())
                and tool_id not in api.POSITION_SIZING_PENDING_TOOLS):
            # 10-B2a：这 4 个模板接了按风险定仓，schema 恢复定仓键是唯一允许的差异；去掉后仍须逐字节相同。
            # 10-B2d：基线待核名单里、现已移出的模板（含走共享止损路径的 CME 缺口）同此口径。
            assert set(api.POSITION_SIZE_KEYS) <= set(properties), tool_id
            for key in api.POSITION_SIZE_KEYS:
                properties.pop(key)
        # P-PATCONF-2a / 2b1 / 2b2：白名单 22 + 15 + 9 个模板多出形态确认两键是唯一允许的差异，剔除后逐字节比对；其余模板不得出现这两键。
        pattern_keys = {key for key in properties if key.startswith('filter_pattern_confirm_')}
        if tool_id.removeprefix('local.backtesting_py.') in PATCONF2A_WIRED:
            # 不 pop：properties 是 TOOL_SPECS 的活引用，pop 会删掉已发布 schema 污染后续用例；改为重建字典。
            assert {k: properties[k] for k in pattern_keys} == PATTERN_CONFIRM_PARAM_SCHEMA_PROPERTIES, tool_id
            properties = entry['param_schema']['properties'] = {
                k: v for k, v in properties.items() if k not in pattern_keys}
        else:
            assert not pattern_keys, tool_id
        # 7-P3 接入过滤层的工具（基线时在未接名单里，7-P3a 的 6 个 K 线形态也在内）只允许多出 filter_* 键，其余逐字节不变。
        if tool_id in getattr(baseline, 'FILTER_LAYER_UNWIRED_TOOLS', ()) and tool_id not in api.FILTER_LAYER_UNWIRED_TOOLS:
            added = {key for key in properties if key.startswith('filter_')}
            assert added == set(FILTER_PARAM_SCHEMA_PROPERTIES), tool_id
            entry['param_schema']['properties'] = {k: v for k, v in properties.items() if k not in added}
        # TURTLE-TIME：海龟 runner 接入时间层，只允许多出 time_* 9 键，其余逐字节不变。
        if spec.get('runner') == api.TURTLE_RUNNER:
            current = entry['param_schema']['properties']
            added = {key for key in current if key.startswith('time_')}
            assert added == set(api._TIME_PARAM_SCHEMA_PROPERTIES), tool_id
            entry['param_schema']['properties'] = {k: v for k, v in current.items() if k not in added}
        # CALEXCH：calendar_schedule 补上与 opening_range_breakout 同形同默认的 exchange 键，是唯一允许的差异。
        if tool_id == 'local.backtesting_py.calendar_schedule':
            current = entry['param_schema']['properties']
            orb = api.TOOL_SPECS['local.backtesting_py.opening_range_breakout']['param_schema_properties']
            assert current.get('exchange') == orb['exchange'], tool_id
            entry['param_schema']['properties'] = {k: v for k, v in current.items() if k != 'exchange'}
        new = json.dumps(entry, ensure_ascii=False, separators=(',', ':')).encode()
        assert old == new, tool_id
    # 集成 D：原断言 len == 基线+1 只算轮动；起点 main 32ae030 之后同批合入做空形态一 / 二各 2 个工具，改为逐 id 比对。
    assert set(api.TOOL_SPECS) - set(baseline.TOOL_SPECS) == {
        TOOL_ID,
        'local.backtesting_py.macd_bearish_divergence', 'local.backtesting_py.rsi_bearish_divergence',
        'local.backtesting_py.double_top', 'local.backtesting_py.head_shoulders',
        'local.backtesting_py.chan_3sell',  # SHORT-PAT-3
        'local.backtesting_py.bearish_engulfing', 'local.backtesting_py.shooting_star',
        'local.backtesting_py.evening_star',  # SHORT-PAT-4
        'local.backtesting_py.three_black_crows', 'local.backtesting_py.bearish_doji_reversal',  # SHORT-PAT-5
        'local.backtesting_py.event_window',  # P-EVENT0
        'local.backtesting_py.macro_release_breakout', 'local.backtesting_py.macro_surprise_direction',
        'local.backtesting_py.fomc_reversal',  # Q18
        'local.backtesting_py.fear_greed_scale_in',  # P1
        'local.backtesting_py.funding_settlement_reversal',  # P2
        'local.backtesting_py.top_long_short_reversal',  # S3
    }
    entry = api._catalog_tool(TOOL_ID, api.TOOL_SPECS[TOOL_ID], symbols)
    assert entry['markets'] == ['spot']
    assert entry['timeframes'] == ['1d']
    assert entry['output_schema']['schema_version'] == 'cutie.backtest_result.v4'

@pytest.mark.parametrize('key,value', [('top_k', 0), ('top_k', 4), ('top_k', 1.0), ('btc_risk_enabled', 'false'), ('direction', 'both'), ('leverage', 1)])
def test_param_bounds_before_fetch(monkeypatch, key, value):
    monkeypatch.setattr(api, '_verify_bearer', lambda _: None)
    monkeypatch.setattr(api, '_fetch_ohlcv_raw', lambda *a: pytest.fail('bad params fetched'))
    req = request()
    req['provider_params'][key] = value
    assert http(req)['error_type'] == 'INVALID_PARAMS'


@pytest.mark.parametrize('key,value', [('position_size_risk_pct', 1), ('compound', False), ('position_size_qty_step', 0.1)])
def test_position_sizing_keys_rejected_as_unwired_before_fetch(monkeypatch, key, value):
    # 集成 D：轮动登记为不接定仓 runner，拒绝形状与 main 其它不接 runner 一致
    monkeypatch.setattr(api, '_verify_bearer', lambda _: None)
    monkeypatch.setattr(api, '_fetch_basket_leg_klines', lambda *a: pytest.fail('sizing params fetched'))
    monkeypatch.setattr(api, '_fetch_ohlcv_raw', lambda *a: pytest.fail('sizing params fetched'))
    req = request()
    req['provider_params'][key] = value
    out = http(req)
    assert out['error_type'] == 'INVALID_PARAMS'
    assert 'not wired' in out['raw_report']['position_sizing']['rejections'][0]['reason']


def test_defaults_enable_risk_k_two():
    req = request()
    del req['provider_params']['top_k']
    del req['provider_params']['btc_risk_enabled']
    cfg = parse_rotation(req['provider_params'], req)
    assert cfg.top_k == 2 and cfg.risk_enabled is True
    assert cfg.start-cfg.fetch_start == 2000*DAY


def test_actual_bars_budget_rejects_extra_rows():
    req = request()
    cfg = parse_rotation(req['provider_params'], req)
    data = streams(cfg)
    data['SOLUSDT'] = (data['SOLUSDT'][0]*1000, *data['SOLUSDT'][1:])
    with pytest.raises(RotationError) as error:
        fetch_rotation(cfg, lambda s, a, b: data[s])
    assert error.value.code == 'DATA_BUDGET_EXCEEDED'


def test_disabled_risk_low_btc_keeps_rotation_ledger():
    req = request()
    cfg = parse_rotation(req['provider_params'], req)
    data = streams(cfg)
    baseline = replay(req, data)[0].result
    for row in data['BTCUSDT'][0]:
        if row['ts'] >= T0+2*DAY:
            row['close'] = '50'
    result = replay(req, data)[0].result
    for key in ('fills', 'snapshots', 'equity_curve', 'metrics'):
        assert json.dumps(result[key], sort_keys=True) == json.dumps(baseline[key], sort_keys=True)


def test_http_execution_deadline_includes_runner(monkeypatch):
    import portfolio_rotation_http as adapter
    monkeypatch.setattr(api, '_verify_bearer', lambda _: None)
    monkeypatch.setattr(adapter, 'EXECUTION_SECONDS', 0.03)
    req = request()
    cfg = parse_rotation(req['provider_params'], req)
    monkeypatch.setattr(adapter, 'fetch_rotation', lambda *a, **kw: streams(cfg))
    release = threading.Event()
    def blocked(*a, **kw):
        release.wait(1)
        return replay(req)
    monkeypatch.setattr(adapter, 'run_rotation', blocked)
    start = time.monotonic()
    try:
        result = http(req)
        assert result['error_type'] == 'EXECUTION_TIMEOUT'
        assert result['limitations']['reason'] == 'execution_deadline'
        assert time.monotonic()-start < 0.5
    finally:
        release.set()


def test_risk_cannot_report_success_with_unsellable_dust():
    req = request(risk=True)
    for r in req['instrument_rules'].values():
        r['min_notional'] = '500'
    cfg = parse_rotation(req['provider_params'], req)
    data = streams(cfg, golden=False)
    for row in data['BTCUSDT'][0]:
        if row['ts'] >= T0+2*DAY:
            row['close'] = '50'
    for s in SYMBOLS:
        for row in data[s][0]:
            if row['ts'] >= T0+3*DAY:
                row['open'] = row['close'] = '1'
    with pytest.raises(RotationError) as error:
        replay(req, data)
    assert error.value.reason == 'liquidation_below_minimum'


def test_shared_slots_across_two_simultaneous_runs():
    from concurrent.futures import ThreadPoolExecutor
    req = request()
    cfg = parse_rotation(req['provider_params'], req)
    data = streams(cfg)
    lock, release = threading.Lock(), threading.Event()
    active, peak = 0, 0
    def fake(s, a, b):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 4:
                release.set()
        release.wait(2)
        with lock:
            active -= 1
        return data[s]
    with ThreadPoolExecutor(max_workers=2) as pool:
        runs = [pool.submit(fetch_rotation, cfg, fake) for _ in range(2)]
        assert all(set(f.result()) == set(SYMBOLS) | {'BTCUSDT'} for f in runs)
    assert peak == 4
