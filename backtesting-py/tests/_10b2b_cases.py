"""10-B2b shared cases: HTTP bodies for the five short templates and chan_3buy (fixtures reused from their own tests)."""
import json
import sys
from pathlib import Path

from backtesting import Backtest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p
import test_9t6_chan_3buy as chan_buy
import test_short_pat1 as top
import test_short_pat2 as bear
import test_short_pat3_chan_3sell as chan_sell

GOLDEN_DIR = Path(__file__).resolve().parent / 'golden_10b2b'
SHORT = ('macd_bearish_divergence', 'rsi_bearish_divergence', 'double_top', 'head_shoulders', 'chan_3sell')

# name -> (data factory, base params, timeframe)
CASES = {
    'macd_bearish_divergence': (bear.hand_frame, bear.hand_params('macd_bearish_divergence'), '1h'),
    'rsi_bearish_divergence': (bear.hand_frame, bear.hand_params('rsi_bearish_divergence'), '1h'),
    'double_top': (lambda: top.frame('double_top'), {}, '1h'),
    'head_shoulders': (lambda: top.frame('head_shoulders'), {}, '1h'),
    'chan_3sell': (chan_sell.frame, {}, '1h'),
    'chan_3buy': (chan_buy.frame, {}, '1h'),
}
# Off-mode goldens: defaults plus one non-default combination per template.
GOLDEN_VARIANTS = {
    'macd_bearish_divergence': {'default': {}, 'compare_hist': dict(compare='hist')},
    'rsi_bearish_divergence': {'default': {}, 'leverage2': dict(leverage=2)},
    'double_top': {'default': {}, 'leverage3': dict(leverage=3)},
    'head_shoulders': {'default': {}, 'leverage2': dict(leverage=2)},
    'chan_3sell': {'default': {}, 'leverage3': dict(leverage=3)},
    'chan_3buy': {'default': {}, 'bi_old': dict(bi_mode='old')},
}


def market_for(name, params):
    return 'futures' if name in SHORT or (params or {}).get('leverage', 1) > 1 else 'spot'


def post(monkeypatch, tmp_path, name, params=None, data=None, capital='10000', fetch=None, fee='0'):
    factory, base, timeframe = CASES[name]
    data = factory() if data is None else data
    step = int((data.index[1] - data.index[0]).total_seconds())
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(p, '_fetch_ohlcv', fetch or (lambda *a, **k: data.copy()))
    monkeypatch.setattr(p, '_fetch_template_warmup', lambda *a, **k: data.iloc[:0].copy())
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **k: None)
    request = dict(run_id='10b2b', provider_tool_id='local.backtesting_py.' + name,
                   provider_params={**base, **(params or {})}, symbol='BTCUSDT', market=market_for(name, params),
                   timeframe=timeframe, start_at=int(data.index[0].timestamp()),
                   end_at=int(data.index[-1].timestamp()) + step, initial_capital=capital,
                   fee_bps=fee, slippage_bps='0')
    return TestClient(p.app).post('/cutie/backtest', json={'backtest': request}).json()


def canonical(body):
    return json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
