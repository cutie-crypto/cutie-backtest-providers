"""Run from provider root: python3 backtesting-py/tests/fixtures/capture_warmup_b_demo.py OUTPUT.json."""
import sys,json,math
from pathlib import Path
import pandas as pd
from fastapi.testclient import TestClient
from backtesting import Backtest
sys.path[:0]=[str(Path.cwd()/'backtesting-py'),str(Path.cwd()/'backtesting-py/tests')]
import cutie_backtesting_provider as p
from canonical_json import canonical_json_sha256
from test_ema_backtest_warmup import START,STEP,frame,fetcher,V2_KEYS
import test_risk_overlay_compatibility as compat
p.AUTH_TOKEN='';p.PROVIDER_REVISION='fixture-warmup-b';p.REPORTS_DIR=Path(__import__('tempfile').mkdtemp(prefix='warmup-b-demo-'));Backtest.plot=lambda *a,**kw:None
full=frame([100+.01*i+12*math.sin(i/17) for i in range(-6000,0)], [100+.01*i+12*math.sin(i/17) for i in range(500)])
p._fetch_ohlcv=fetcher(full)
cases={}
client=TestClient(p.app)
for name in ('ema_cross','ema_rsi_pullback','ema_trend_rsi','ema_pullback','ema_triple_alignment','bias_reversion','keltner_breakout','volume_breakout','macd','macd_above_zero','rsi_reversal','supertrend','adx_di_cross'):
 params={}
 body=client.post('/cutie/backtest',json={'backtest':dict(run_id='golden-'+name,provider_tool_id='local.backtesting_py.'+name,provider_params=params,symbol='BTCUSDT',market='spot',timeframe='1h',start_at=START,end_at=START+500*STEP,initial_capital='10000',fee_bps='10',slippage_bps='5')}).json()
 assert body['result_status']=='success',body
 cases[name]=dict(params=params,trade_count=len(body['trades']),canonical_result_hash=canonical_json_sha256({k:body[k] for k in V2_KEYS}),trades=body['trades'],indicator_warmup_bars=body['assumptions']['indicator_warmup_bars'])
 if 'warmup_manifest' in body['assumptions']:
  cases[name]['warmup_manifest']=body['assumptions']['warmup_manifest'];cases[name]['warmup_manifest_hash']=body['assumptions']['warmup_manifest_hash']
Path(sys.argv[1]).write_text(json.dumps(dict(schema_version='warmup-b-demo.v1',source_revision=p.PROVIDER_REVISION,cases=cases),indent=2,ensure_ascii=False)+'\n')
