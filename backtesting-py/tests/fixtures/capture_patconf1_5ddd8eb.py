"""Capture once at 5ddd8eb (before P-PATCONF-1); tests never regenerate this immutable baseline.

P-PATCONF-1 adds the pattern-confirmation filter engine and its two FILTER keys without wiring any
template. Baseline proof that every template supporting entry filters (runtime enumeration, 51 at
5ddd8eb) is untouched while the new keys are omitted:
- `off/<tool>`: filter layer off;
- `on/<tool>`: same-timeframe EMA(10) filter on (the three P-LOW1 range/calendar tools excluded: their
  frames carry no warmup prefix, see capture_plow2a_b48e65e.py);
- `traded/<tool>`: filter off on each template's own trading frame (10-B2a..d cases, mirrored short
  pattern frames of test_short_pat4_candles.py), so the pattern templates that never fire on the shared
  frame still pin at least one trade.
Frames and params reuse capture_plow2a_b48e65e.py (contiguous prefix) and, for the three P-LOW1
tools, tests/_10b2d_cases.py. Each case is the sorted-key JSON of the full HTTP body (report_path
dropped). Run from an export of 5ddd8eb: `python3 tests/fixtures/capture_patconf1_5ddd8eb.py <out.json>`.
"""
from pathlib import Path
import json
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import _10b2a_cases as cases_a  # noqa: E402
import _10b2b_cases as cases_b  # noqa: E402
import _10b2c_cases as cases_c  # noqa: E402
import _10b2d_cases as range_cases  # noqa: E402
import capture_plow2a_b48e65e as cap  # noqa: E402
import cutie_backtesting_provider as p  # noqa: E402
import test_short_pat4_candles as short_pat4  # noqa: E402

BASELINE_SHA = '5ddd8eb'
TRADED = {**{name: cases_a for name in ('fibonacci_retracement', 'macd_bullish_divergence', 'rsi_bullish_divergence',
                                        'vwap_reversion')},
          **{name: cases_b for name in ('chan_3buy', 'chan_3sell', 'double_top', 'head_shoulders',
                                        'macd_bearish_divergence', 'rsi_bearish_divergence')},
          **{name: cases_c for name in ('bullish_doji_reversal', 'bullish_engulfing', 'double_bottom', 'hammer_pin_bar',
                                        'inside_bar_breakout', 'inverse_head_shoulders', 'morning_star',
                                        'three_white_soldiers')},
          **{name: range_cases for name in range_cases.NAMES},
          **{name: short_pat4 for name in short_pat4.SHORT}}


def filter_tools():
    return sorted(k.removeprefix('local.backtesting_py.') for k, v in p.TOOL_SPECS.items()
                  if getattr(v.get('build'), '_supports_entry_filters', False))


def cases():
    """case -> (tool, params, state); state picks the frame helper."""
    out = {}
    for tool in filter_tools():
        if tool in cap.PLOW1_TOOLS:
            out['off/' + tool] = (tool, {}, 'range')
            continue
        out['off/' + tool] = (tool, cap.params_for(tool), 'contiguous')
        out['on/' + tool] = (tool, {**cap.params_for(tool), **cap.EMA10}, 'contiguous')
    for tool in TRADED:
        out['traded/' + tool] = (tool, {}, 'traded')
    return out


def snapshot(monkeypatch, tmp_path, tool, params, state):
    if state == 'range':
        body = range_cases.post(monkeypatch, tmp_path, tool, params)
    elif state == 'traded':
        body = TRADED[tool].post(monkeypatch, tmp_path, tool, params)
        body = body[0] if TRADED[tool] is short_pat4 else body
    else:
        body, _ = cap.post(monkeypatch, tmp_path, tool, params, state)
    body.pop('report_path', None)
    return json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


if __name__ == '__main__':
    import tempfile
    sha = subprocess.check_output(['git', 'rev-parse', '--short=7', 'HEAD'], text=True).strip()
    assert sha == BASELINE_SHA, sha
    values = {}
    for key, case in cases().items():
        with pytest.MonkeyPatch.context() as mp, tempfile.TemporaryDirectory() as reports:
            values[key] = snapshot(mp, Path(reports), *case)
    Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).with_name('patconf1_5ddd8eb.json')).write_text(
        json.dumps(dict(baseline_sha=sha, cases=values), indent=2, ensure_ascii=False) + '\n')
    print('CAPTURED', len(values))
