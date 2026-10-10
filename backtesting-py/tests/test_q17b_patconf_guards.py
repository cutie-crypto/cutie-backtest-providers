"""Q17b: real detector -> confirmation -> broker fill regressions (pin 3 baseline)."""
import hashlib
import json
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest
from backtesting import Backtest

import test_patconf2b1_patterns as b1
import test_patconf2b2_more as b2
import test_event0_event_window as event

p = b1.p
PATTERNS = ('double_bottom', 'inverse_head_shoulders', 'double_top', 'head_shoulders')
LAYER = {**b1.LAYER, 'filter_pattern_confirm_bars': 1}


def pattern_case(monkeypatch, tmp_path, name, offset=0):
    data = b1.frame(name)
    s, tag = b1.signal_of(monkeypatch, tmp_path, name, data)
    short = name in b1.SHORT
    b1.set_close(data, s + 1, data['Low' if short else 'High'].iloc[s] + (-1 if short else 1))
    opening = tag.target + (-offset if short else offset)
    b1.set_bar(data, s + 2, opening, opening + .1, opening - .1, opening)
    return data, s, tag


@pytest.mark.parametrize('name', PATTERNS)
@pytest.mark.parametrize('offset', [0, 1, -1])
def test_frozen_target_boundary(name, offset, monkeypatch, tmp_path):
    data, s, tag = pattern_case(monkeypatch, tmp_path, name, offset)
    runs = b2.capture_strategy(monkeypatch)
    body = b1.post(monkeypatch, tmp_path, name, LAYER, data)
    report = getattr(runs[-1]['_strategy'], 'top_pattern_report' if name in b1.SHORT else 'bottom_pattern_report')
    skips = [r for r in report['skipped_entries'] if r['signal_bar'] == s]
    fills = [t for t in body['trades'] if b1.bar_of(data, t['opened_at']) == s + 2]
    if offset >= 0:
        assert not fills
        assert len(skips) == 1
        assert report['skipped_entry_count'] == len(report['skipped_entries'])
        assert skips[0] == dict(reason='entry_open_at_or_below_frozen_target' if name in b1.SHORT else
                               'entry_open_at_or_above_frozen_target', signal_bar=s, entry_bar=s + 2,
                               entry_open=float(data.Open.iloc[s + 2]), frozen_target=tag.target)
    else:
        assert len(fills) == 1 and not skips


@pytest.mark.parametrize('name', PATTERNS)
def test_spread_crosses_target_before_fill(name, monkeypatch, tmp_path):
    data, s, tag = pattern_case(monkeypatch, tmp_path, name, -.01)
    original = Backtest.__init__
    def with_spread(self, *args, **kwargs):
        kwargs['spread'] = .001
        original(self, *args, **kwargs)
    monkeypatch.setattr(Backtest, '__init__', with_spread)
    body = b1.post(monkeypatch, tmp_path, name, LAYER, data)
    assert all(b1.bar_of(data, t['opened_at']) != s + 2 for t in body['trades'])


@pytest.mark.parametrize('name', ['double_bottom', 'double_top'])
@pytest.mark.parametrize('size', [{'position_size_notional': 1000}, {'position_size_pct': 10}])
def test_delayed_fill_risk_uses_s_stop_without_distance_cap(name, size, monkeypatch, tmp_path):
    data = b1.frame(name)
    s, tag = b1.signal_of(monkeypatch, tmp_path, name, data)
    short = name in b1.SHORT
    b1.set_close(data, s + 1, data['Low' if short else 'High'].iloc[s] + (-1 if short else 1))
    opening = tag.target + (1 if short else -1)
    b1.set_bar(data, s + 2, opening, opening + .1, opening - .1, opening)
    runs = b2.capture_strategy(monkeypatch)
    body = b1.post(monkeypatch, tmp_path, name, {**LAYER, **size}, data)
    trade = runs[-1]['_trades'].query('EntryBar == @s + 2').iloc[0]
    assert trade.Tag == tag and trade.EntryPrice == opening
    distance = abs(Decimal(str(trade.EntryPrice)) - Decimal(str(trade.Tag.stop)))
    expected = abs(Decimal(str(opening)) - Decimal(str(tag.stop)))
    assert distance == expected == Decimal('19.1')
    row = next(t for t in body['trades'] if b1.bar_of(data, t['opened_at']) == s + 2)
    quantity = Decimal(row['qty'])
    assert float(quantity) == pytest.approx(1000 / opening, abs=0.001)
    assert distance > abs(Decimal(str(data.Open.iloc[s + 1])) - Decimal(str(tag.stop)))
    # The original units still bear the enlarged distance; no risk cap or stop recomputation.
    assert quantity * distance > quantity * abs(Decimal(str(data.Open.iloc[s + 1])) - Decimal(str(tag.stop)))
    risk = body['assumptions']['pattern_confirm']['frozen_stop_risk']
    assert risk['stop_anchor'] == 'signal_bar_s'
    assert risk['risk_distance'] == 'abs(actual_fill_price_at_k_plus_1_minus_frozen_stop_at_s)'
    assert risk['distance_cap'] is None
    assert 'position_size_notional' in risk['note'] and 'position_size_pct' in risk['note']


