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
SHORT = {'macd_bearish_divergence', 'rsi_bearish_divergence', 'double_top', 'head_shoulders', 'chan_3sell',
         'bearish_engulfing', 'shooting_star', 'evening_star'}  # SHORT-PAT-4
# Templates whose stop needs a user stop_loss_pct to exist at all.
EXTRA = {'red_streak_rsi': dict(stop_loss_pct=2)}
# Closes walk to the stop: legacy close-only stop decisions (risk layer off), and RSI divergences whose
# RSI exit would fire first on flat closes.
# vwap_reversion walks closes too: a close back at VWAP would exit first.
CLOSE_ONLY = {'red_streak_rsi', 'fibonacci_retracement', 'rsi_bullish_divergence', 'rsi_bearish_divergence',
              'vwap_reversion'}
TEMPLATE_STOP_NAMES = sorted(t.rsplit('.', 1)[1] for t in p.POSITION_SIZING_TEMPLATE_STOP_TOOLS)


def _vwap_early_frame():
    """The golden vwap fixture (6h) fills on the UTC day's last bar, where utc_day_end expiry closes at the
    same next open as the stop; with 6h bars the earliest possible fill (12:00) still exits at the next
    00:00. Hourly bars instead: flat at 100 until the 20:00 signal (close 90 <= VWAP ~99.7 * 0.985), so
    the fill is 21:00 (f), the stop exits at the 23:00 open (f+2) one bar before the day-end flatten
    (next 00:00), and no re-entry follows (23:00 is the day's last bar, 00:00 the frame's last bar)."""
    rows = [(100, 100, 100, 100)] * 20 + [(100, 100, 90, 90)] + [(91, 92, 90, 91)] * 4
    return pd.DataFrame([dict(Open=o, High=h, Low=lo, Close=c, Volume=1) for o, h, lo, c in rows],
                        index=pd.date_range('2026-01-01', periods=len(rows), freq='1h', tz='UTC')).astype(float)


def _module(name):
    for module in (ca, cb, cc):
        if name in module.CASES:
            return module
    return None


def _frame(name):
    if name == 'vwap_reversion':
        return _vwap_early_frame()
    if name in p._SHORT_CANDLE_TOOL_NAMES.values():
        import test_short_pat4_candles as pat4
        return pat4.frame(name)
    module = _module(name)
    if module is None:
        return cd.frame(name)
    entry = module.CASES[name]
    return (entry[0] if isinstance(entry, tuple) else entry)()


def _post(monkeypatch, tmp_path, name, data):
    if name in p._SHORT_CANDLE_TOOL_NAMES.values():  # SHORT-PAT-4: futures-only, posted through its own suite
        import test_short_pat4_candles as pat4
        return pat4.post(monkeypatch, tmp_path, name, RISK, data)[0]
    module = _module(name) or cd
    if name == 'vwap_reversion':  # hourly frame, see _vwap_early_frame
        monkeypatch.setitem(ca.CASES, name, (_vwap_early_frame, {}, '1h'))
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


# 2. Self-placed template entries carry _sizing_signal_bar (the no_next_open guard reads it).
SIGNAL_BAR_FAMILIES = {  # one tool per template module that places its own entry order
    'strategy_divergence': 'macd_bullish_divergence',
    'strategy_top_patterns': 'double_top',
    'strategy_bottom_patterns': 'double_bottom',
    'strategy_pattern_template': 'bullish_engulfing',
    'strategy_chan': 'chan_3buy',
}


@pytest.mark.parametrize('module_name', sorted(SIGNAL_BAR_FAMILIES))
def test_sized_template_entry_order_records_its_signal_bar(module_name, monkeypatch, tmp_path):
    from backtesting import Strategy
    name = SIGNAL_BAR_FAMILIES[module_name]
    placed = []
    for side in ('buy', 'sell'):
        original = getattr(Strategy, side)

        def spy(self, *args, _original=original, **kwargs):
            order = _original(self, *args, **kwargs)
            if type(self).__module__ == module_name:
                placed.append((order, len(self.data) - 1, kwargs.get('tag')))
            return order
        monkeypatch.setattr(Strategy, side, spy)
    body = _post(monkeypatch, tmp_path, name, _frame(name))
    assert body['result_status'] == 'success', body.get('error_message')
    assert body['raw_report']['position_sizing']['fills'], 'fixture must open a sized trade'
    assert placed, 'template placed no entry order'
    for order, bar, tag in placed:
        assert tag is not None and tag.signal_bar == bar
        assert getattr(order, '_sizing_signal_bar', None) == bar


# 3. The filter decorator's direction default must match the template's own direction default,
# otherwise an enabled filter silently masks with the opposite side.
FILTER_ON = dict(filter_layer_enabled=True, filter_ema_enabled=True)


def test_filter_default_direction_matches_template_direction():
    filtered = {key.rsplit('.', 1)[1]: spec for key, spec in p.TOOL_SPECS.items()
                if getattr(spec.get('build'), '_supports_entry_filters', False)}
    assert len(filtered) > 20
    schema_checked, built_checked, mismatches = [], [], []
    for name, spec in sorted(filtered.items()):
        decorated = spec['build']._filter_default_direction
        field = spec['param_schema_properties'].get('direction')
        if field is not None and field.get('default') == 'both':
            continue  # filters already reject both
        # Schema-declared default where there is one; otherwise the template's fixed side.
        expected = field['default'] if field is not None else ('short' if name in SHORT else 'long')
        (schema_checked if field is not None else built_checked).append(name)
        if name in SHORT and expected != 'short':
            mismatches.append((name, 'short template', expected))
        strategy = spec['build'](dict(FILTER_ON, **EXTRA.get(name, {})))['strategy']
        if (decorated, strategy._filter_direction) != (expected, expected):
            mismatches.append((name, expected, decorated, strategy._filter_direction))
    assert mismatches == []
    # The five short templates declare direction=short in their schema, so they land in the schema branch.
    assert SHORT <= set(schema_checked)
    assert len(schema_checked) + len(built_checked) > 20 and built_checked
