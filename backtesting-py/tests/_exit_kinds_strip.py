"""Strip the P6 ``raw_report.trade_exit_kinds`` key before comparing against frozen bytes.

P6 新增键；冻结字节钉的是旧行为，除此键外 body 不变由 test_p6_trade_exit_kinds 的去套层逐字节对比保证。
Only this one key is removed (by name); every other key of the body stays in the comparison.
"""
from __future__ import annotations

import re

KEY = 'trade_exit_kinds'


def without_exit_kinds(body):
    """Return a shallow copy of a response body whose raw_report lacks ``trade_exit_kinds``."""
    if not isinstance(body, dict) or not isinstance(body.get('raw_report'), dict):
        return body
    out = dict(body)
    out['raw_report'] = {k: v for k, v in body['raw_report'].items() if k != KEY}
    return out


_TEXT = re.compile(r',"trade_exit_kinds":\[[^\]]*\]')


def without_exit_kinds_text(text):
    """Same removal on an unparsed JSON response text (the list holds only flat seq/exit_kind objects)."""
    return _TEXT.sub('', text)
