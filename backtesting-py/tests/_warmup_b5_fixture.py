"""Opt-in synthetic history, gated by old request/main and financial fingerprints."""
import hashlib
import json
from functools import lru_cache
from pathlib import Path
from _complete_seed_fixture import extend_seed


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False).encode()).hexdigest()


def main_digest(main):
    return hashlib.sha256(main.to_json(orient='split', date_unit='ns',
                                      double_precision=15).encode()).hexdigest()


@lru_cache(maxsize=1)
def cases():
    return json.loads((Path(__file__).parent / 'fixtures/warmup_b5_financial.json').read_text())['cases']


def matches(request, main):
    return [row for row in cases() if digest(row['request']) == digest(request)
            and row['main_sha256'] == main_digest(main)]


def seed_reader(request, main, old_reader):
    audited = bool(matches(request, main))
    def read(*args, **kwargs):
        seed = old_reader(*args)
        return extend_seed(seed, args[3], args[5], args[6]) if audited else seed
    return read


def check_financial(request, main, body):
    rows = matches(request, main)
    if rows:
        assert body['result_status'] == 'success', body
        actual = digest({key: body[key] for key in
                         ('trades', 'equity_curve', 'metrics', 'data_manifest')})
        assert all(row['financial_sha256'] == actual for row in rows), 'financial fields changed; stop'
    return body
