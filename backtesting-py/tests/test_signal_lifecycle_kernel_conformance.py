import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

"""Frozen observations captured from the pre-extraction production detector."""
import json
from dataclasses import asdict
from decimal import Decimal as D
from pathlib import Path

from signal_lifecycle_kernel import Candle, SignalLifecycleKernel


def test_pre_extraction_lifecycle_conformance():
    cases = json.loads((Path(__file__).parent / 'fixtures/signal_lifecycle_conformance.json').read_text())
    for case in cases:
        direction = case['direction']
        s = {'id':1,'signal_type':'crypto','direction':direction,'status':'active' if case['entered'] else 'published',
             'lifecycle_status':'entered' if case['entered'] else 'awaiting_entry',
             'created_at':case['created_at'],'entry_price':D(100),'create_snapshot_price':D(100),
             'target_prices':[D(110 if direction=='long' else 90)], 'stop_loss':D(90 if direction=='long' else 110),
             'entry_execution_mode':case['entry_mode'],'market_entry_pct':D(30),'expires_at':3000,
             'entry_hit_at':1000 if case['entered'] else None}
        c=Candle(1000,1299,D(case['low']),D(case['high']),D(case['low']),D(case['high']))
        actual=[asdict(e) for e in SignalLifecycleKernel.detect_candle_events(s,[c],now=1300)]
        assert json.loads(json.dumps(actual,default=str)) == case['events'], case
