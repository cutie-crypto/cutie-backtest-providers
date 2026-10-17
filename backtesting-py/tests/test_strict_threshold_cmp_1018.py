"""#14: public-route threshold equality, strict bool validation and catalog contract."""
from decimal import Decimal

import pytest

import cutie_backtesting_provider as p
import test_p2_funding_settlement_reversal as funding
import test_q18_macro_events as macro
from strategy_funding_reversal import FundingReversalConfig
from strategy_macro_events import MacroConfig

KINDS = (macro.H2, macro.H3, funding.KIND)


def mode_params(mode):
    return {} if mode == 'default' else {'threshold_inclusive': mode == 'inclusive'}


def run_boundary(monkeypatch, tmp_path, kind, sign, boundary, mode):
    extra = mode_params(mode)
    if kind == funding.KIND:
        rate = Decimal({'below': '0.0004', 'equal': '0.0005', 'above': '0.0006'}[boundary]) * sign
        body, _ = funding.run(monkeypatch, tmp_path, {0: str(rate)}, extra)
        return body, funding.events(body)[0]
    values = macro.params(kind, **extra)
    data = macro.frame()
    if kind == macro.H2:
        size = Decimal({'below': '0.09', 'equal': '0.1', 'above': '0.11'}[boundary])
        # Nonzero expected value exercises decimal subtraction, including 2.9 - 2.8.
        values['events'] = [macro.event(expected=2.9, actual=float(Decimal('2.9') + sign * size))]
    else:
        size = Decimal({'below': '4.9', 'equal': '5', 'above': '5.1'}[boundary])
        price = float(100 + sign * size)
        data.iloc[11] = [100, max(101, price), min(99, price), price, 1]
    body = macro.post(monkeypatch, tmp_path, kind, values, data)
    return body, macro.report(body, kind)['events'][0]


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('sign', [-1, 1])
@pytest.mark.parametrize('mode', ['default', 'inclusive', 'strict'])
@pytest.mark.parametrize('boundary', ['below', 'equal', 'above'])
def test_public_threshold_boundary(monkeypatch, tmp_path, kind, sign, mode, boundary):
    body, record = run_boundary(monkeypatch, tmp_path, kind, sign, boundary, mode)
    enters = boundary == 'above' or (boundary == 'equal' and mode != 'strict')
    assert len(body['trades']) == int(enters)
    assert record['status'] == ('entered' if enters else 'skipped')
    if enters:
        assert body['trades'][0]['side'] == ('short' if sign > 0 else 'long')
    else:
        assert record['reason'] == {macro.H2: 'surprise_below_threshold', macro.H3: 'move_below_threshold',
                                    funding.KIND: 'rate_below_threshold'}[kind]
    assumptions = body['assumptions'][kind]
    assert assumptions['threshold_inclusive'] is (mode != 'strict')
    if kind == macro.H2:
        assert ('_gt_threshold;' if mode == 'strict' else '_gte_threshold;') in assumptions['threshold']
    if kind == funding.KIND:
        assert ('boundary values do not trade' if mode == 'strict' else 'boundary values trade') in assumptions['rate_rule']


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('bad', [None, 0, 1, 0.0, 'false', 'true', [], {}])
def test_invalid_type_rejected_before_fetch_and_by_parser(monkeypatch, tmp_path, kind, bad):
    extra = {'threshold_inclusive': bad}
    if kind == funding.KIND:
        funding.install(monkeypatch, tmp_path, {})
        def forbidden(*args, **kwargs):
            pytest.fail('invalid params must reject before market/funding fetch')
        monkeypatch.setattr(p, '_fetch_ohlcv', forbidden)
        monkeypatch.setattr(p, '_fetch_artifact_metric_chunk', forbidden)
        body = funding.post(extra)
        with pytest.raises(ValueError, match='threshold_inclusive must be a boolean'):
            FundingReversalConfig.parse(extra)
    else:
        values = macro.params(kind, **extra)
        body = macro.post(monkeypatch, tmp_path, kind, values, invalid=True)
        with pytest.raises(ValueError, match='threshold_inclusive must be a boolean'):
            MacroConfig.parse(values, kind)
    assert body['error_type'] == 'INVALID_PARAMS'
    assert 'threshold_inclusive must be a boolean' in body['error_message']


@pytest.mark.parametrize('kind', KINDS)
def test_catalog_exposes_boolean_default_true(kind):
    tool_id = f'local.backtesting_py.{kind}'
    schema = p._catalog_tool(tool_id, p.TOOL_SPECS[tool_id], ['BTCUSDT'])['param_schema']
    assert schema['properties']['threshold_inclusive'] == {'type': 'boolean', 'default': True}
    assert schema['additionalProperties'] is False


@pytest.mark.parametrize('mode', ['default', 'inclusive', 'strict'])
def test_zero_surprise_has_no_direction(monkeypatch, tmp_path, mode):
    values = macro.params(macro.H2, events=[macro.event(expected=0, actual=0)],
                          surprise_threshold=0, **mode_params(mode))
    body = macro.post(monkeypatch, tmp_path, macro.H2, values)
    assert not body['trades']
    assert macro.report(body, macro.H2)['events'][0]['reason'] == 'surprise_below_threshold'


def test_funding_no_settlement_retains_strict_mode():
    config = FundingReversalConfig.parse({'threshold_inclusive': False})
    config.unbound_assumptions()
    assert config.assumptions['threshold_inclusive'] is False
