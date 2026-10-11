"""Strip the P6 ``raw_report.trade_exit_kinds`` key before comparing against frozen bytes.

P6 新增键；冻结字节钉的是旧行为，除此键外 body 不变由 test_p6_trade_exit_kinds 的去套层逐字节对比保证。
Only this one key is removed (by name); every other key of the body stays in the comparison.
"""
from __future__ import annotations

import json

KEY = 'trade_exit_kinds'


def without_exit_kinds(body):
    """Return a shallow copy of a response body whose raw_report lacks ``trade_exit_kinds``."""
    if not isinstance(body, dict) or not isinstance(body.get('raw_report'), dict):
        return body
    out = dict(body)
    out['raw_report'] = {k: v for k, v in body['raw_report'].items() if k != KEY}
    return out


def without_exit_kinds_text(text):
    """Same removal on an unparsed JSON response text: parse, drop only raw_report.trade_exit_kinds, re-serialize.

    Re-serialization uses the response encoder's own form (compact separators, non-ASCII kept), so every
    other byte is unchanged; a same-named key anywhere else (e.g. inside turtle_groups) is left alone.
    """
    data = json.loads(text)
    if isinstance(data, dict) and isinstance(data.get('raw_report'), dict):
        data['raw_report'].pop(KEY, None)
    return json.dumps(data, separators=(',', ':'), ensure_ascii=False)
