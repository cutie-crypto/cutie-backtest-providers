"""Offline HTTP validator with a real registered EMA engine and calendar gate."""
from pathlib import Path
import json
import sys
root = Path.cwd()
sys.path[:0] = [str(root/'backtesting-py'), str(root/'backtesting-py/tests'), str(root/'validator')]
import cutie_backtesting_provider as p
from test_f4_cme_gap import frame
from backtesting import Backtest
from cutie_backtest_provider_validator.transport import _AsgiTransport
from cutie_backtest_provider_validator.validator import ProviderValidator, SmokeParams

p.AUTH_TOKEN = 'calendar-local-test-token'
p.REPORTS_DIR = Path('/tmp/f4/validator-reports')
p._supported_symbols = lambda: ['BTCUSDT']
data = frame()
p._fetch_ohlcv = lambda *args: data.copy()
p._fetch_template_warmup = lambda *args: data.iloc[:0].copy()
Backtest.plot = lambda *args, **kwargs: None
transport = _AsgiTransport(p.app, 30)
smoke = SmokeParams(tool_id='local.backtesting_py.cme_weekend_gap', symbol='BTCUSDT', timeframe='1h', market='futures',
    start_at=1772755200, end_at=1773360000, provider_params={}, instruction='')
report = ProviderValidator(transport, 'http://127.0.0.1', p.AUTH_TOKEN, smoke).run()
transport.close()
print(json.dumps(report.to_machine_json(), indent=2))
assert report.ok
assert report.tools_checked == len(p.TOOL_SPECS) + int(p._artifact_capability_pair() is not None)
assert all(check.passed for check in report.checks)
