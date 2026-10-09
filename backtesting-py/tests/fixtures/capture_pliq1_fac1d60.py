"""Capture once at fac1d60 (integration I head, before P-LIQ1); tests never regenerate this immutable baseline.

Two off-state proofs, each case the sorted-key JSON of the full HTTP body (report_path dropped):
  * lev1/<template>/<spot|futures|sizing>: the eight long pattern templates plus red_streak_rsi without
    leverage must stay byte-identical (`tests/_pliq1_cases.py` LEV1, unmodified fixture frames);
  * control/<double_top|head_shoulders>/<scenario>/<sizing>: the short control group under the same
    lev=10 scenario frames P-LIQ1 asserts on the long templates.
Run from a detached fac1d60 checkout with `tests/_pliq1_cases.py` copied in (it is new in P-LIQ1).
"""
from pathlib import Path
import json
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import _pliq1_cases as q

BASELINE_SHA = 'fac1d60'


def cases():
    out = {f'lev1/{key}': ('lev1', case) for key, case in q.lev1_cases().items()}
    out.update({f'control/{key}': ('control', case) for key, case in q.control_cases().items()})
    return out


def snapshot(monkeypatch, tmp_path, kind, case):
    if kind == 'lev1':
        return q.lev1_snapshot(monkeypatch, tmp_path, *case)
    return q.control_snapshot(monkeypatch, tmp_path, *case)


if __name__ == '__main__':
    import tempfile
    sha = subprocess.check_output(['git', 'rev-parse', '--short=7', 'HEAD'], text=True).strip()
    assert sha == BASELINE_SHA, sha
    values = {}
    for key, (kind, case) in cases().items():
        with pytest.MonkeyPatch.context() as mp, tempfile.TemporaryDirectory() as reports:
            values[key] = snapshot(mp, Path(reports), kind, case)
    Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).with_name('pliq1_fac1d60.json')).write_text(
        json.dumps(dict(baseline_sha=sha, cases=values), indent=2, ensure_ascii=False) + '\n')
    print('CAPTURED', len(values))
