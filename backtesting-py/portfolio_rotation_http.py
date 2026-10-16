"""仅第47条工具的 HTTP / catalog 适配，不修改旧 runner。"""
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError

from portfolio_rotation import (
    RotationError, parse_rotation, fetch_rotation, run_rotation, EXECUTION_SECONDS,
)

TOOL_ID = 'local.backtesting_py.portfolio_rotation'
# EXT-901：服务端 external-result 只认 raw_report_json 为 v4 七键本体（键集严格，API-CONTRACT.md「独立组合回测 v4」）；
# connector 原样转发 raw_report，所以成功回包的 raw_report 必须就是这七键。
RAW_REPORT_V4_KEYS = ('schema_version', 'fills', 'snapshots', 'equity_curve', 'btc_benchmark', 'metrics', 'input_manifests')
RUNNER = 'portfolio_rotation_v4'
_EXECUTOR = ThreadPoolExecutor(max_workers=4)
TOOL_SPEC = {
    'name': 'Local Spot Portfolio Rotation',
    'description': 'Fixed frozen pool, weekly BTC-relative seven-day momentum, equal-weight spot rotation with optional BTC EMA200 portfolio risk.',
    'strategy_family': 'portfolio_rotation', 'runner': RUNNER, 'is_default': False,
    'markets': ['spot'], 'timeframes': ['1d'],
    'param_schema_properties': {
        'coin_pool': {
            'type': 'object', 'additionalProperties': False,
            'required': ['selected_at', 'source', 'size', 'symbols', 'excluded', 'version', 'hash'],
            'properties': {
                'selected_at': {'type': 'integer', 'minimum': 1},
                'source': {'type': 'string', 'enum': ['coins.coingecko.market_cap_rank']},
                'size': {'type': 'integer', 'minimum': 1, 'maximum': 30},
                'symbols': {'type': 'array', 'minItems': 1, 'maxItems': 30, 'items': {
                    'type': 'object', 'additionalProperties': False, 'required': ['symbol', 'rank'],
                    'properties': {'symbol': {'type': 'string'}, 'rank': {'type': 'integer', 'minimum': 1}}}},
                'excluded': {'type': 'array', 'maxItems': 30, 'items': {
                    'type': 'object', 'additionalProperties': False, 'required': ['symbol', 'rank', 'reason'],
                    'properties': {'symbol': {'type': 'string'}, 'rank': {'type': 'integer', 'minimum': 1},
                                   'reason': {'type': 'string', 'enum': ['stable_coin', 'pegged_asset', 'not_tradeable', 'no_spot_rules', 'btc_benchmark']}}}},
                'version': {'type': 'string', 'enum': ['coin_pool.v1']},
                'hash': {'type': 'string', 'minLength': 64, 'maxLength': 64},
            },
        },
        'top_k': {'type': 'integer', 'default': 2, 'minimum': 1, 'maximum': 30},
        'btc_risk_enabled': {'type': 'boolean', 'default': True},
        'direction': {'type': 'string', 'default': 'long', 'enum': ['long']},
    },
}


def rotation_catalog(entry):
    entry['supported_symbols'] = []
    entry['param_schema']['required'] = ['coin_pool']
    # Q46（Codex 复审 P2）：轮动自己按 EXECUTION_SECONDS 截止（见 handle 里的 deadline），目录声明
    # 必须等于运行时实际预算，不能沿用共享的 300s——否则 connector 等 300s、provider 120s 就自行失败。
    entry['execution'].update(timeout_ms=EXECUTION_SECONDS * 1000, max_bars=20000, max_range_days=5000)
    entry['output_schema'] = {'schema_version': 'cutie.backtest_result.v4',
                              'metrics': ['total_return', 'max_drawdown', 'fill_count', 'trade_count'],
                              'artifacts': [], 'series': ['equity_curve', 'btc_benchmark'],
                              'tables': ['fills', 'snapshots', 'input_manifests']}
    entry['report_capabilities'].update(report_url=False, formats=[])
    entry['failure_codes'] += ['DATA_BUDGET_EXCEEDED', 'DATA_FETCH_TIMEOUT', 'EXECUTION_TIMEOUT']
    return entry


