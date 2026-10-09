"""Capture once at c72c4a1 (before 7-P3b2 wiring); tests never regenerate this immutable baseline.

Off-state proof for double_bottom / inverse_head_shoulders / opening_range_breakout /
asia_range_breakout / calendar_schedule: requests without any filter_* key must stay
byte-identical to main. Each case is the sorted-key JSON of the full HTTP body.
F1/F2 never fetch indicator warmup when the filter is off; the capture fails loudly if they do.
"""
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
import json
import subprocess
import sys
import tempfile

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as provider
from backtesting import Backtest
from fastapi.testclient import TestClient

BASELINE_SHA = 'c72c4a1'
STEP = {'1h': 3600, '15m': 900}


def bottom_frame(name, signal=53, size=120):
    """Same hand frame as test_9t3_patterns.compatibility_frame: breakout close at 53, entry 54."""
    data = pd.DataFrame(dict(Open=[103.]*size, High=[104.]*size, Low=[102.]*size,
                             Close=[103.]*size, Volume=[1.]*size),
                        index=pd.date_range('2026-01-01', periods=size, freq='h'))
    if name == 'double_bottom':
        data.iloc[signal-25, 2] = 100
        data.iloc[signal-7, 2] = 100
        data.iloc[signal-18, 1] = 110
        opening = 111
    else:
        data.iloc[signal-37, 2] = 100
        data.iloc[signal-22, 2] = 95
        data.iloc[signal-7, 2] = 101
        data.iloc[signal-30, 1] = 110
        data.iloc[signal-15, 1] = 114
        opening = 119
    data.iloc[signal:, :4] = [opening, opening+1, opening-1, opening]
    data.iloc[signal+4, 1] = 150
    return data


def range_frame(count=16, signal=4):
    """Same as test_f1_range_breakout.market_frame (long): range 90..110, breakout close at `signal`."""
    rows = [[100., 110., 90., 100., 1.] for _ in range(count)]
    for i in range(signal, count):
        rows[i] = [112., 113., 111., 112., 1.]
    rows[signal] = [100., 112., 100., 111., 1.]
    return pd.DataFrame(rows, columns=['Open', 'High', 'Low', 'Close', 'Volume'],
                        index=pd.date_range('2026-01-01T00:00', periods=count, freq='15min'))


def asia_frame():
    data = range_frame(count=84, signal=28)
    data.iloc[30] = [112, 143, 111, 142, 1]
    data.iloc[31] = [144, 145, 143, 144, 1]
    return data


def calendar_frame():
    data = pd.DataFrame(dict(Open=[100.]*12, High=[101.]*12, Low=[99.]*12, Close=[100.]*12, Volume=[1.]*12),
                        index=pd.date_range('2026-01-01', periods=12, freq='h'))
    data.iloc[1] = [100, 103, 99, 102, 1]
    data.iloc[2] = [104, 105, 100, 103, 1]
    data.iloc[4] = [106, 107, 105, 106, 1]
    return data


FRAMES = {
    'double_bottom': lambda: bottom_frame('double_bottom'),
    'inverse_head_shoulders': lambda: bottom_frame('inverse_head_shoulders'),
    'orb': range_frame,
    'asia': asia_frame,
    'calendar': calendar_frame,
}
SCHEDULE = dict(time_entry_at='02:00', time_max_holding_minutes=120)
SESSION = dict(time_layer_enabled=True, time_session_start='08:00', time_session_end='10:00')


def cases():
    """case -> (tool, params, frame, warm, market, timeframe)."""
    return {
        'double_bottom/default': ('double_bottom', {}, 'double_bottom', 0, 'spot', '1h'),
        'double_bottom/warm40': ('double_bottom', {}, 'double_bottom', 40, 'spot', '1h'),
        'double_bottom/session': ('double_bottom', SESSION, 'double_bottom', 0, 'spot', '1h'),
        'inverse_head_shoulders/default': ('inverse_head_shoulders', {}, 'inverse_head_shoulders', 0, 'spot', '1h'),
        'inverse_head_shoulders/warm40': ('inverse_head_shoulders', {}, 'inverse_head_shoulders', 40, 'spot', '1h'),
        'opening_range_breakout/default': ('opening_range_breakout', {}, 'orb', 0, 'futures', '15m'),
        'opening_range_breakout/both': ('opening_range_breakout', {'direction': 'both'}, 'orb', 0, 'futures', '15m'),
        'asia_range_breakout/default': ('asia_range_breakout', {}, 'asia', 0, 'futures', '15m'),
        'calendar_schedule/default': ('calendar_schedule', SCHEDULE, 'calendar', 0, 'spot', '1h'),
        'calendar_schedule/first_bar': ('calendar_schedule', dict(time_entry_at='01:00', time_max_holding_minutes=60),
                                        'calendar', 0, 'spot', '1h'),
        'calendar_schedule/entry_gate': ('calendar_schedule', dict(time_entry_at='01:00', time_max_holding_minutes=60,
                                         time_layer_enabled=True, time_session_start='02:00', time_session_end='03:00'),
                                         'calendar', 0, 'spot', '1h'),
    }


def snapshot(tool, params, frame_name, warm, market, timeframe):
    full = FRAMES[frame_name]()
    prefix, data = full.iloc[:warm], full.iloc[warm:]

    def warmup(*args):
        if tool in ('opening_range_breakout', 'asia_range_breakout', 'calendar_schedule'):
            raise AssertionError('F1/F2 off-state must not fetch indicator warmup')
        return prefix

    with ExitStack() as stack, tempfile.TemporaryDirectory() as reports:
        stack.enter_context(patch.object(provider, 'AUTH_TOKEN', ''))
        stack.enter_context(patch.object(provider, 'REPORTS_DIR', Path(reports)))
        stack.enter_context(patch.object(provider, '_fetch_ohlcv', lambda *a: data.copy()))
        stack.enter_context(patch.object(provider, '_fetch_template_warmup', warmup))
        stack.enter_context(patch.object(Backtest, 'plot', lambda *a, **kw: None))
        request = dict(run_id='7p3b2_off', provider_tool_id='local.backtesting_py.' + tool, provider_params=params,
            symbol='BTCUSDT', market=market, timeframe=timeframe, start_at=int(data.index[0].timestamp()),
            end_at=int(data.index[-1].timestamp()) + STEP[timeframe], initial_capital='10000', fee_bps='10',
            slippage_bps='0')
        body = TestClient(provider.app).post('/cutie/backtest', json={'backtest': request}).json()
    assert body.get('result_status') == 'success', body
    body.pop('report_path', None)
    return json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


if __name__ == '__main__':
    sha = subprocess.check_output(['git', 'rev-parse', '--short=7', 'HEAD'], text=True).strip()
    assert sha == BASELINE_SHA, sha
    values = {key: snapshot(*case) for key, case in cases().items()}
    Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).with_name('7p3b2_off_c72c4a1.json')).write_text(
        json.dumps(dict(baseline_sha=sha, cases=values), indent=2, ensure_ascii=False) + '\n')
    print('CAPTURED', len(values))
