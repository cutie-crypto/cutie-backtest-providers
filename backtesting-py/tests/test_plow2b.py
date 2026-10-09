"""P-LOW2b: template-stop sizing guards.

1. Templates that size against their own frozen stop (POSITION_SIZING_TEMPLATE_STOP_TOOLS) do not
   write broker _sizing_states; the exit side reads its own frozen stop. This end-to-end guard pins
   that the sizing record's initial_stop is the stop the exit actually fires on.
"""
from decimal import Decimal

import pandas as pd
import pytest

import _10b2a_cases as ca
import _10b2b_cases as cb
import _10b2c_cases as cc
import _10b2d_cases as cd
import cutie_backtesting_provider as p

RISK = dict(position_size_risk_pct=1, position_size_qty_step=0.001)
SHORT = {'macd_bearish_divergence', 'rsi_bearish_divergence', 'double_top', 'head_shoulders', 'chan_3sell'}
# Templates whose stop needs a user stop_loss_pct to exist at all.
EXTRA = {'red_streak_rsi': dict(stop_loss_pct=2)}
# Closes walk to the stop: legacy close-only stop decisions (risk layer off), and RSI divergences whose
# RSI exit would fire first on flat closes.
CLOSE_ONLY = {'red_streak_rsi', 'fibonacci_retracement', 'rsi_bullish_divergence', 'rsi_bearish_divergence'}
# vwap_reversion: its fixture fills on the last 6h bar of the UTC day, so utc_day_end expiry closes
# at the same next open as the stop and cannot discriminate; not covered here (reported as residual).
TEMPLATE_STOP_NAMES = sorted(t.rsplit('.', 1)[1] for t in p.POSITION_SIZING_TEMPLATE_STOP_TOOLS
                             if not t.endswith('.vwap_reversion'))


def _module(name):
    for module in (ca, cb, cc):
        if name in module.CASES:
            return module
    return None


def _frame(name):
    module = _module(name)
    if module is None:
        return cd.frame(name)
    entry = module.CASES[name]
    return (entry[0] if isinstance(entry, tuple) else entry)()


def _post(monkeypatch, tmp_path, name, data):
    module = _module(name) or cd
    return module.post(monkeypatch, tmp_path, name, {**RISK, **EXTRA.get(name, {})}, data=data)


def _stop_out_frame(data, fill_at, stop, short, close_only=False):
    """Keep every bar before the fill bar f and its open. Bar f closes EPS inside the frozen stop
    (Low/High and Close, so touch-based and close-only exits both see it) without crossing; bar f+1
    closes EPS beyond it; then flat. A nearer exit stop fires on bar f, a farther one never fires."""
    data = data.copy()
    f = data.index.get_loc(pd.Timestamp(fill_at, unit='s', tz=data.index.tz))
    data = data.iloc[:f + 4].copy()
    opening = float(data['Open'].iloc[f])
    eps = abs(opening - float(stop)) * 0.01
    near, far = (float(stop) - eps, float(stop) + eps) if short else (float(stop) + eps, float(stop) - eps)
    if close_only:
        path = [(opening, near, near), (near, far, far), (far, far, far), (far, far, far)]
    else:  # touch-based: closes stay at the fill open so no close-driven exit (MACD cross) fires first
        path = [(opening, near, opening), (opening, far, opening), (opening, opening, opening),
                (opening, opening, opening)]
    for i, (start, extreme, end) in zip(range(f, len(data)), path):
        row = dict(Open=start, High=max(start, extreme, end), Low=min(start, extreme, end), Close=end)
        for key, value in row.items():
            data.iloc[i, data.columns.get_loc(key)] = value
    return data, f


@pytest.mark.parametrize('name', TEMPLATE_STOP_NAMES)
def test_sizing_initial_stop_is_the_stop_the_exit_fires_on(name, monkeypatch, tmp_path):
    short = name in SHORT
    base = _post(monkeypatch, tmp_path, name, _frame(name))
    assert base['result_status'] == 'success', base.get('error_message')
    first = base['raw_report']['position_sizing']['fills'][0]
    stop = Decimal(first['initial_stop'])
    data, f = _stop_out_frame(_frame(name), first['at'], stop, short, name in CLOSE_ONLY)
    body = _post(monkeypatch, tmp_path, name, data)
    assert body['result_status'] == 'success', body.get('error_message')
    [record] = body['raw_report']['position_sizing']['fills']
    assert Decimal(record['initial_stop']) == stop
    [trade] = body['trades']
    # Decided on the crossing bar f+1; the queued market close fills at the open of f+2.
    closed = pd.Timestamp(trade['closed_at'], unit='s', tz=data.index.tz)
    assert closed == data.index[f + 2], trade
    assert Decimal(trade['exit_price']) == Decimal(str(data['Open'].iloc[f + 2])), trade
