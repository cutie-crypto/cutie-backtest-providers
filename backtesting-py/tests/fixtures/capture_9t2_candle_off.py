"""Explicit capture for new tools with the time decorator bypassed.

Run manually once after template implementation. Tests only read this fixture.
There is no historical pre-template trade baseline: these are new tool ids.
"""
from pathlib import Path
from unittest.mock import patch
import hashlib
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as provider
import capture_time_layer_11e8cfb as capture
from canonical_json import canonical_json
from test_9t2_patterns import compatibility_frame, NAMES


def main():
    path = Path(__file__).with_name('9t2_candle_off.json')
    inputs = {}
    for name in NAMES:
        data = compatibility_frame(name, signal=89, size=180)
        inputs[name] = dict(index=[str(t) for t in data.index], columns=data.to_dict(orient='list'))
    frozen = dict(baseline='new-template time decorator bypassed; risk/time keys absent', inputs=inputs, single={})
    path.write_text(json.dumps(frozen, indent=2)+'\n')
    for name in NAMES:
        tool = provider.TOOL_SPECS['local.backtesting_py.'+name]
        original = tool['build']
        assert hasattr(original, '__wrapped__'), 'capture requires the real decorator'
        with patch.dict(tool, build=original.__wrapped__):
            single = dict(min_bars=tool['build']({})['min_bars'])
            assert tool['build']({})['strategy']._time_config is None
            for warm in (False, True):
                body = capture.response(name, {}, warm)
                assert len(body['trades']) == 1
                assert 'time_layer' not in body['assumptions']
                v2 = {key: body[key] for key in capture.V2_KEYS}
                single[str(int(warm))] = dict(warmup_bars=body['assumptions']['indicator_warmup_bars'],
                    assumptions_sha256=capture.digest(body['assumptions']), raw_report_sha256=capture.digest(body['raw_report']),
                    trade_count=len(body['trades']), trades_sha256=capture.digest(body['trades']),
                    equity_sha256=capture.digest(body['equity_curve']),
                    result_v2_sha256=hashlib.sha256(canonical_json(v2).encode()).hexdigest())
        frozen['single'][name] = single
    path.write_text(json.dumps(frozen, indent=2)+'\n')
    print('CAPTURED 4 templates x absent-time/risk keys x warm/no-warm; each one trade')


if __name__ == '__main__':
    main()
