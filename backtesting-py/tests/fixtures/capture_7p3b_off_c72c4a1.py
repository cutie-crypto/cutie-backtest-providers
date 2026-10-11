"""Capture once at c72c4a1 (before 7-P3b wiring); tests never regenerate this immutable baseline.

Off-state proof for red_streak_rsi: requests without any filter_* key must stay byte-identical
to main. Each case is the sorted-key JSON of the full HTTP body (floats keep repr precision).
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
from _exit_kinds_strip import without_exit_kinds

TOOL = 'local.backtesting_py.red_streak_rsi'
BASELINE_SHA = 'c72c4a1'


def frame():
    """Same hand frame as test_f6_streak_rsi: four red closes at 40..43, signal 43, entry 44."""
    close = [100.] * 40 + [98., 96., 94., 92.] + [92.] * 26
    opens = close.copy()
    for i in range(40, 44):
        opens[i] = close[i] + 1
    opens[44] = 93.
    return pd.DataFrame(dict(Open=opens, Close=close,
        High=[max(o, c) + .1 for o, c in zip(opens, close)],
        Low=[min(o, c) - .1 for o, c in zip(opens, close)], Volume=[1] * 70),
        index=pd.date_range('2026-01-01', periods=70, freq='h'))


def wavy():
    """Repeating 10-bar cycle: 3 green rises, 4 red falls, 3 flat dojis."""
    closes, opens = [], []
    price = 100.
    for i in range(120):
        phase = i % 10
        if phase < 3:
            opens.append(price); price += 2; closes.append(price)
        elif phase < 7:
            opens.append(price + 1); price -= 1.5; closes.append(price)
        else:
            opens.append(price); closes.append(price)
    return pd.DataFrame(dict(Open=opens, Close=closes,
        High=[max(o, c) + .5 for o, c in zip(opens, closes)],
        Low=[min(o, c) - .5 for o, c in zip(opens, closes)], Volume=[1] * 120),
        index=pd.date_range('2026-01-01', periods=120, freq='h'))


def cases():
    return {
        'default/hand': ({}, 'hand', 0),
        'default/hand_warm20': ({}, 'hand', 20),
        'stop_target/hand': ({'stop_loss_pct': 2, 'take_profit_pct': 4}, 'hand', 0),
        'red3_rsi2/wavy': ({'red_bars': 3, 'rsi_period': 2, 'oversold': 49}, 'wavy', 0),
        'time_gate/wavy': ({'red_bars': 3, 'rsi_period': 2, 'oversold': 49, 'time_layer_enabled': True,
                            'time_session_start': '02:00', 'time_session_end': '20:00'}, 'wavy', 0),
        'risk_layer/wavy': ({'red_bars': 3, 'rsi_period': 2, 'oversold': 49, 'risk_layer_enabled': True,
                             'stop_loss_pct': 3, 'take_profit_pct': 5}, 'wavy', 20),
    }


def snapshot(params, data_name, warm):
    full = frame() if data_name == 'hand' else wavy()
    prefix, data = full.iloc[:warm], full.iloc[warm:]
    with ExitStack() as stack, tempfile.TemporaryDirectory() as reports:
        stack.enter_context(patch.object(provider, 'AUTH_TOKEN', ''))
        stack.enter_context(patch.object(provider, 'REPORTS_DIR', Path(reports)))
        stack.enter_context(patch.object(provider, '_fetch_ohlcv', lambda *a: data))
        stack.enter_context(patch.object(provider, '_fetch_template_warmup', lambda *a: prefix))
        stack.enter_context(patch.object(Backtest, 'plot', lambda *a, **kw: None))
        request = dict(run_id='7p3b_off', provider_tool_id=TOOL, provider_params=params,
            symbol='BTCUSDT', market='spot', timeframe='1h', start_at=int(data.index[0].timestamp()),
            end_at=int(data.index[-1].timestamp()) + 3600, initial_capital='10000', fee_bps='10', slippage_bps='0')
        body = TestClient(provider.app).post('/cutie/backtest', json={'backtest': request}).json()
    body = without_exit_kinds(body)
    body.pop('report_path', None)
    return json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


if __name__ == '__main__':
    sha = subprocess.check_output(['git', 'rev-parse', '--short=7', 'HEAD'], text=True).strip()
    assert sha == BASELINE_SHA, sha
    values = {key: snapshot(*case) for key, case in cases().items()}
    Path(__file__).with_name('7p3b_off_c72c4a1.json').write_text(
        json.dumps(dict(baseline_sha=sha, cases=values), indent=2, ensure_ascii=False) + '\n')
    print('CAPTURED', len(values))
