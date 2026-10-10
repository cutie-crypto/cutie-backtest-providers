"""Q17b second review: stop anchors, per-template skip schema, confirmation-only evidence."""
from decimal import Decimal

import pytest
from backtesting import Backtest

import test_q17b_patconf_guards as q

b1, b2, p = q.b1, q.b2, q.p


@pytest.mark.parametrize('name', ['vwap_reversion', 'red_streak_rsi', 'fibonacci_retracement'])
@pytest.mark.parametrize('gap', [1, 1.1], ids=['flat', 'gap'])
def test_confirmation_stop_near_signal_close_has_no_cap(name, gap, monkeypatch, tmp_path):
    data = b2.frame(name)
    s, _ = b2.signal_of(monkeypatch, tmp_path, name, data, b2.NTH.get(name, 0))
    close_k = b2.level_of(name, data, s) + 1
    b1.set_close(data, s + 1, close_k)
    stop_pct = (1 - data.Close.iloc[s] / close_k) * 100 + 1e-8
    opening = close_k * gap
    b1.set_bar(data, s + 2, opening, opening + .1, opening - .1, opening)
    frozen = []
    advance = p._FixedRiskMixin._pattern_confirm_advance
    def capture_stop(self):
        advance(self)
        if len(self.data) - 1 == s + 1 and self.orders:
            frozen.append(self._sizing_template_stop(self.orders[0]))
            assert not hasattr(self.orders[0], '_pattern_confirm_risk')
    monkeypatch.setattr(p._FixedRiskMixin, '_pattern_confirm_advance', capture_stop)
    body = b2.post(monkeypatch, tmp_path, name, {**q.LAYER, 'stop_loss_pct': stop_pct}, data)
    fills = [t for t in body['trades'] if b1.bar_of(data, t['opened_at']) == s + 2]
    assert len(fills) == 1
    assert float(fills[0]['entry_price']) == pytest.approx(opening)
    stop = Decimal(str(close_k)) * (1 - Decimal(str(stop_pct)) / 100)
    assert len(frozen) == 1
    assert abs(frozen[0] - stop) < Decimal('1e-12')
    assert float(stop) == pytest.approx(data.Close.iloc[s], abs=1e-6)
    report = body['assumptions']['pattern_confirm']
    assert not report['skipped_entries']
    assert report['frozen_stop_risk']['distance_cap'] is None
    assert report['frozen_stop_risk']['signal_entry_reference'] is None
    assert '只适用于止损冻结在信号根 s' in report['frozen_stop_risk']['note']
    assert '确认根 k' in report['frozen_stop_risk']['note']


REPORTS = {**{n: 'bottom_pattern' for n in q.PATTERNS[:2]},
           **{n: 'top_pattern' for n in q.PATTERNS[2:]},
           'bullish_engulfing': 'candle_pattern', 'three_black_crows': 'candle_pattern',
           **{n: 'chan' if n.startswith('chan') else 'divergence' for n in b2.TAGGED}}
CAP_FIELDS = {'reason', 'signal_bar', 'entry_bar', 'entry_open', 'adjusted_open',
              'signal_entry_price', 'frozen_stop', 'signal_risk_distance',
              'entry_risk_distance', 'risk_distance_cap_multiple', 'risk_distance_multiple'}


@pytest.mark.parametrize('name', REPORTS)
def test_signal_stop_cap_preserves_report_schema(name, monkeypatch, tmp_path):
    from dataclasses import replace
    api = b2 if name in b2.TAGGED else b1
    data = api.frame(name)
    s, tag = api.signal_of(monkeypatch, tmp_path, name, data)
    short = name in api.SHORT
    direction = -1 if short else 1
    # Keep targets untouched and choose a narrow s stop so the cap is reached first.
    reference = float(data.Close.iloc[s])
    stop = tag.stop if name in b2.TAGGED else reference - direction * .1
    opening = stop + 3 * (reference - stop)
    b1.set_close(data, s + 1, data['Low' if short else 'High'].iloc[s] + direction)
    b1.set_bar(data, s + 2, opening, opening + .1, opening - .1, opening)
    register = p.PatternConfirmQueue.register
    def freeze(self, bar, side, level, payload=None):
        if bar == s and payload is not None and name not in b2.TAGGED:
            payload = replace(payload, stop=stop)
        register(self, bar, side, level, payload)
    monkeypatch.setattr(p.PatternConfirmQueue, 'register', freeze)
    body = api.post(monkeypatch, tmp_path, name, q.LAYER, data)
    report = body['raw_report'][REPORTS[name]]
    skip = next(r for r in report['skipped_entries'] if r['signal_bar'] == s)
    assert skip['reason'] == 'confirm_risk_distance_exceeds_cap'
    assert CAP_FIELDS <= skip.keys()
    assert report['skipped_entry_count'] == len(report['skipped_entries'])
    assert skip in body['assumptions']['pattern_confirm']['skipped_entries']
    assert all(b1.bar_of(data, t['opened_at']) != s + 2 for t in body['trades'])


