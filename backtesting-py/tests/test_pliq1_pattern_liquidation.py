"""P-LIQ1: K-line six, double bottom, inverse head-and-shoulders and red_streak_rsi judge isolated
liquidation bar by bar with T2-2b arbitration against the frozen stop (mirror of double_top).

Expected values are T2-2b answers written by hand from the scenario inputs (lev 10, long L = E * 9 / 10):
  A1  stop nearer than L, crossed intrabar, close back inside L      -> stop_loss, next bar open
  A2  as A1 but close beyond L: pct20 -> stop_loss, next bar open;
      full equity -> stop_loss at the crash close (backtesting 0.6.5 insolvency close, same as double_top)
  B1/B2  only L crossed (stop farther)                               -> liquidation at L on the crash bar
  G   crash bar opens beyond L                                       -> liquidation at that open (gap)
red_streak_rsi off the risk layer judges its stop on the close only, so any intrabar liquidation precedes
it (mixin legacy rule): every scenario liquidates on the crash bar and arbitration carries no stop.
"""
from decimal import Decimal
import json
import sys
from pathlib import Path

import pytest
import backtesting.backtesting as bb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p
import strategy_bottom_patterns
import strategy_pattern_template
import _pliq1_cases as q

GOLDEN = json.loads((Path(__file__).parent / 'fixtures/pliq1_fac1d60.json').read_text())

# name -> (frozen stop A, frozen stop B, entry A, entry B,
#          A1 / A2-pct20 exit = next open, A2-full exit = crash close, B exit = L_B, G exit = gap open)
EXPECTED = {
    **{n: ('96.903', '96.903', '104', '116', '104', '93.5', '104.4', '92.6') for n in q.c.CANDLE},
    'double_bottom': ('99.9', '99.9', '108', '115', '108', '97.1', '103.5', '96.2'),
    'inverse_head_shoulders': ('100.899', '100.899', '108', '119', '108', '97.1', '107.1', '96.2'),
    'red_streak_rsi': ('89.24', '78.2', '93', '93', '93', '83.6', '83.7', '82.7'),
}
# Close-only stops: liquidation precedes them whenever L is crossed intrabar (A and B share E 93, L 83.7).
CLOSE_ONLY = ('red_streak_rsi',)


def expected(name, scenario, sizing):
    """-> (exit reason, exit price, closed_at bar offset from the crash bar, liquidation count, gap)."""
    *_, next_open, crash_close, liquidation, gap_open = EXPECTED[name]
    if name in CLOSE_ONLY:
        return ('liquidation', gap_open, 0, 1, True) if scenario == 'G' else ('liquidation', liquidation, 0, 1, False)
    if scenario == 'A1' or (scenario == 'A2' and sizing == 'pct20'):
        return 'stop_loss', next_open, 1, 0, None
    if scenario == 'A2':
        return 'stop_loss', crash_close, 0, 0, None
    if scenario == 'G':
        return 'liquidation', gap_open, 0, 1, True
    return 'liquidation', liquidation, 0, 1, False


def run_spied(monkeypatch, tmp_path, name, scenario, sizing):
    calls, closes = [], []
    arbitrate, close = p._FixedRiskMixin._risk_isolated_exit, bb.Position.close

    def isolated_exit(self, stop=None):
        frames, frame = set(), sys._getframe(1)
        while frame is not None:
            frames.add(frame.f_code.co_name)
            frame = frame.f_back
        calls.append(dict(bar=len(self.data) - 1, stop=stop, bridge='process_before_insolvency' in frames))
        return arbitrate(self, stop)

    def position_close(self, portion=1.0):
        strategy = sys._getframe(1).f_locals['self']
        closes.append((len(strategy.data) - 1, strategy._risk_exit_reason))
        return close(self, portion)
    monkeypatch.setattr(p._FixedRiskMixin, '_risk_isolated_exit', isolated_exit)
    monkeypatch.setattr(bb.Position, 'close', position_close)
    body, data, k = q.post_scenario(monkeypatch, tmp_path, name, scenario, sizing)
    assert body['result_status'] == 'success', body
    return body, data, k, calls, closes


