"""Offline real HTTP/engine validator smoke; fixture OHLCV and symbol inventory, repository cwd required."""
from pathlib import Path
import json
import sys
root = Path.cwd()
sys.path[:0] = [str(root/'backtesting-py'), str(root/'backtesting-py/tests'), str(root/'validator')]
import cutie_backtesting_provider as provider
from test_f2_calendar import frame, SCHEDULE, TOOL
from backtesting import Backtest
from cutie_backtest_provider_validator.transport import _AsgiTransport
from cutie_backtest_provider_validator.validator import ProviderValidator, SmokeParams

provider.AUTH_TOKEN = 'f2-local-test-token'
provider.REPORTS_DIR = Path('/tmp/f2/validator-reports')
provider._supported_symbols = lambda: ['BTCUSDT']
provider._fetch_ohlcv = lambda *args: frame()
provider._fetch_template_warmup = lambda *args: (_ for _ in ()).throw(AssertionError('calendar warmup fetched'))
Backtest.plot = lambda *args, **kwargs: None
transport = _AsgiTransport(provider.app, 30)
smoke = SmokeParams(tool_id=TOOL, symbol='BTCUSDT', timeframe='1h', market='spot',
    start_at=1767225600, end_at=1767268800, provider_params=SCHEDULE, instruction='')
report = ProviderValidator(transport, 'http://127.0.0.1', provider.AUTH_TOKEN, smoke).run()
transport.close()
print(json.dumps(report.to_machine_json(), indent=2))
assert report.ok
assert report.tools_checked == len(provider.TOOL_SPECS) + int(provider._artifact_capability_pair() is not None)
assert all(check.passed for check in report.checks)
