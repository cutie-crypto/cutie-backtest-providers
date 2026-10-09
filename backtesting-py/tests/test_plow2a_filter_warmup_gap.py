"""P-LOW2a: same-timeframe filter warmup continuity guard extended to every filter-capable template.

P-LOW1 made the F1/F2 (ORB / Asia / calendar) same-timeframe filter prefix strict. Here the other 43
templates whose build supports entry filters get the same rule, only while a same-timeframe filter is on:
- filter on + prefix with a missing bar -> INSUFFICIENT_DATA / filter_history_insufficient, no backtest run;
- filter on + contiguous prefix -> body byte-identical to b48e65e;
- filter off + prefix with a missing bar -> best-effort warmup, body byte-identical to b48e65e;
- filter_timeframe set -> the same-timeframe guard does not apply.
One template table (`capture_plow2a_b48e65e.tools()`), one assertion per parametrized function.
"""
import json
from pathlib import Path
import sys

import pandas as pd
import pytest
from backtesting import Backtest

sys.path.insert(0, str(Path(__file__).resolve().parent / 'fixtures'))
import capture_plow2a_b48e65e as cap  # noqa: E402
import test_risk_overlay_compatibility as compat  # noqa: E402

GOLDEN = json.loads((Path(__file__).parent / 'fixtures/plow2a_b48e65e.json').read_text())
TOOLS = cap.tools()


def test_template_table_is_the_43_filter_capable_templates():
    assert len(TOOLS) == 43
    assert set(GOLDEN['cases']) == {state + '/' + tool for tool in TOOLS for state in ('on', 'off')}


def _forbid_backtest(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError('backtest must not run when the filter warmup has a gap')
    monkeypatch.setattr(Backtest, 'run', forbidden)


def _served_gap(tool, bars):
    freq = cap.TIMEFRAME.get(tool, '1h')
    _, data = cap.series(freq)
    stamps = list(cap.prefix('gap', freq).tail(bars).index) + [data.index[0]]
    return any(b - a != pd.Timedelta(freq) for a, b in zip(stamps, stamps[1:]))


@pytest.mark.parametrize('tool', TOOLS)
def test_filter_on_gapped_warmup_fails_insufficient(tool, monkeypatch, tmp_path):
    _forbid_backtest(monkeypatch)
    body, calls = cap.post(monkeypatch, tmp_path, tool, {**cap.params_for(tool), **cap.EMA10}, 'gap')
    assert len(calls) == 1 and _served_gap(tool, calls[0])
    assert (body['result_status'], body['error_type'], body['error_message']) == (
        'failed', 'INSUFFICIENT_DATA', 'Entry filter indicator history has gaps'), body
    assert 'filter_history_insufficient' in json.dumps(body)


@pytest.mark.parametrize('tool', TOOLS)
def test_filter_on_contiguous_warmup_byte_identical_to_base(tool, monkeypatch, tmp_path):
    case = cap.cases()['on/' + tool]
    assert cap.snapshot(monkeypatch, tmp_path, *case) == GOLDEN['cases']['on/' + tool]


@pytest.mark.parametrize('tool', TOOLS)
def test_filter_off_gapped_warmup_byte_identical_to_base(tool, monkeypatch, tmp_path):
    case = cap.cases()['off/' + tool]
    assert 'filter_layer_enabled' not in case[1]
    expected = GOLDEN['cases']['off/' + tool]
    assert json.loads(expected)['result_status'] == 'success'
    assert cap.snapshot(monkeypatch, tmp_path, *case) == expected


def test_filter_timeframe_ignores_same_timeframe_warmup_gap(monkeypatch, tmp_path):
    # Multi-timeframe filter: filter_warmup is 0, so the template's own (gapped) warmup is best effort.
    full = compat.frame()
    source = full.resample('4h').agg(dict(Open='first', High='max', Low='min', Close='last', Volume='sum'))
    params = {**cap.params_for('ema_cross'), **cap.EMA10, 'filter_timeframe': '4h'}
    body, calls = cap.post(monkeypatch, tmp_path, 'ema_cross', params, 'gap', source)
    assert len(calls) == 1 and _served_gap('ema_cross', calls[0])
    assert body['result_status'] == 'success', body
    assert body['raw_report']['entry_filters']['timeframe'] == '4h'