@pytest.mark.parametrize('sizing', q.SIZINGS)
@pytest.mark.parametrize('scenario', q.SCENARIOS)
@pytest.mark.parametrize('name', q.LONG)
def test_scenario_matrix_follows_t2_2b(monkeypatch, tmp_path, name, scenario, sizing):
    body, data, k, calls, closes = run_spied(monkeypatch, tmp_path, name, scenario, sizing)
    stop_a, stop_b, entry_a, entry_b = EXPECTED[name][:4]
    stop, entry = (stop_b, entry_b) if scenario.startswith('B') else (stop_a, entry_a)
    reason, price, offset, count, gap = expected(name, scenario, sizing)
    [trade] = body['trades']
    assert (q.bar_of(data, trade['opened_at']), trade['entry_price']) == (k - 1, entry)
    assert (trade['exit_price'], q.bar_of(data, trade['closed_at'])) == (price, k + offset)
    assert closes == [(k, reason)]
    report = body['raw_report']['isolated_risk']
    assert report['liquidation_count'] == count
    if count:
        [row] = report['liquidations']
        assert (row['fill_price'], row['liquidation_gap']) == (price, gap)
    # Every arbitration call carries the frozen stop; none falls back to the stop-less default path.
    # Close-only stops are the exception: they must arbitrate without the stop.
    assert calls and {call['stop'] for call in calls} == ({None} if name in CLOSE_ONLY else {Decimal(stop)})
    # Fill bar and crash bar are both arbitrated (the crash bar by the insolvency bridge when equity runs out).
    assert sorted({call['bar'] for call in calls}) == [k - 1, k]


@pytest.mark.parametrize('name', q.LONG)
def test_insolvency_bridge_passes_frozen_stop(monkeypatch, tmp_path, name):
    body, data, k, calls, closes = run_spied(monkeypatch, tmp_path, name, 'A2', 'full')
    bridge = [call for call in calls if call['bridge']]
    if name in CLOSE_ONLY:
        assert [(call['bar'], call['stop']) for call in bridge] == [(k, None)]
        assert closes == [(k, 'liquidation')]
        assert body['raw_report']['isolated_risk']['liquidation_count'] == 1
        return
    assert [(call['bar'], call['stop']) for call in bridge] == [(k, Decimal(EXPECTED[name][0]))]
    assert closes == [(k, 'stop_loss')]
    assert body['raw_report']['isolated_risk']['liquidation_count'] == 0


@pytest.mark.parametrize('key', sorted(q.control_cases()))
def test_control_group_bytes_match_fac1d60(monkeypatch, tmp_path, key):
    assert q.control_snapshot(monkeypatch, tmp_path, *q.control_cases()[key]) == GOLDEN['cases']['control/' + key]


@pytest.mark.parametrize('key', sorted(q.lev1_cases()))
def test_without_leverage_bytes_match_fac1d60(monkeypatch, tmp_path, key):
    assert q.lev1_snapshot(monkeypatch, tmp_path, *q.lev1_cases()[key]) == GOLDEN['cases']['lev1/' + key]


def test_golden_covers_every_case():
    assert GOLDEN['baseline_sha'] == 'fac1d60'
    assert sorted(GOLDEN['cases']) == sorted(['control/' + key for key in q.control_cases()]
                                             + ['lev1/' + key for key in q.lev1_cases()])
    assert len(GOLDEN['cases']) == 2 * 5 * 2 + 9 * 3


@pytest.mark.parametrize('name', q.c.CANDLE + q.c.BOTTOM)
def test_assumption_names_arbitration_only_with_leverage(monkeypatch, tmp_path, name):
    body, *_ = run_spied(monkeypatch, tmp_path, name, 'B1', 'pct20')
    text = body['assumptions']['pattern_execution']
    assert text.count('Isolated liquidation uses T2-2b gap/distance arbitration against the frozen stop') == 1


@pytest.mark.parametrize('sizing', q.SIZINGS)
@pytest.mark.parametrize('name', q.c.CANDLE + q.c.BOTTOM)
def test_signal_on_liquidation_bar_places_no_order(monkeypatch, tmp_path, name, sizing):
    data, k = q.scenario_frame(name, 'B1')
    signal_bar = q.SETUP[name]['fill'] - 1
    module, factory = ((strategy_bottom_patterns, 'make_bottom_strategy') if name in q.c.BOTTOM
                       else (strategy_pattern_template, 'make_pattern_strategy'))
    make, buys, buy = getattr(module, factory), [], bb.Strategy.buy

    def with_extra_signal(*args, **kwargs):
        class ExtraSignal(make(*args, **kwargs)):
            def init(self):
                super().init()
                # Repeat the original signal on the liquidation bar.
                at, source = self._warmup_bars + k, self._warmup_bars + signal_bar
                self._signals[at] = self._signals[source]
                if hasattr(self, '_anchors'):
                    self._anchors[at] = self._anchors[source]
        return ExtraSignal

    def spy_buy(self, *args, **kwargs):
        buys.append(len(self.data) - 1)
        return buy(self, *args, **kwargs)
    monkeypatch.setattr(module, factory, with_extra_signal)
    monkeypatch.setattr(bb.Strategy, 'buy', spy_buy)
    body = q.post(monkeypatch, tmp_path, name, q.scenario_params(name, 'B1', sizing), data, 'futures')
    assert body['result_status'] == 'success', body
    assert buys == [signal_bar]
    [trade] = body['trades']
    assert body['raw_report']['isolated_risk']['liquidation_count'] == 1
    assert q.bar_of(data, trade['closed_at']) == k
