"""10-B2c shared cases: HTTP bodies for the six candle templates, double bottom and inverse
head-and-shoulders (fixtures reused from their own tests)."""
import json
import sys
from pathlib import Path

from backtesting import Backtest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p
import test_9t1_engulf_pin as engulf_pin
import test_9t2_patterns as candles
import test_9t3_patterns as bottoms
from _exit_kinds_strip import without_exit_kinds

GOLDEN_DIR = Path(__file__).resolve().parent / 'golden_10b2c'
CANDLE = ('bullish_engulfing', 'hammer_pin_bar', 'morning_star', 'three_white_soldiers',
          'bullish_doji_reversal', 'inside_bar_breakout')
BOTTOM = ('double_bottom', 'inverse_head_shoulders')

# name -> data factory (signal bar 25 / fill bar 26 for candles; signal 53 / fill 54 for bottoms)
CASES = {
    **{n: (lambda n=n: engulf_pin.frame(n)) for n in ('bullish_engulfing', 'hammer_pin_bar')},
    **{n: (lambda n=n: candles.frame(n)) for n in candles.NAMES},
    **{n: (lambda n=n: bottoms.frame(n)) for n in BOTTOM},
}
# Off-mode goldens: defaults plus one non-default combination per template.
GOLDEN_VARIANTS = {
    'bullish_engulfing': {'default': {}, 'reward_r3': dict(reward_r=3)},
    'hammer_pin_bar': {'default': {}, 'no_position_filter': dict(position_filter=False)},
    'morning_star': {'default': {}, 'leverage2': dict(leverage=2)},
    'three_white_soldiers': {'default': {}, 'no_position_filter': dict(position_filter=False)},
    'bullish_doji_reversal': {'default': {}, 'reward_r3': dict(reward_r=3)},
    'inside_bar_breakout': {'default': {}, 'leverage3': dict(leverage=3)},
    'double_bottom': {'default': {}, 'leverage2': dict(leverage=2)},
    'inverse_head_shoulders': {'default': {}, 'leverage3': dict(leverage=3)},
}


def market_for(params):
    return 'futures' if (params or {}).get('leverage', 1) > 1 else 'spot'


def post(monkeypatch, tmp_path, name, params=None, data=None, capital='10000', fetch=None, fee='0'):
    data = CASES[name]() if data is None else data
    step = int((data.index[1] - data.index[0]).total_seconds())
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(p, '_fetch_ohlcv', fetch or (lambda *a, **k: data.copy()))
    monkeypatch.setattr(p, '_fetch_template_warmup', lambda *a, **k: data.iloc[:0].copy())
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **k: None)
    request = dict(run_id='10b2c', provider_tool_id='local.backtesting_py.' + name,
                   provider_params=dict(params or {}), symbol='BTCUSDT', market=market_for(params),
                   timeframe='1h', start_at=int(data.index[0].timestamp()),
                   end_at=int(data.index[-1].timestamp()) + step, initial_capital=capital,
                   fee_bps=fee, slippage_bps='0')
    return TestClient(p.app).post('/cutie/backtest', json={'backtest': request}).json()


def canonical(body):
    return json.dumps(without_exit_kinds(body), sort_keys=True, separators=(',', ':'), ensure_ascii=False)