def _streams_provenance(api, metas):
    """PROV-PROVENANCE：失败回包按已取流的真实来源填扁平 provenance（服务端按顶层 central_failure_reason 读）。"""
    if not metas:
        return None
    sources = {m[0] for m in metas}
    central = all(m[1] is True for m in metas)
    return {
        'provider_revision': api.PROVIDER_REVISION,
        'source': sources.pop() if len(sources) == 1 else None,
        'central_market_data_used': central,
        # 缓存命中的流本次没请求中心；attempted 只认本次真发过中心请求的流。
        'central_market_data_attempted': any(m[1] is True and m[2] is not True for m in metas),
        'central_failure_reason': api.CENTRAL_REASON_USED if central else None,
        'auth_mode': api._central_market_data_auth_mode(),
        'cache_hit': all(m[2] is True for m in metas),
    }


def _rotation_failure(api, run_id, e):
    provenance = e.provenance if isinstance(e.provenance, dict) else (
        _streams_provenance(api, [e.stream]) if e.stream is not None else None)
    return api._business_failure(run_id, e.code, str(e), reason=e.reason, market_data_provenance=provenance)


def rotation_response(body, request, run_id, api, *, tool_spec=None, parser=None, runner=None,
                      validate_series=None):
    started = time.monotonic()
    tool_spec = TOOL_SPEC if tool_spec is None else tool_spec
    parser = parse_rotation if parser is None else parser
    runner = run_rotation if runner is None else runner
    params = request.get('provider_params')
    params = {} if params is None else params
    if not isinstance(params, dict):
        return api._validation_failure('INVALID_PARAMS', 'provider_params must be an object')
    error = api._validate_params_against_schema(params, tool_spec['param_schema_properties'])
    if error:
        return api._validation_failure('INVALID_PARAMS', error)
    try:
        config = parser(params, request)
    except (RotationError, ValueError, TypeError, ArithmeticError) as e:
        return api._validation_failure('INVALID_PARAMS', str(e)) if not isinstance(e, RotationError) or e.code == 'INVALID_PARAMS' else api._business_failure(run_id, e.code, str(e), reason=e.reason)

    @api.in_decimal128
    def fetcher(symbol, start, end):
        rows, source, central, cached = api._fetch_basket_leg_klines(
            api.CENTRAL_SUPPORTED_EXCHANGE, 'spot', symbol, '1d', start, end)
        return [{**r, 'ts': r['open_time']} for r in rows], source, central, cached

    deadline = started + EXECUTION_SECONDS

    def execute():
        if validate_series is not None:
            validate_series(request.get('metric_series'), config.start, config.end - 86400)
        streams = fetch_rotation(config, fetcher, deadline=deadline)
        try:
            output, decisions, assumptions = runner(config, streams, request,
                                                          metric_series=request.get('metric_series'), deadline=deadline)
        except RotationError as e:
            if e.provenance is None and e.stream is None:
                e.provenance = _streams_provenance(api, [v[1:] for v in streams.values()])
            raise
        return streams, output, decisions, assumptions

    future = _EXECUTOR.submit(execute)
    try:
        # decisions / rejections 不随 raw_report 回传（EXT-901：服务端 v4 键集严格，且无消费方）。
        streams, output, _decisions, assumptions = future.result(timeout=max(0, deadline-time.monotonic()))
    except TimeoutError:
        future.cancel()
        return api._business_failure(run_id, 'EXECUTION_TIMEOUT', '120 second execution deadline exceeded', reason='execution_deadline')
    except RotationError as e:
        return _rotation_failure(api, run_id, e)
    except (ValueError, ArithmeticError) as e:
        return api._business_failure(run_id, 'ENGINE_ERROR', str(e), reason='portfolio_reconciliation_or_input')
    result = output.result
    return api._bounded_basket_response(run_id, {
        'schema': api.RESPONSE_SCHEMA, 'result_status': 'success',
        'provider_name': api.PROVIDER_NAME, 'provider_revision': api.PROVIDER_REVISION,
        'provider_run_id': f'bt_{run_id}', 'engine_name': api.ENGINE_NAME,
        'engine_version': api._engine_version(), 'data_source': api.DATA_SOURCE,
        'central_market_data_used': all(v[2] for v in streams.values()),
        'market_data_cache_hit': all(v[3] for v in streams.values()),
        **result, 'trades': [], 'initial_capital': str(request.get('initial_capital', '10000')),
        'assumptions': assumptions,
        'limitations': {'verification': 'external_unverified', 'verified_by_cutie': False},
        'raw_report': {k: result[k] for k in RAW_REPORT_V4_KEYS},
    })
