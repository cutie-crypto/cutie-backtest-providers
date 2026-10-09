"""Local ASGI validator smoke with fixed market data; never contacts an exchange."""
from pathlib import Path
import json
import sys
from unittest.mock import patch
import tempfile
ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT/'validator'),str(ROOT/'backtesting-py'),str(ROOT/'backtesting-py/tests')]
import cutie_backtesting_provider as p
from test_9t6_chan_3buy import frame
from backtesting import Backtest
from cutie_backtest_provider_validator.cli import run
f=frame()
with tempfile.TemporaryDirectory(prefix='9t6-validator-') as reports, \
     patch.object(p,'AUTH_TOKEN','local-dev-token'), \
     patch.object(p,'REPORTS_DIR',Path(reports)), \
     patch.object(p,'_fetch_ohlcv',lambda *a:f.copy()), \
     patch.object(p,'_fetch_template_warmup',lambda *a:f.iloc[:0].copy()), \
     patch.object(Backtest,'plot',return_value=None):
    code=run(['--app',str(ROOT/'backtesting-py/cutie_backtesting_provider.py'),
        '--token','local-dev-token','--tool-id','local.backtesting_py.chan_3buy',
        '--start-at',str(int(f.index[0].timestamp())),
        '--end-at',str(int(f.index[-1].timestamp())+3600),'--json'])
sys.exit(code)
