"""仅第47条工具的 HTTP / catalog 适配，不修改旧 runner。"""
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError

from portfolio_rotation import (
    RotationError, parse_rotation, fetch_rotation, run_rotation, EXECUTION_SECONDS,
)

TOOL_ID = 'local.backtesting_py.portfolio_rotation'
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
                                   'reason': {'type': 'string', 'enum': ['stable_coin', 'not_tradeable', 'no_spot_rules', 'btc_benchmark']}}}},
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
    # timeout_ms 沿用 _catalog_tool 已写入的 EXECUTION_TIMEOUT_MS（本模块被 provider 导入，
    # 反向引用会循环导入，所以这里不再覆盖，避免与共享常量漂移）。
    entry['execution'].update(max_bars=20000, max_range_days=5000)
    entry['output_schema'] = {'schema_version': 'cutie.backtest_result.v4',
                              'metrics': ['total_return', 'max_drawdown', 'fill_count'],
                              'artifacts': [], 'series': ['equity_curve', 'btc_benchmark'],
                              'tables': ['fills', 'snapshots', 'input_manifests']}
    entry['report_capabilities'].update(report_url=False, formats=[])
    entry['failure_codes'] += ['DATA_BUDGET_EXCEEDED', 'DATA_FETCH_TIMEOUT', 'EXECUTION_TIMEOUT']
    return entry


def rotation_response(body, request, run_id, api):
    started = time.monotonic()
    params = request.get('provider_params')
    params = {} if params is None else params
    if not isinstance(params, dict):
        return api._validation_failure('INVALID_PARAMS', 'provider_params must be an object')
    error = api._validate_params_against_schema(params, TOOL_SPEC['param_schema_properties'])
    if error:
        return api._validation_failure('INVALID_PARAMS', error)
    try:
        config = parse_rotation(params, request)
    except (RotationError, ValueError, TypeError, ArithmeticError) as e:
        return api._validation_failure('INVALID_PARAMS', str(e)) if not isinstance(e, RotationError) or e.code == 'INVALID_PARAMS' else api._business_failure(run_id, e.code, str(e), reason=e.reason)

    @api.in_decimal128
    def fetcher(symbol, start, end):
        rows, source, central, cached = api._fetch_basket_leg_klines(
            api.CENTRAL_SUPPORTED_EXCHANGE, 'spot', symbol, '1d', start, end)
        return [{**r, 'ts': r['open_time']} for r in rows], source, central, cached

    deadline = started + EXECUTION_SECONDS

    def execute():
        streams = fetch_rotation(config, fetcher, deadline=deadline)
        output, decisions, assumptions = run_rotation(config, streams, request,
                                                      metric_series=body.get('metric_series'), deadline=deadline)
        return streams, output, decisions, assumptions

    future = _EXECUTOR.submit(execute)
    try:
        streams, output, decisions, assumptions = future.result(timeout=max(0, deadline-time.monotonic()))
    except TimeoutError:
        future.cancel()
        return api._business_failure(run_id, 'EXECUTION_TIMEOUT', '120 second execution deadline exceeded', reason='execution_deadline')
    except RotationError as e:
        return api._business_failure(run_id, e.code, str(e), reason=e.reason)
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
        'raw_report': {'decisions': decisions, 'rejections': output.rejections},
    })
