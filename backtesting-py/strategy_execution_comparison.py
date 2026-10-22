"""Conservative, local evidence for legacy template execution.

Only the closed EMA grammar below has complete rule coverage. Other templates
still expose observed execution facts, without inferring semantics from names.
No client-supplied match flag or execution attestation is trusted.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import re

EMA_TOOL = 'local.backtesting_py.ema_cross'
TURTLE_TOOL = 'local.backtesting_py.turtle'
UNKNOWN_MODE = 'provider_strategy_class_not_verified'


def _dict(value):
    return value if isinstance(value, dict) else {}


def _number(value):
    try:
        if isinstance(value, bool):
            return None
        number = Decimal(str(value))
        return format(number.normalize(), 'f') if number.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _known(kind, value, unit, source):
    number = _number(value)
    if number is None:
        return {'status': 'unknown', 'source': source}
    return dict(status='known', type=kind, value=number, unit=unit, source=source)


def _none(source):
    return dict(status='none', source=source)


def _risk_evidence(tool_id, built):
    cls = built.get('strategy')
    # Tool AND the actual class are required. A tool/name alone proves nothing.
    ema = tool_id == EMA_TOOL and getattr(cls, '__name__', '') == 'EmaCrossStrategy'
    turtle = tool_id == TURTLE_TOOL and getattr(cls, '__name__', '') == 'TurtleStrategy'
    overlay = any(base.__name__ == '_FixedRiskMixin' and base.__module__ == 'cutie_backtesting_provider'
                  for base in getattr(cls, '__mro__', ()))
    ema = ema and overlay
    risk = deepcopy(_dict(getattr(cls, '_risk', None))) if overlay else {}
    source = 'built.strategy._risk'
    unknown = dict(status='unknown', source='unmapped_native_execution')
    stop, take, position = (dict(unknown) for _ in range(3))
    if risk.get('atr_stop_multiplier'):
        stop = _known('atr', risk['atr_stop_multiplier'], 'atr_multiple', source)
        stop['period'] = risk.get('risk_atr_period')
    elif 'stop_loss_pct' in risk:
        stop = _known('fixed_pct', risk['stop_loss_pct'], 'pct_fraction', source)
    elif risk.get('trailing_stop_pct'):
        stop = _known('trailing_pct', risk['trailing_stop_pct'], 'pct_percent', source)
    elif ema:
        stop = _none(source)
    if 'take_profit_pct' in risk:
        take = _known('fixed_pct', risk['take_profit_pct'], 'pct_fraction', source)
    elif risk.get('take_profit_r'):
        take = _known('risk_multiple', risk['take_profit_r'], 'risk_multiple', source)
    elif risk.get('tp1_r'):
        take = dict(status='known', type='levels', unit='risk_multiple', source=source,
                    levels=[dict(value=_number(risk[f'tp{n}_r']), close_pct=_number(risk[f'tp{n}_close_pct']),
                                 close_unit='pct_percent') for n in (1, 2, 3) if risk.get(f'tp{n}_r')])
    elif ema:
        take = _none(source)
    for key, kind, unit in (('position_size_pct', 'fixed_pct', 'pct_fraction'),
                             ('position_size_notional', 'fixed_notional', 'quote_currency'),
                             ('position_size_risk_pct', 'risk_pct', 'pct_fraction')):
        if key in risk:
            position = _known(kind, risk[key], unit, source)
            break
    else:
        if ema:
            position = dict(status='known', type='engine_default', source='EmaCrossStrategy._risk_buy')
    if turtle:
        source = 'built.strategy._stop_multiple'
        stop = _known('atr', getattr(cls, '_stop_multiple', None), 'atr_multiple', source)
        trisk = _dict(getattr(cls, '_turtle_risk', None))
        take = (_known('fixed_pct', trisk['take_profit_pct'], 'pct_percent', 'built.strategy._turtle_risk')
                if 'take_profit_pct' in trisk else _none('built.strategy._turtle_risk'))
        position = _known('risk_pct', getattr(cls, '_unit_risk', None), 'pct_percent', 'built.strategy._unit_risk')
        risk = deepcopy(trisk)
    # Dynamic stops can coexist with the initial protective stop. Keep both.
    if stop['status'] == 'known':
        stop.update({key: deepcopy(risk[key]) for key in ('trailing_stop_pct', 'breakeven_stop') if key in risk})
    return ema, risk, stop, take, position


def execution_evidence(*, body, built=None, tool_id=None, params=None, context=None):
    """Build evidence AFTER the engine has built/defaulted its class/config.

    context contains actual validated runner locals, not HTTP top-level values.
    provider_params preserves frozen inputs and overlays observed normalized
    class values. Unmapped defaults explicitly remain partial.
    """
    built, params, context = _dict(built), _dict(params), _dict(context)
    strategy = _dict(_dict(body.get('backtest')).get('strategy'))
    spec = _dict(strategy.get('strategy_spec'))
    ema, risk, stop, take, position = _risk_evidence(tool_id, built)
    actual_params = deepcopy(params)
    cls = built.get('strategy')
    if ema:
        actual_params.update(ema_fast=cls._ema_fast, ema_slow=cls._ema_slow)
    ep = dict(schema='execution_parameters.v1', template_name=built.get('executed_name'),
              provider_tool_id=tool_id, provider_params=actual_params,
              provider_params_status='partial', provider_params_source='frozen_inputs_and_built_class',
              stop_loss=stop, take_profit=take, position=position,
              risk_config=risk, live_execution_parity=False)
    for key in ('symbol', 'market', 'timeframe', 'start_at', 'end_at'):
        ep[key] = context.get(key)
    for key in ('fee_bps', 'slippage_bps'):
        ep[key] = _number(context.get(key))
    items = []

    def item(path, requested, executed, unit=None, *, known=True, reason='frozen_strategy_spec_vs_built_execution'):
        status = ('match' if requested == executed else 'mismatch') if known else 'unknown'
        items.append(dict(path=path, requested=deepcopy(requested), executed=deepcopy(executed),
                          unit=unit, status=status, reason=reason))

    for key in ('symbol', 'market', 'timeframe'):
        item(key, spec.get(key), ep[key], known=key in spec and ep[key] is not None)
    rules = _dict(spec.get('rules'))
    # A deliberately small grammar, already used by the platform EMA router.
    # No substring matching, no NLP/model prose, no inferred complete coverage.
    entry = rules.get('entry')
    parsed = (re.fullmatch(r'EMA\(([0-9]+)\) crosses above EMA\(([0-9]+)\)', entry[0])
              if isinstance(entry, list) and len(entry) == 1 and isinstance(entry[0], str) else None)
    executed_periods = [cls._ema_fast, cls._ema_slow] if ema else None
    requested_periods = [int(parsed[1]), int(parsed[2])] if parsed else None
    item('rules.entry', requested_periods, executed_periods, 'bars', known=bool(parsed and ema),
         reason='closed_EMA_cross_above_grammar_vs_built_class')
    item('rules.exit', rules.get('exit'), ['opposite cross'] if ema else None,
         known=ema and rules.get('exit') == ['opposite cross'], reason='closed_EMA_opposite_cross_exit')
    item('direction', spec.get('direction'), 'long' if ema else None, known=ema and 'direction' in spec)
    for path, executed in (('stop_loss', stop), ('take_profit', take), ('position_sizing', position)):
        requested = spec.get(path)
        actual = {k: v for k, v in executed.items() if k not in ('status', 'source')}
        known = path in spec and executed['status'] != 'unknown'
        if actual.get('type') == 'fixed_pct' and actual.get('unit') == 'pct_percent':
            actual.update(value=_number(Decimal(actual['value']) / 100), unit='pct_fraction')
        if requested is None and path in spec:
            expected = {'type': 'engine_default'} if path == 'position_sizing' else {}
        elif (isinstance(requested, dict) and set(requested) == {'type', 'pct'}
              and requested.get('type') == 'fixed_pct' and _number(requested['pct']) is not None):
            expected = dict(type='fixed_pct', value=_number(Decimal(str(requested['pct'])) / 100), unit='pct_fraction')
            # Units come from the declared spec contract, never from magnitude.
        else:
            expected = requested
            known = False
        item(path, expected, actual if executed['status'] != 'unknown' else None,
             actual.get('unit'), known=known, reason='spec_pct_percent_vs_built_risk_explicit_unit')
    allowed_spec = {'schema', 'strategy_type', 'symbol', 'market', 'timeframe', 'direction', 'parameters',
                    'rules', 'stop_loss', 'take_profit', 'position_sizing', 'strategy_name', 'name', 'description'}
    allowed_params = {'ema_fast', 'ema_slow', 'exchange', 'stop_loss_pct', 'take_profit_pct',
                      'position_size_pct', 'position_size_notional', 'risk_layer_enabled', 'leverage'}
    requested_params = _dict(spec.get('parameters'))
    item('parameters.ema_periods', [requested_params.get('ema_fast'), requested_params.get('ema_slow')],
         executed_periods, 'bars', known=ema and set(requested_params) == {'ema_fast', 'ema_slow'})
    allowed_snapshot = {'draft_id', 'strategy_id', 'id', 'status', 'strategy_name', 'name', 'description',
                        'strategy_kind', 'asset', 'direction', 'timeframe', 'rules', 'evidence',
                        'strategy_spec', 'publish_payload'}
    asset = strategy.get('asset')
    asset_consistent = asset is None or (isinstance(asset, str) and isinstance(ep['symbol'], str)
        and (asset == ep['symbol'] or re.fullmatch(re.escape(asset) + r'(USDT|USDC|BUSD)', ep['symbol'])))
    complete = bool(
        ema and parsed and spec.get('schema') == 'cutie.strategy_spec.v1'
        and spec.get('strategy_type') == 'trend' and not (set(spec) - allowed_spec)
        and set(rules) == {'entry', 'exit', 'risk'} and rules.get('risk') == []
        and strategy.get('rules', rules) == rules
        and not (set(params) - allowed_params) and risk.get('leverage', 1) == 1
        and not any(risk.get(k) for k in ('atr_stop_multiplier', 'trailing_stop_pct', 'breakeven_stop',
                                         'max_holding_bars', 'take_profit_r', 'tp1_r', 'position_sizing_enabled'))
        and getattr(cls, '_time_config', None) is None and getattr(cls, '_filter_config', None) is None
        and not (set(strategy) - allowed_snapshot) and asset_consistent
        and not (params.get('risk_layer_enabled') is False
                 and any(k in risk for k in ('stop_loss_pct', 'take_profit_pct')))
        and strategy.get('direction', 'long') == 'long'
        and strategy.get('timeframe', ep['timeframe']) == ep['timeframe'])
    if not complete:
        item('rules.coverage', None, None, known=False, reason='complete_rules_not_proven')
    # One proved discrepancy is enough to reject; agreement of a subset is not match.
    statuses = {i['status'] for i in items}
    status = 'mismatch' if 'mismatch' in statuses else ('unknown' if 'unknown' in statuses else 'match')
    comparison = dict(schema='execution_comparison.v1', status=status, items=items)
    return comparison, ep
