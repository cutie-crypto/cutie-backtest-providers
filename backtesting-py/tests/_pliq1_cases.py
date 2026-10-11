"""P-LIQ1 shared cases: K-line six, double bottom, inverse head-and-shoulders and red_streak_rsi under
isolated leverage, plus the double_top / head_shoulders control group (fixtures reused from 10-B2b/c/d).

Scenario frames keep every bar up to the signal bar, then overwrite the fill bar (entry open E), the
crash bar k = fill + 1 and the flat tail. E is chosen per geometry so that the frozen stop S is either
nearer than the isolated liquidation price L (A / G) or farther (B):
  long  L = E * 9 / 10,  short L = E * 11 / 10   (lev 10, MMR 0, fees excluded; T2-2b).
"""
from decimal import Decimal
import sys
from pathlib import Path

import pandas as pd
from backtesting import Backtest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p
import _10b2b_cases as b
import _10b2c_cases as c
import _10b2d_cases as d
from _exit_kinds_strip import without_exit_kinds

LONG = c.CANDLE + c.BOTTOM + ('red_streak_rsi',)
CONTROL = ('double_top', 'head_shoulders')
SIZINGS = {'full': {}, 'pct20': {'position_size_pct': 20}}
SCENARIOS = ('A1', 'A2', 'B1', 'B2', 'G')
LEVERAGE = 10


def base_frame(name):
    if name in c.CASES:
        return c.CASES[name]()
    if name == 'red_streak_rsi':
        return d.frame(name)
    return b.CASES[name][0]()


# name -> fill bar, frozen stop per geometry (read off the fixture comments in _10b2c_cases / test_f6 /
# test_short_pat1: anchor Low * 0.999, F6 signal close 92 * (1 -+ stop pct)), entry open E per geometry,
# and params per geometry. B on red_streak_rsi widens the user stop to 15% (stop 78.2 below L 83.7).
SETUP = {
    **{n: dict(fill=26, side='long', A=('96.903', 104, {}), B=('96.903', 116, {})) for n in c.CANDLE},
    'double_bottom': dict(fill=54, side='long', A=('99.9', 108, {}), B=('99.9', 115, {})),
    'inverse_head_shoulders': dict(fill=54, side='long', A=('100.899', 108, {}), B=('100.899', 119, {})),
    'red_streak_rsi': dict(fill=44, side='long', A=('89.24', 93, {}),
                           B=('78.2', 93, dict(stop_loss_pct=15, take_profit_pct=15))),
    'double_top': dict(fill=54, side='short', A=('100.1', 95, {}), B=('100.1', 89, {})),
    'head_shoulders': dict(fill=54, side='short', A=('99.099', 94, {}), B=('99.099', 81, {})),
}


def liquidation_price(entry, side):
    return Decimal(entry) * Decimal(9 if side == 'long' else 11) / Decimal(10)


def geometry(scenario):
    return 'B' if scenario.startswith('B') else 'A'


def scenario_frame(name, scenario):
    """Rows after the signal bar replaced; returns (frame, crash bar k)."""
    setup = SETUP[name]
    stop, entry, _ = setup[geometry(scenario)]
    side, fill = setup['side'], setup['fill']
    E, S = float(entry), float(stop)
    L = float(liquidation_price(entry, side))
    sign = 1 if side == 'long' else -1

    def px(x):
        return round(x, 6)

    def bar(o, through, back, close):
        # through = adverse extreme (Low for long, High for short); back = favourable extreme.
        high, low = (back, through) if side == 'long' else (through, back)
        return [px(o), px(high), px(low), px(close)]

    crash = {
        'A1': bar(E, L - sign * .1, E + sign * .2, (L + S) / 2),
        'A2': bar(E, L - sign * .2, E + sign * .2, L - sign * .1),
        'B1': bar(E, L - sign * .1, E + sign * .2, L + sign * .5),
        'B2': bar(E, L - sign * .2, E + sign * .2, L - sign * .1),
        'G': bar(L - sign * 1, L - sign * 1.2, L - sign * .5, L - sign * .8),
    }[scenario]
    data = base_frame(name)
    k = fill + 1
    # Fixture column order differs (F6 builds Open, Close, High, Low); address OHLC by name.
    ohlc = [data.columns.get_loc(column) for column in ('Open', 'High', 'Low', 'Close')]
    data.iloc[fill, ohlc] = bar(E, E - sign * .2, E + sign * .2, E)
    data.iloc[k, ohlc] = crash
    data.iloc[k + 1:, ohlc] = bar(E, E - sign * .2, E + sign * .2, E)
    return data, k


def post(monkeypatch, tmp_path, name, params, data, market):
    step = int((data.index[1] - data.index[0]).total_seconds())
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a, **k: data.copy())
    monkeypatch.setattr(p, '_fetch_template_warmup', lambda *a, **k: data.iloc[:0].copy())
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **k: None)
    request = dict(run_id='pliq1', provider_tool_id='local.backtesting_py.' + name,
                   provider_params=dict(params), symbol='BTCUSDT', market=market, timeframe='1h',
                   start_at=int(data.index[0].timestamp()), end_at=int(data.index[-1].timestamp()) + step,
                   initial_capital='1000', fee_bps='0', slippage_bps='0')
    return TestClient(p.app).post('/cutie/backtest', json={'backtest': request}).json()


def scenario_params(name, scenario, sizing):
    return dict(leverage=LEVERAGE, **SETUP[name][geometry(scenario)][2], **SIZINGS[sizing])


def post_scenario(monkeypatch, tmp_path, name, scenario, sizing):
    data, k = scenario_frame(name, scenario)
    return post(monkeypatch, tmp_path, name, scenario_params(name, scenario, sizing), data, 'futures'), data, k


# Off-state (lev=1) cases: spot, futures and position sizing on, all on the unmodified fixture frame.
LEV1 = {'spot': ({}, 'spot'), 'futures': ({}, 'futures'), 'sizing': (dict(position_size_risk_pct=1), 'spot')}


def lev1_cases():
    return {f'{name}/{label}': (name, label) for name in LONG for label in LEV1}


def control_cases():
    return {f'{name}/{scenario}/{sizing}': (name, scenario, sizing)
            for name in CONTROL for scenario in SCENARIOS for sizing in SIZINGS}


def snapshot(body):
    assert body.get('result_status') == 'success', body
    body = without_exit_kinds(body)
    body.pop('report_path', None)
    return c.canonical(body)


def lev1_snapshot(monkeypatch, tmp_path, name, label):
    params, market = LEV1[label]
    return snapshot(post(monkeypatch, tmp_path, name, params, base_frame(name), market))


def control_snapshot(monkeypatch, tmp_path, name, scenario, sizing):
    return snapshot(post_scenario(monkeypatch, tmp_path, name, scenario, sizing)[0])


def bar_of(data, ts):
    return int(data.index.get_loc(pd.Timestamp(ts, unit='s')))
