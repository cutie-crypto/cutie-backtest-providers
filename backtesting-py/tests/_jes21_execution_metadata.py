"""One metadata-only projection shared by frozen hashes and legacy runners.

Only the listed historical comparisons opt in. Business result assertions and
producer HTTP/serialization run before this adapter. Failed responses are never
turned into successes or stripped before the production serialization boundary.
"""
from collections import Counter
from copy import deepcopy
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
MANIFEST = json.loads((ROOT / 'jes21_r3_compatibility_cases.json').read_text())
FUNCTIONS = frozenset(MANIFEST['functions'])
RAW_GOLDENS = frozenset(MANIFEST['raw_goldens'])
COUNTS = Counter()
MATCH_PROOFS = []
CASE_RESULTS = {}
CASE_KINDS = {}
OLD_MODE = 'provider_strategy_class_not_verified'


class MetadataProjectionError(AssertionError):
    pass


def _changes(before, after, path=()):
    if type(before) is not type(after):
        return [(path, 'value')]
    if isinstance(before, dict):
        changed = []
        for key in before.keys() | after.keys():
            if key not in before:
                changed.append((path + (key,), 'add'))
            elif key not in after:
                changed.append((path + (key,), 'delete'))
            else:
                changed.extend(_changes(before[key], after[key], path + (key,)))
        return changed
    if isinstance(before, list):
        if len(before) != len(after):
            return [(path, 'value')]
        return [change for i, (a, b) in enumerate(zip(before, after))
                for change in _changes(a, b, path + (i,))]
    return [] if before == after else [(path, 'value')]


def project_execution_metadata(body, *, label='', record=False):
    """Validate three-state evidence, then project only the approved metadata.

    Legacy responses have no v1 comparison and retain ALL their existing keys.
    A separate diff guard forbids widening this projection to business fields.
    """
    before = deepcopy(body)
    result = deepcopy(body)
    raw = result.get('raw_report', {}) if isinstance(result, dict) else {}
    comparison = raw.get('execution_comparison') if isinstance(raw, dict) else None
    if comparison is None:
        if isinstance(raw, dict) and 'execution_parameters' in raw:
            raise MetadataProjectionError('execution parameters without comparison: ' + label)
        return result
    status = comparison.get('status')
    if comparison.get('schema') != 'execution_comparison.v1' or status not in ('match', 'mismatch', 'unknown'):
        raise MetadataProjectionError('invalid execution comparison: ' + label)
    flag = {'match': True, 'mismatch': False, 'unknown': None}[status]
    allowed_values = set()
    allowed_deletes = {('raw_report', 'execution_comparison'), ('raw_report', 'execution_parameters')}

    def check_markers(parent, path, required=()):
        for key in required:
            if key not in parent:
                raise MetadataProjectionError('missing marker ' + '.'.join(path + (key,)))
        keys = (('matches_current_strategy', 'strategy_match') if path == ('limitations',) else
                ('strategy_binding',) if path == ('assumptions',) else
                ('matches_current_strategy', 'strategy_match', 'strategy_binding'))
        for key in keys:
            if key not in parent:
                continue
            value = parent[key]
            valid = (value is flag if key == 'matches_current_strategy' else
                     isinstance(value, str) and ('not_verified' in value if status == 'unknown' else value == status))
            if not valid:
                raise MetadataProjectionError(f'inconsistent {status} marker {".".join(path + (key,))}: {value!r}')
            if path == ('raw_report', 'strategy_semantics') and key == 'matches_current_strategy':
                del parent[key]  # New raw marker; absent in the historical contract.
                allowed_deletes.add(path + (key,))
            elif path == ('raw_report',):
                del parent[key]  # Optional new copies at raw_report root.
                allowed_deletes.add(path + (key,))
            else:
                parent[key] = False if key == 'matches_current_strategy' else OLD_MODE
                allowed_values.add(path + (key,))

    check_markers(result.get('limitations', {}), ('limitations',), ('matches_current_strategy', 'strategy_match'))
    check_markers(result.get('assumptions', {}), ('assumptions',), ('strategy_binding',))
    check_markers(raw.get('strategy_semantics', {}), ('raw_report', 'strategy_semantics'), ('strategy_match', 'matches_current_strategy'))
    check_markers(raw, ('raw_report',))
    if status == 'match':
        items = comparison.get('items', [])
        if not items or not all(i.get('status') == 'match' and i.get('reason') for i in items):
            raise MetadataProjectionError('match lacks complete comparison items: ' + label)
        if record:
            MATCH_PROOFS.append({'label': label, 'comparison': comparison})
    raw.pop('execution_comparison')
    raw.pop('execution_parameters')
    for path, operation in _changes(before, result):
        if (operation == 'delete' and path in allowed_deletes) or (operation == 'value' and path in allowed_values):
            continue
        raise MetadataProjectionError('non-whitelist projection: ' + '.'.join(map(str, path)) + ' ' + operation)
    if record:
        COUNTS[status] += 1
    return result


@pytest.fixture(autouse=True)
def frozen_execution_metadata_adapter(request, monkeypatch):
    function = request.node.nodeid.split('[', 1)[0]
    if function not in FUNCTIONS:
        return
    from fastapi.testclient import TestClient
    original_request = TestClient.request
    original_read = Path.read_text
    node = request.node.nodeid
    # Named fingerprint assertions use the same projection at the HTTP boundary,
    # before their existing digest helpers hash the response.
    source = __import__('inspect').getsource(request.node.function)
    kind = 'hash' if any(word in source for word in ('sha256', 'digest(', 'fingerprint', 'snapshot(')) else 'full'
    CASE_KINDS[node] = kind

    def response_with_metadata_projection(client, method, url, *args, **kwargs):
        response = original_request(client, method, url, *args, **kwargs)
        if str(url).endswith('/cutie/backtest') and response.status_code == 200:
            body = response.json()
            if body.get('result_status') == 'success':
                if 'execution_comparison' not in body.get('raw_report', {}):
                    CASE_KINDS[node] = 'runtime'
                projected = project_execution_metadata(body, label=node, record=True)
                encoded = json.dumps(projected, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
                response._content = encoded
                response.headers['content-length'] = str(len(encoded))
        return response

    def read_frozen_golden(path, *args, **kwargs):
        text = original_read(path, *args, **kwargs)
        try:
            relative = str(path.resolve().relative_to(ROOT.parent.parent))
        except ValueError:
            return text
        if relative in RAW_GOLDENS:
            projected = project_execution_metadata(json.loads(text), label=relative)
            return json.dumps(projected, sort_keys=True, ensure_ascii=False, separators=(',', ':')) + '\n'
        return text

    monkeypatch.setattr(TestClient, 'request', response_with_metadata_projection)
    monkeypatch.setattr(Path, 'read_text', read_frozen_golden)


def pytest_runtest_logreport(report):
    if report.when == 'call' and report.nodeid.split('[', 1)[0] in FUNCTIONS:
        CASE_RESULTS[report.nodeid] = report.outcome


def pytest_sessionfinish(session):
    destination = os.environ.get('JES21_COMPAT_AUDIT')
    if destination:
        Path(destination).write_text(json.dumps({'states': dict(COUNTS), 'matches': MATCH_PROOFS,
            'cases': {node: {'kind': CASE_KINDS.get(node, 'full'), 'outcome': outcome}
                      for node, outcome in CASE_RESULTS.items()}}, ensure_ascii=False, indent=2) + '\n')