@pytest.mark.parametrize('warmup', [0, 3])
def test_fib_wave_cap_report_has_absolute_indices(warmup, monkeypatch, tmp_path):
    import test_9t5_fibonacci as fib
    data = b2.frame('fibonacci_retracement')
    b1.set_bar(data, 9, 109, 121, 106, 115)
    b1.set_bar(data, 10, 119, 119.1, 118.9, 119)
    main, prefix = data.iloc[warmup:].copy(), data.iloc[:warmup].copy()
    runs = b2.capture_strategy(monkeypatch)
    body = fib.response(monkeypatch, tmp_path, {**q.LAYER, 'swing_n': 2}, main, warm=prefix)
    assert body['result_status'] == 'success', body
    report = body['raw_report']['fibonacci_retracement']
    skip = next(r for r in report['skipped_entries'] if r['reason'] == 'confirm_risk_distance_exceeds_cap')
    assert CAP_FIELDS | {'signal_index', 'execution_index', 'frozen_target'} <= skip.keys()
    strategy = runs[-1]['_strategy']
    assert strategy._warmup_bars == warmup
    # Existing fib schema calls its decision bar (k under confirmation) signal_index.
    assert skip['signal_index'] == strategy._fib_signal_index == 9
    assert skip['execution_index'] == 10 == warmup + skip['entry_bar']
    assert skip['signal_bar'] == 8 - warmup
    assert strategy._fib_order is None
    assert all(isinstance(skip[key], str) for key in ('entry_open', 'adjusted_open', 'frozen_stop', 'frozen_target'))


@pytest.mark.parametrize('name', b2.TAGGED)
@pytest.mark.parametrize('enabled', [False, True], ids=['off', 'on'])
def test_stop_guard_adjusted_price_only_added_with_confirmation(name, enabled, monkeypatch, tmp_path):
    data = b2.frame(name)
    s, tag = b2.signal_of(monkeypatch, tmp_path, name, data)
    short = name in b2.SHORT
    direction = -1 if short else 1
    if enabled:
        b1.set_close(data, s + 1, data['Low' if short else 'High'].iloc[s] + direction)
    entry_bar = s + (2 if enabled else 1)
    opening = tag.stop - direction
    b1.set_bar(data, entry_bar, opening, opening + .1, opening - .1, opening)
    original = Backtest.__init__
    def with_spread(self, *args, **kwargs):
        kwargs['spread'] = .001
        original(self, *args, **kwargs)
    monkeypatch.setattr(Backtest, '__init__', with_spread)
    body = b2.post(monkeypatch, tmp_path, name, q.LAYER if enabled else {}, data)
    report = body['raw_report']['chan' if name.startswith('chan') else 'divergence']
    skip = next(r for r in report['skipped_entries'] if r['signal_bar'] == s)
    assert skip['reason'] == ('entry_open_at_or_above_frozen_stop' if short else 'entry_open_at_or_below_frozen_stop')
    assert skip['entry_open'] == opening
    assert all(b1.bar_of(data, t['opened_at']) != entry_bar for t in body['trades'])
    if enabled:
        assert skip['adjusted_open'] == pytest.approx(opening * (1 + direction * .001))
        assert report['skipped_entry_count'] == len(report['skipped_entries'])
    else:
        assert 'adjusted_open' not in skip
        assert 'skipped_entry_count' not in report
