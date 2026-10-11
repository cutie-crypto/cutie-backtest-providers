"""10-B2a shared cases: HTTP bodies for the four templates (fixtures reused from their own tests)."""
import json
import sys
from pathlib import Path

from backtesting import Backtest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p
import test_9t4_divergence as div
import test_9t5_fibonacci as fib
import test_f5_vwap_reversion as vwap
from _exit_kinds_strip import without_exit_kinds

GOLDEN_DIR = Path(__file__).resolve().parent / 'golden_10b2a'


def fib_frame():
    data = fib.frame()
    data.loc[data.index[9], 'High'] = 121  # take-profit touch; signal8 close108, fill9 open109
    return data


def vwap_frame():
    data = vwap.frame()
    data.loc[data.index[3], 'Open'] = 95.  # signal2 close94, fill3 open95 (inside Low88..High100)
    return data


# name -> (data factory, base params, timeframe)
CASES = {
    'macd_bullish_divergence': (div.hand_frame, div.hand_params('macd_bullish_divergence'), '1h'),
    'rsi_bullish_divergence': (div.hand_frame, div.hand_params('rsi_bullish_divergence'), '1h'),
    'fibonacci_retracement': (fib_frame, dict(swing_n=2), '1h'),
    'vwap_reversion': (vwap_frame, {}, '6h'),
}
# Off-mode goldens: defaults plus one non-default combination per template.
GOLDEN_VARIANTS = {
    'macd_bullish_divergence': {'default': {}, 'compare_hist': dict(compare='hist')},
    'rsi_bullish_divergence': {'default': {}, 'leverage2': dict(leverage=2)},
    'fibonacci_retracement': {'default': {}, 'user_stop3': dict(stop_loss_pct=3)},
    'vwap_reversion': {'default': {}, 'user_stop3': dict(stop_loss_pct=3)},
}


def market_for(params):
    return 'futures' if (params or {}).get('leverage', 1) > 1 else 'spot'


def post(monkeypatch, tmp_path, name, params=None, data=None, capital='10000', fetch=None):
    factory, base, timeframe = CASES[name]
    data = factory() if data is None else data
    step = int((data.index[1] - data.index[0]).total_seconds())
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(p, '_fetch_ohlcv', fetch or (lambda *a: data.copy()))
    monkeypatch.setattr(p, '_fetch_template_warmup', lambda *a: data.iloc[:0].copy())
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **k: None)
    request = dict(run_id='10b2a', provider_tool_id='local.backtesting_py.' + name,
                   provider_params={**base, **(params or {})}, symbol='BTCUSDT', market=market_for(params),
                   timeframe=timeframe, start_at=int(data.index[0].timestamp()),
                   end_at=int(data.index[-1].timestamp()) + step, initial_capital=capital,
                   fee_bps='0', slippage_bps='0')
    return TestClient(p.app).post('/cutie/backtest', json={'backtest': request}).json()


def canonical(body):
    return json.dumps(without_exit_kinds(body), sort_keys=True, separators=(',', ':'), ensure_ascii=False)