@pytest.mark.parametrize('name', ['red_streak_rsi', 'vwap_reversion'])
@pytest.mark.parametrize('offset', [0, 1, -1])
def test_reversion_frozen_target_cancels_gap_order(name, offset, monkeypatch, tmp_path):
    data = b2.frame(name)
    s, _ = b2.signal_of(monkeypatch, tmp_path, name, data, b2.NTH.get(name, 0))
    b1.set_close(data, s + 1, b2.level_of(name, data, s) + 1)
    params = {**LAYER, 'take_profit_pct': 5}
    target = Decimal(str(data.Close.iloc[s + 1])) * Decimal('1.05')
    opening = float(target) + offset
    b1.set_bar(data, s + 2, opening, opening + .1, opening - .1, opening)
    runs = b2.capture_strategy(monkeypatch)
    body = b2.post(monkeypatch, tmp_path, name, params, data)
    strategy = runs[-1]['_strategy']
    skips = getattr(strategy, '_f6_skips' if name == 'red_streak_rsi' else '_f5_skips')
    matches = [r for r in skips if r['execution_at'] == int(data.index[s + 2].timestamp())]
    fills = [t for t in body['trades'] if b1.bar_of(data, t['opened_at']) == s + 2]
    if offset >= 0:
        assert not fills and len(matches) == 1
        assert matches[0]['reason'] == 'entry_open_at_or_above_frozen_target'
        assert Decimal(matches[0]['frozen_target']) == target
    else:
        assert len(fills) == 1 and not matches


@pytest.mark.parametrize('offset', [0, 1, -1])
def test_fibonacci_existing_target_guard(offset, monkeypatch, tmp_path):
    name = 'fibonacci_retracement'
    data = b2.frame(name)
    b1.set_bar(data, 9, 109, 121, 106, 115)
    opening = 120 + offset
    b1.set_bar(data, 10, opening, opening + .1, opening - .1, opening)
    body = b2.post(monkeypatch, tmp_path, name, LAYER, data)
    fills = [t for t in body['trades'] if b1.bar_of(data, t['opened_at']) == 10]
    assert bool(fills) == (offset < 0)


@pytest.mark.parametrize('name', ['bullish_engulfing', 'three_black_crows', *b2.TAGGED])
def test_actual_fill_targets_remain_2r(name, monkeypatch, tmp_path):
    api = b2 if name in b2.TAGGED else b1
    data = api.frame(name)
    s, tag = api.signal_of(monkeypatch, tmp_path, name, data)
    short = name in api.SHORT
    b1.set_close(data, s + 1, data['Low' if short else 'High'].iloc[s] + (-1 if short else 1))
    # Gap across a hypothetical s-close 2R target: these targets must be computed at the fill.
    old_target = data.Close.iloc[s] + (data.Close.iloc[s] - tag.stop) * 2
    opening = old_target + (-1 if short else 1)
    b1.set_bar(data, s + 2, opening, opening + .1, opening - .1, opening)
    runs = b2.capture_strategy(monkeypatch)
    body = api.post(monkeypatch, tmp_path, name, LAYER, data)
    assert any(b1.bar_of(data, t['opened_at']) == s + 2 for t in body['trades'])
    if name in b2.TAGGED:
        report = body['raw_report']['chan' if name.startswith('chan') else 'divergence']
        entry = next(e for e in report['entries'] if e['entry_bar'] == s + 2)
        assert entry['frozen_target'] == pytest.approx(opening + 2 * (opening - tag.stop))


@pytest.mark.parametrize('warmup', [1, 30])
def test_event_window_rejects_engine_indicator_warmup(warmup, monkeypatch, tmp_path):
    original = p._FixedRiskMixin._risk_init
    def with_indicator(self):
        original(self)
        values = np.full(len(self.data), np.nan)
        values[min(warmup, len(values) - 1):] = 1
        self.future_indicator = self.I(lambda: values)
    monkeypatch.setattr(p._FixedRiskMixin, '_risk_init', with_indicator)
    body = event.post(monkeypatch, tmp_path, event.GOLDEN_PARAMS)
    assert body['result_status'] == 'failed'
    assert body['error_type'] == 'ENGINE_ERROR'
    assert 'event_window requires zero engine indicator warmup' in body['error_message']


OFF_NAMES = sorted(b1.WIRED_2B1 | set(b2.NINE))
OFF_GOLDEN = Path(__file__).parent / 'fixtures' / 'q17b_39bc346_off_sha256.json'


