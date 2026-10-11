"""10-B2d shared cases: HTTP bodies for ORB, Asia range, calendar schedule, red streak RSI,
US open momentum and CME weekend gap (fixtures reused from their own tests)."""
import json
import sys
from pathlib import Path

from backtesting import Backtest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p
import test_f1_range_breakout as f1
import test_f2_calendar as f2
import test_f3_us_open as f3
import test_f4_cme_gap as f4
import test_f6_streak_rsi as f6
from _exit_kinds_strip import without_exit_kinds

GOLDEN_DIR = Path(__file__).resolve().parent / 'golden_10b2d'
NAMES = ('opening_range_breakout', 'asia_range_breakout', 'calendar_schedule', 'red_streak_rsi',
         'us_open_momentum', 'cme_weekend_gap')

# name -> (timeframe, market, base params). Short ORB/Asia needs a futures market.
SETUP = {
    'opening_range_breakout': ('15m', 'futures', {}),
    'asia_range_breakout': ('15m', 'futures', {}),
    'calendar_schedule': ('1h', 'futures', dict(f2.SCHEDULE)),
    'red_streak_rsi': ('1h', 'spot', {}),
    'us_open_momentum': ('15m', 'futures', {}),
    'cme_weekend_gap': ('1h', 'futures', {}),
}


def frame(name, side='long'):
    if name == 'opening_range_breakout':
        return f1.market_frame(side=side)  # range 110/90; signal bar 4, fill bar 5 open 112 (short 88)
    if name == 'asia_range_breakout':
        return f1.market_frame(count=84, signal=28, side=side)  # signal 28, fill 29 open 112 (short 88)
    if name == 'calendar_schedule':
        return f2.frame()  # 02:00 event: signal close 100 (bar 1), fill bar 2 open
    if name == 'red_streak_rsi':
        return f6.frame()  # signal close 92 (bar 43), fill bar 44 open 93
    if name == 'us_open_momentum':
        return f3.frame()  # window 13:30-14:00 Low 98, fill 14:00 open 100
    return f4.frame()  # Friday close 100, Sunday 99 => long, fill open 99


# Off-mode goldens: defaults plus one non-default combination per template.
# label -> (params added to base params, data side)
GOLDEN_VARIANTS = {
    'opening_range_breakout': {'default': ({}, 'long'), 'short': (dict(direction='short'), 'short')},
    'asia_range_breakout': {'default': ({}, 'long'), 'short': (dict(direction='short'), 'short')},
    'calendar_schedule': {'default': ({}, 'long'), 'stop_off': (dict(calendar_stop_enabled=False), 'long')},
    'red_streak_rsi': {'default': ({}, 'long'), 'user_stop': (dict(stop_loss_pct=2), 'long')},
    'us_open_momentum': {'default': ({}, 'long'), 'user_stop': (dict(stop_loss_pct=1.5), 'long')},
    'cme_weekend_gap': {'default': ({}, 'long'), 'user_stop': (dict(stop_loss_pct=1), 'long')},
}


def post(monkeypatch, tmp_path, name, params=None, data=None, side='long', capital='10000', fee='0'):
    timeframe, market, base = SETUP[name]
    data = frame(name, side) if data is None else data
    step = int((data.index[1] - data.index[0]).total_seconds())
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a, **k: data.copy())
    monkeypatch.setattr(p, '_fetch_template_warmup', lambda *a, **k: data.iloc[:0].copy())
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **k: None)
    request = dict(run_id='10b2d', provider_tool_id='local.backtesting_py.' + name,
                   provider_params={**base, **(params or {})}, symbol='BTCUSDT', market=market,
                   timeframe=timeframe, start_at=int(data.index[0].timestamp()),
                   end_at=int(data.index[-1].timestamp()) + step, initial_capital=capital,
                   fee_bps=fee, slippage_bps='0')
    return TestClient(p.app).post('/cutie/backtest', json={'backtest': request}).json()


def canonical(body):
    return json.dumps(without_exit_kinds(body), sort_keys=True, separators=(',', ':'), ensure_ascii=False)
