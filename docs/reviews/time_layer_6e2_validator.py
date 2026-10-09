"""Offline HTTP validator with a real registered EMA engine and calendar gate."""
from pathlib import Path
import json
import sys
root = Path.cwd()
sys.path[:0] = [str(root/'backtesting-py'), str(root/'backtesting-py/tests'), str(root/'validator')]
import cutie_backtesting_provider as p
from test_time_layer_6e2 import frame
from backtesting import Backtest
from cutie_backtest_provider_validator.transport import _AsgiTransport
from cutie_backtest_provider_validator.validator import ProviderValidator, SmokeParams

p.AUTH_TOKEN = '6e2-local-test-token'
p.REPORTS_DIR = Path('/tmp/6e2/validator-reports')
p._supported_symbols = lambda: ['BTCUSDT']
data = frame('2026-01-08T13:00', 64)
data['Close'] = [100. if i % 8 < 4 else 90. for i in range(len(data))]
data['Open'], data['High'], data['Low'] = data['Close'], data['Close']+1, data['Close']-1
p._fetch_ohlcv = lambda *args: data.copy()
p._fetch_template_warmup = lambda *args: frame('2026-01-08T01:00', 48)
Backtest.plot = lambda *args, **kwargs: None
transport = _AsgiTransport(p.app, 30)
smoke = SmokeParams(tool_id='local.backtesting_py.ema_cross', symbol='BTCUSDT', timeframe='15m', market='spot',
    start_at=1767877200, end_at=1767934800,
    provider_params=dict(ema_fast=2, ema_slow=3, time_layer_enabled=True,
        time_timezone='America/New_York', time_calendar='us_equity_regular'), instruction='')
report = ProviderValidator(transport, 'http://127.0.0.1', p.AUTH_TOKEN, smoke).run()
transport.close()
print(json.dumps(report.to_machine_json(), indent=2))
assert report.ok
assert report.tools_checked == len(p.TOOL_SPECS) + int(p._artifact_capability_pair() is not None)
assert all(check.passed for check in report.checks)
