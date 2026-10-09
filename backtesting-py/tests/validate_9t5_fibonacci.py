"""Real provider HTTP contract and filled trade; only market data/plot are local proxies."""
from pathlib import Path
import json
import sys
import tempfile
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'validator'))
sys.path.insert(0,str(ROOT/'backtesting-py'))
sys.path.insert(0,str(Path(__file__).parent))
import cutie_backtesting_provider as p
from test_9t5_fibonacci import frame, TOOL
from backtesting import Backtest
from cutie_backtest_provider_validator.transport import ValidatorTransport
from cutie_backtest_provider_validator.validator import ProviderValidator, SmokeParams


def main():
    data=frame()
    data.loc[data.index[9],'High']=121
    with tempfile.TemporaryDirectory(prefix='9t5-validator-') as reports, \
         patch.object(p,'AUTH_TOKEN','9t5-contract-token'), \
         patch.object(p,'REPORTS_DIR',Path(reports)), \
         patch.object(p,'_fetch_ohlcv',lambda *a:data.copy()), \
         patch.object(p,'_fetch_template_warmup',lambda *a:data.iloc[:0].copy()), \
         patch.object(Backtest,'plot',return_value=None):
        transport=ValidatorTransport.for_asgi('cutie_backtesting_provider','app',30)
        seen=[]
        original=transport.post_json
        def observe(*args,**kwargs):
            result=original(*args,**kwargs)
            seen.append(result.json_body)
            return result
        transport.post_json=observe
        try:
            report=ProviderValidator(transport,'http://provider.local','9t5-contract-token',
                SmokeParams(TOOL,'BTCUSDT','1h','spot',int(data.index[0].timestamp()),
                    int(data.index[-1].timestamp())+3600,dict(swing_n=2),'9T5 fixed causal proof')).run()
        finally:
            transport.close()
        print(json.dumps(report.to_machine_json(),indent=2))
        assert report.ok,report.errors
        assert seen[-1]['result_status']=='success',seen[-1]
        assert len(seen[-1]['trades'])==1
        print('9T5 VALIDATOR PASS: real catalog/auth/schema/ASGI HTTP/engine; fixed OHLCV proxy; 1 filled trade')


if __name__=='__main__': main()
