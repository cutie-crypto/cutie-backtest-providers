"""Explicit one-time capture on the new feature head; tests never regenerate it."""
from pathlib import Path
from unittest.mock import patch
import json
import subprocess
import sys

sys.path[:0]=[str(Path(__file__).resolve().parents[2]),str(Path(__file__).resolve().parents[1])]
import cutie_backtesting_provider as p
from test_short_pat1 import NAMES, GOLDEN_PATH, compatibility_frame, off_response, fingerprint


def main():
    inputs={}
    for name in NAMES:
        data=compatibility_frame(name,signal=113,size=200)
        inputs[name]=dict(index=[str(t) for t in data.index],columns=data.to_dict(orient='list'))
    sha=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
    frozen=dict(source_sha=sha,baseline='new-template head; absent risk/time/leverage; time decorator bypassed',
                inputs=inputs,single={})
    GOLDEN_PATH.write_text(json.dumps(frozen,indent=2)+'\n')
    for name in NAMES:
        spec=p.TOOL_SPECS['local.backtesting_py.'+name]
        build=spec['build']
        assert hasattr(build,'__wrapped__')
        with patch.dict(spec,build=build.__wrapped__):
            single=dict(min_bars=spec['build']({})['min_bars'])
            for warm in (False,True):
                body=off_response(name,{},warm)
                assert len(body['trades'])==1
                assert 'isolated_margin' not in body['assumptions']
                assert 'isolated_risk' not in body['raw_report'] and 'time_layer' not in body['raw_report']
                single[str(int(warm))]=fingerprint(body)
        frozen['single'][name]=single
    GOLDEN_PATH.write_text(json.dumps(frozen,indent=2)+'\n')
    print('CAPTURED',sha,'2 tools x 2 warm states; each one trade')


if __name__=='__main__':
    main()