def off_digest(monkeypatch, tmp_path, name, explicit_false=False, gap=False):
    api = b2 if name in b2.NINE else b1
    data = api.frame(name)
    params = {'filter_pattern_confirm_enabled': False} if explicit_false else {}
    if gap:
        s, tag = api.signal_of(monkeypatch, tmp_path, name, data, b2.NTH.get(name, 0)) if api is b2 else api.signal_of(monkeypatch, tmp_path, name, data)
        if name in PATTERNS:
            opening = tag.target
        else:
            params['take_profit_pct'] = 5
            opening = data.Close.iloc[s] * 1.05
        b1.set_bar(data, s + 1, opening, opening + .1, opening - .1, opening)
    body = api.post(monkeypatch, tmp_path, name, params, data)
    body.pop('report_path', None)  # Only machine-local artifact path is excluded.
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


@pytest.mark.parametrize('name', OFF_NAMES)
@pytest.mark.parametrize('explicit_false', [False, True])
def test_off_response_bytes_match_pin3(name, explicit_false, monkeypatch, tmp_path):
    expected = json.loads(OFF_GOLDEN.read_text())
    assert off_digest(monkeypatch, tmp_path, name, explicit_false) == expected[name]


@pytest.mark.parametrize('name', [*PATTERNS, 'red_streak_rsi', 'vwap_reversion'])
def test_off_gap_response_bytes_match_pin3(name, monkeypatch, tmp_path):
    expected = json.loads(OFF_GOLDEN.read_text())
    assert off_digest(monkeypatch, tmp_path, name, True, gap=True) == expected[name + '.gap']


@pytest.mark.parametrize('name', ['fibonacci_retracement', 'red_streak_rsi', 'vwap_reversion'])
def test_untagged_effective_fill_crosses_target(name, monkeypatch, tmp_path):
    data = b2.frame(name)
    s, _ = b2.signal_of(monkeypatch, tmp_path, name, data, b2.NTH.get(name, 0))
    b1.set_close(data, s + 1, b2.level_of(name, data, s) + 1)
    params = dict(LAYER)
    if name == 'fibonacci_retracement':
        target = 120
    else:
        params['take_profit_pct'] = 5
        target = float(Decimal(str(data.Close.iloc[s + 1])) * Decimal('1.05'))
    opening = target - .01
    b1.set_bar(data, s + 2, opening, opening + .1, opening - .1, opening)
    original = Backtest.__init__
    def with_spread(self, *args, **kwargs):
        kwargs['spread'] = .001
        original(self, *args, **kwargs)
    monkeypatch.setattr(Backtest, '__init__', with_spread)
    runs = b2.capture_strategy(monkeypatch)
    body = b2.post(monkeypatch, tmp_path, name, params, data)
    assert all(b1.bar_of(data, t['opened_at']) != s + 2 for t in body['trades'])
    key = {'fibonacci_retracement': '_fib_skips', 'red_streak_rsi': '_f6_skips',
           'vwap_reversion': '_f5_skips'}[name]
    assert any(r['reason'] == 'entry_open_at_or_above_frozen_target'
               for r in getattr(runs[-1]['_strategy'], key))


@pytest.mark.parametrize('name', OFF_NAMES)
def test_assumptions_disclose_each_template_stop_anchor(name, monkeypatch, tmp_path):
    api = b2 if name in b2.NINE else b1
    body = api.post(monkeypatch, tmp_path, name, LAYER, api.frame(name))
    risk = body['assumptions']['pattern_confirm']['frozen_stop_risk']
    expected = ('confirmation_bar_k' if name in ('red_streak_rsi', 'vwap_reversion') else
                'signal_bar_s_wave_stop_or_confirmation_bar_k_user_stop' if name == 'fibonacci_retracement'
                else 'signal_bar_s')
    assert risk['stop_anchor'] == expected
    assert risk['distance_cap'] is None


def test_pi_double_bottom_target_overshoot_exact_prices(monkeypatch, tmp_path):
    # Original pi reproduction: s High122 is already beyond frozen target120;
    # k Close123 confirms, k+1 Open125 fills, k+2 Open100 exits for -1999.98.
    data = b1.frame('double_bottom')
    b1.set_bar(data, 53, 111, 122, 110, 121)
    b1.set_bar(data, 54, 121, 124, 120, 123)
    b1.set_bar(data, 55, 125, 126, 124, 125)
    b1.set_bar(data, 56, 100, 101, 99, 100)
    off = b1.post(monkeypatch, tmp_path, 'double_bottom', {}, data)
    assert [(t['entry_price'], t['exit_price'], t['pnl']) for t in off['trades']] == [
        ('121', '125', '330.576')]
    on = b1.post(monkeypatch, tmp_path, 'double_bottom', LAYER, data)
    assert on['trades'] == []
    report = on['raw_report']['bottom_pattern']
    assert report['skipped_entry_count'] == 1
    assert report['skipped_entries'] == [dict(reason='entry_open_at_or_above_frozen_target',
        signal_bar=53, entry_bar=55, entry_open=125., frozen_target=120.)]
