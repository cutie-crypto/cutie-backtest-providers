"""The compatibility projection must preserve all historical business evidence."""
from copy import deepcopy

import pytest

import _jes21_execution_metadata as metadata

from _jes21_execution_metadata import MetadataProjectionError, project_execution_metadata


@pytest.fixture(autouse=True)
def unit_projection_expectation(monkeypatch):
    # Anonymous synthetic unit fixtures have their own explicitly declared state.
    monkeypatch.setitem(metadata.MANIFEST['expected_status'], '', 'unknown')


def legacy_response():
    return {'result_status': 'success', 'metrics': {'total_return': '0.125', 'max_drawdown': '0.02', 'trade_count': 1},
            'trades': [{'pnl': '1250'}], 'equity_curve': [{'ts': 1767225600, 'equity': '11250'}],
            'data_manifest': {'checksum': 'frozen-data'},
            'assumptions': {'strategy_binding': 'provider_strategy_class_not_verified', 'fee_bps': '10'},
            'limitations': {'matches_current_strategy': False, 'strategy_match': 'provider_strategy_class_not_verified'},
            'raw_report': {'strategy_semantics': {'strategy_match': 'provider_strategy_class_not_verified'},
                           'legacy_metrics': {'total_return_pct': 12.5}}}


def new_response(status='unknown'):
    result = legacy_response()
    mode = 'provider_strategy_class_not_verified' if status == 'unknown' else status
    flag = {'match': True, 'mismatch': False, 'unknown': None}[status]
    result['assumptions']['strategy_binding'] = mode
    result['limitations'].update(strategy_match=mode, matches_current_strategy=flag)
    result['raw_report']['strategy_semantics'].update(strategy_match=mode, matches_current_strategy=flag)
    result['raw_report']['execution_comparison'] = {'schema': 'execution_comparison.v1', 'status': status,
        'items': [{'path': 'rules.coverage', 'status': status, 'reason': 'independent_closed_fixture'}]}
    result['raw_report']['execution_parameters'] = {'schema': 'execution_parameters.v1', 'stop_loss': {'status': 'unknown'}}
    return result


@pytest.mark.parametrize('status', ['match', 'mismatch', 'unknown'])
def test_projection_preserves_the_complete_historical_response(status, monkeypatch):
    monkeypatch.setitem(metadata.MANIFEST['expected_status'], '', status)
    candidate = new_response(status)
    original = deepcopy(candidate)
    assert project_execution_metadata(candidate) == legacy_response()
    assert candidate == original


@pytest.mark.parametrize('location', ['limitations', 'raw_report'])
@pytest.mark.parametrize('status', ['match', 'mismatch', 'unknown'])
def test_inconsistent_boolean_is_rejected_before_projection(location, status, monkeypatch):
    monkeypatch.setitem(metadata.MANIFEST['expected_status'], '', status)
    candidate = new_response(status)
    marker = candidate[location] if location == 'limitations' else candidate[location]['strategy_semantics']
    marker['matches_current_strategy'] = 'true'
    with pytest.raises(MetadataProjectionError, match='inconsistent'):
        project_execution_metadata(candidate)


def test_non_whitelist_metric_difference_survives_projection():
    candidate = new_response()
    candidate['metrics']['total_return'] = '0.126'
    projected = project_execution_metadata(candidate)
    assert projected['metrics']['total_return'] == '0.126'
    assert projected != legacy_response()


def test_projection_cannot_remove_existing_result_keys():
    projected = project_execution_metadata(new_response())
    assert projected['raw_report']['legacy_metrics'] == {'total_return_pct': 12.5}
    assert projected['data_manifest'] == {'checksum': 'frozen-data'}
    assert project_execution_metadata(legacy_response()) == legacy_response()


def test_similarly_named_field_outside_whitelist_is_not_normalized():
    candidate = new_response()
    candidate['assumptions']['matches_current_strategy'] = None
    projected = project_execution_metadata(candidate)
    assert projected['assumptions']['matches_current_strategy'] is None
    assert projected != legacy_response()


@pytest.mark.parametrize('label', [
    metadata.MANIFEST['raw_goldens'][0],
    'tests/test_7p3b_pattern_range_filter.py::test_off_state_byte_identical_to_main[False-default/hand]',
    'tests/test_isolated_liquidation_arbitration.py::test_runtime_mixin_off_state_bytes[default-stoch_oversold_cross]',
])
@pytest.mark.parametrize('status', ['match', 'mismatch'])
def test_recorded_status_drift_is_rejected(label, status):
    with pytest.raises(MetadataProjectionError, match='execution status changed.*expected unknown'):
        project_execution_metadata(new_response(status), label=label)


@pytest.mark.parametrize('location,key', [
    ('limitations', 'strategy_match'), ('assumptions', 'strategy_binding'),
    ('semantics', 'strategy_match'), ('raw_report', 'strategy_match'),
    ('raw_report', 'strategy_binding'),
])
@pytest.mark.parametrize('mode', ['prefix_provider_strategy_class_not_verified',
                                  'provider_strategy_class_not_verified_suffix'])
def test_unknown_mode_requires_exact_legacy_enum(location, key, mode):
    candidate = new_response()
    parent = (candidate['raw_report']['strategy_semantics'] if location == 'semantics'
              else candidate[location])
    parent[key] = mode
    with pytest.raises(MetadataProjectionError, match='inconsistent unknown marker'):
        project_execution_metadata(candidate)


def test_unrecorded_comparison_cannot_silently_opt_into_projection():
    with pytest.raises(MetadataProjectionError, match='missing expected status'):
        project_execution_metadata(new_response(), label='unrecorded-case')
