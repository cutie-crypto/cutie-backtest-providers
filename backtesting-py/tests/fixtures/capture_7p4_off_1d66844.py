"""Capture once at 1d66844 (before 7-P4 short filter wiring); tests never regenerate this immutable baseline.

Off-state proof for the five short templates: requests without any filter_* key must stay
byte-identical to main. Each case is the sorted-key JSON of the full HTTP body (report_path dropped).
Reuses the 10-B2b hand frames (`tests/_10b2b_cases.py`).
"""
from pathlib import Path
import json
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import _10b2b_cases as c
from _exit_kinds_strip import without_exit_kinds

BASELINE_SHA = '1d66844'
SESSION = dict(time_layer_enabled=True, time_session_start='00:00', time_session_end='23:00')


def cases():
    """case -> (tool, params)."""
    out = {}
    for name in c.SHORT:
        out[name + '/default'] = (name, {})
        label, params = [(k, v) for k, v in c.GOLDEN_VARIANTS[name].items() if k != 'default'][0]
        out[name + '/' + label] = (name, params)
        out[name + '/session'] = (name, SESSION)
    return out


def snapshot(monkeypatch, tmp_path, tool, params):
    body = c.post(monkeypatch, tmp_path, tool, params)
    assert body.get('result_status') == 'success', body
    body = without_exit_kinds(body)
    body.pop('report_path', None)
    return c.canonical(body)


if __name__ == '__main__':
    import tempfile
    sha = subprocess.check_output(['git', 'rev-parse', '--short=7', 'HEAD'], text=True).strip()
    assert sha == BASELINE_SHA, sha
    values = {}
    for key, case in cases().items():
        with pytest.MonkeyPatch.context() as mp, tempfile.TemporaryDirectory() as reports:
            values[key] = snapshot(mp, Path(reports), *case)
    Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).with_name('7p4_off_1d66844.json')).write_text(
        json.dumps(dict(baseline_sha=sha, cases=values), indent=2, ensure_ascii=False) + '\n')
    print('CAPTURED', len(values))
