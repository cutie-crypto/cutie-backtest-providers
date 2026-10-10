"""SHORT-PAT-4: bearish_engulfing / shooting_star / evening_star; SHORT-PAT-5: three_black_crows /
bearish_doji_reversal. Futures short mirrors of the long candle templates (bullish_engulfing /
hammer_pin_bar / morning_star / three_white_soldiers / bullish_doji_reversal).

Fixtures are the 10-B2c long candle frames reflected around 200 (P -> 200 - P, High <-> Low), so every
geometric relation flips: one short signal at bar 25, entry at bar 26 open 94 (long 106; star, crows and doji 90 vs 110).
Frozen stop = mirrored anchor High 103 * 1.001 = 103.103 (long 97 * 0.999 = 96.903);
target = entry - (stop - entry) * reward_r.

Isolated scenarios (lev 10, short L = E * 11 / 10, T2-2b, hand-written from the inputs):
  A  E 96, L 105.6, stop 103.103 nearer:  A1 / A2-pct20 -> stop_loss at next open 96;
                                          A2-full -> stop_loss at the crash close L + 0.1 = 105.7
  B  E 90, L 99, stop farther:            B1 / B2 -> liquidation at L 99 on the crash bar
  G  crash bar opens at L_A + 1 = 106.6   -> liquidation at that open (gap)
"""
from decimal import Decimal
import json
import sys
from pathlib import Path

import pytest
import backtesting.backtesting as bb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p
import _10b2c_cases as c
import _pliq1_cases as q

MIRROR = {'bearish_engulfing': 'bullish_engulfing', 'shooting_star': 'hammer_pin_bar', 'evening_star': 'morning_star',
          # SHORT-PAT-5
          'three_black_crows': 'three_white_soldiers', 'bearish_doji_reversal': 'bullish_doji_reversal'}
SHORT = tuple(MIRROR)
STOP = '103.103'
ENTRY = {'bearish_engulfing': '94', 'shooting_star': '94', 'evening_star': '90',
         'three_black_crows': '90', 'bearish_doji_reversal': '90'}
# Unexited trades settle at the last close (mirror of the long fixtures' 100 / 110).
FINAL = {'bearish_engulfing': '100', 'shooting_star': '100', 'evening_star': '90',
         'three_black_crows': '90', 'bearish_doji_reversal': '90'}


def reflect(data):
    data = data.copy()
    high, low = data['High'].copy(), data['Low'].copy()
    data['Open'], data['Close'] = 200 - data['Open'], 200 - data['Close']
    data['High'], data['Low'] = 200 - low, 200 - high
    return data


def frame(name):
    return reflect(c.CASES[MIRROR[name]]())


def compatibility_frame(name):
    """Mirror of the long template's compatibility frame, for other suites' per-template loops."""
    if MIRROR[name] in ('morning_star', 'three_white_soldiers', 'bullish_doji_reversal'):
        from test_9t2_patterns import compatibility_frame as long_frame
    else:
        from test_9t1_engulf_pin import compatibility_frame as long_frame
    return reflect(long_frame(MIRROR[name]))


def post(monkeypatch, tmp_path, name, params=None, data=None, market='futures'):
    data = frame(name) if data is None else data
    body = q.post(monkeypatch, tmp_path, name, params or {}, data, market)
    return body, data


def trades(body, data):
    return [(q.bar_of(data, t['opened_at']), t['side'], t['entry_price'], q.bar_of(data, t['closed_at']), t['exit_price'])
            for t in body['trades']]


def set_bar(data, i, o, h, low, close):
    data.iloc[i, [data.columns.get_loc(k) for k in ('Open', 'High', 'Low', 'Close')]] = [o, h, low, close]


# --- catalog / validation -------------------------------------------------------------------------

def test_catalog_lists_three_futures_short_tools_within_one_mib(monkeypatch):
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    from fastapi.testclient import TestClient
    response = TestClient(p.app).get('/catalog')
    tools = {t['tool_id']: t for t in response.json()['tools']}
    assert len(tools) == 59 and len(response.content) <= 1 << 20
    for name in SHORT:
        tool = tools['local.backtesting_py.' + name]
        props = tool['param_schema']['properties']
        assert tool['markets'] == ['futures']
        assert props['direction'] == {'type': 'string', 'default': 'short', 'enum': ['short']}
        assert ('position_filter' in props) is (name != 'evening_star')
        assert {'leverage', 'filter_layer_enabled', 'position_size_risk_pct'} <= set(props)


@pytest.mark.parametrize('name', SHORT)
def test_spot_rejected_before_fetch(monkeypatch, tmp_path, name):
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a, **k: pytest.fail('spot must not fetch'))
    from fastapi.testclient import TestClient
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    data = frame(name)
    request = dict(run_id='spat4', provider_tool_id='local.backtesting_py.' + name, provider_params={},
                   symbol='BTCUSDT', market='spot', timeframe='1h', start_at=int(data.index[0].timestamp()),
                   end_at=int(data.index[-1].timestamp()) + 3600, initial_capital='1000', fee_bps='0', slippage_bps='0')
    body = TestClient(p.app).post('/cutie/backtest', json={'backtest': request}).json()
    assert (body['error_type'], body['error_message']) == ('INVALID_PARAMS', 'this template requires futures market')


@pytest.mark.parametrize('params', [{'direction': 'long'}, {'stop_loss_pct': 2}, {'take_profit_pct': 3},
                                    {'take_profit_r': 2}, {'trailing_stop_pct': 1}, {'atr_stop_multiplier': 2}])
@pytest.mark.parametrize('name', SHORT)
def test_direction_and_pattern_exit_keys_rejected(name, params):
    with pytest.raises(ValueError, match='^INVALID_PARAMS:'):
        p.TOOL_SPECS['local.backtesting_py.' + name]['build'](params)


def test_evening_star_has_no_position_filter():
    with pytest.raises(ValueError, match='^INVALID_PARAMS:'):
        p.TOOL_SPECS['local.backtesting_py.evening_star']['build']({'position_filter': False})


# --- execution --------------------------------------------------------------------------------------

@pytest.mark.parametrize('name', ['bearish_engulfing', 'shooting_star'])
@pytest.mark.parametrize('enabled', [True, False])
def test_position_filter_switch(monkeypatch, tmp_path, name, enabled):
    # Mirror of test_9t1_engulf_pin's background=90 frame: prior highs near 110.5, BB/EMA stay above the
    # pattern's [95, 103], so only the bare shape fires.
    from test_9t1_engulf_pin import frame as long_frame
    body, data = post(monkeypatch, tmp_path, name, dict(position_filter=enabled),
                      reflect(long_frame(MIRROR[name], background=90)))
    assert body['result_status'] == 'success', body
    assert [t[:3] for t in trades(body, data)] == ([] if enabled else [(26, 'short', '94')])


@pytest.mark.parametrize('name', SHORT)
def test_filter_layer_wired_as_short_and_off_state_unchanged(monkeypatch, tmp_path, name):
    build = p.TOOL_SPECS['local.backtesting_py.' + name]['build']
    assert getattr(build, '_supports_entry_filters', False)
    cls = build({'filter_layer_enabled': True, 'filter_ema_enabled': True})['strategy']
    assert cls._filter_direction == 'short' and cls._filter_config.ema_enabled
    base = post(monkeypatch, tmp_path, name)[0]
    off = post(monkeypatch, tmp_path, name, dict(filter_layer_enabled=False))[0]
    assert off['trades'] == base['trades'] and off['metrics'] == base['metrics']


@pytest.mark.parametrize('name', SHORT)
@pytest.mark.parametrize('period', [2, 30])
def test_filter_decided_at_signal_bar_on_mirrored_7p3a_bars(monkeypatch, tmp_path, name, period):
    # 7-P3a's hand bars reflected around 200. Reflection maps "close > EMA" (long allowed) onto
    # "close < EMA" (short allowed), so the long EMA oracle on the unreflected closes is the short mask.
    import test_7p3a_kline_filter as f7
    params, _, k, entry_open = f7.CASES[MIRROR[name]]
    mask = f7.ema_mask(list(f7.frame(MIRROR[name]).Close), period)
    if period == 2:  # allowed at the signal bar only: a decision one bar late would block it
        assert mask[k] and not mask[k + 1]
    else:  # EMA30 still near the reflected 80 plateau: short blocked at the signal bar
        assert not mask[k]
    body, data = post(monkeypatch, tmp_path, name, {**params, **f7.FILTER_ON, 'filter_ema_period': period},
                      reflect(f7.frame(MIRROR[name])))
    assert body['result_status'] == 'success', body
    expected = [(k + 1, 'short', str(Decimal(str(200 - entry_open))))] if period == 2 else []
    assert [t[:3] for t in trades(body, data)] == expected


@pytest.mark.parametrize('name', SHORT)
def test_mirror_of_long_fixture_trades_one_short(monkeypatch, tmp_path, name):
    body, data = post(monkeypatch, tmp_path, name)
    assert body['result_status'] == 'success', body
    assert trades(body, data) == [(26, 'short', ENTRY[name], 69, FINAL[name])]
    report = body['raw_report']['candle_pattern']
    assert (report['direction'], report['skipped_entry_count']) == ('short', 0)


@pytest.mark.parametrize('name', SHORT)
def test_frozen_stop_above_entry_exits_next_open(monkeypatch, tmp_path, name):
    data = frame(name)
    E = float(ENTRY[name])
    set_bar(data, 30, E, 103.2, E - .5, E)        # High crosses 103.103 -> stop_loss queued
    set_bar(data, 31, 101, 101.5, 100.5, 101)     # fills at this open
    body, _ = post(monkeypatch, tmp_path, name, data=data)
    assert trades(body, data) == [(26, 'short', ENTRY[name], 31, '101')], body
    # Just below the stop does not trigger.
    data = frame(name)
    set_bar(data, 30, E, 103.1, E - .5, E)
    body, _ = post(monkeypatch, tmp_path, name, data=data)
    assert trades(body, data)[0][3] == 69


@pytest.mark.parametrize('reward_r', [2, 0.5])
@pytest.mark.parametrize('name', SHORT)
def test_target_below_entry_by_reward_r_times_risk(monkeypatch, tmp_path, name, reward_r):
    E = float(ENTRY[name])
    target = E - (103.103 - E) * reward_r
    data = frame(name)
    set_bar(data, 30, E, E + .5, target, E)       # Low touches the target exactly
    set_bar(data, 31, 88, 88.5, 87.5, 88)
    body, _ = post(monkeypatch, tmp_path, name, dict(reward_r=reward_r), data)
    assert trades(body, data) == [(26, 'short', ENTRY[name], 31, '88')], body
    data = frame(name)
    set_bar(data, 30, E, E + .5, target + .01, E)  # one cent short of the target
    body, _ = post(monkeypatch, tmp_path, name, dict(reward_r=reward_r), data)
    assert trades(body, data)[0][3] == 69


@pytest.mark.parametrize('name', SHORT)
def test_entry_open_at_or_above_frozen_stop_is_skipped(monkeypatch, tmp_path, name):
    data = frame(name)
    set_bar(data, 26, 103.103, 103.5, 102, 103)
    body, _ = post(monkeypatch, tmp_path, name, data=data)
    assert body['trades'] == []
    [skip] = body['raw_report']['candle_pattern']['skipped_entries']
    assert (skip['reason'], skip['entry_bar'], skip['frozen_stop']) == ('entry_open_at_or_above_frozen_stop', 26, 103.103)


@pytest.mark.parametrize('name', SHORT)
def test_risk_sizing_uses_frozen_stop_distance(monkeypatch, tmp_path, name):
    body, data = post(monkeypatch, tmp_path, name, dict(position_size_risk_pct=1))
    [trade] = body['trades']
    E = Decimal(ENTRY[name])
    # 1% of 1000 over |fill - 103.103|, floored by the engine to the quantity step.
    assert abs(Decimal(trade['qty']) - Decimal(10) / (Decimal(STOP) - E)) < Decimal('0.01'), trade


# --- isolated liquidation (P-LIQ1 / T2-2b) ----------------------------------------------------------

EXPECTED = {'A_next_open': '96', 'A2_full_close': '105.7', 'B_liquidation': '99', 'G_gap_open': '106.6'}


def expected(scenario, sizing):
    if scenario == 'A1' or (scenario == 'A2' and sizing == 'pct20'):
        return 'stop_loss', EXPECTED['A_next_open'], 1, 0, None
    if scenario == 'A2':
        return 'stop_loss', EXPECTED['A2_full_close'], 0, 0, None
    if scenario == 'G':
        return 'liquidation', EXPECTED['G_gap_open'], 0, 1, True
    return 'liquidation', EXPECTED['B_liquidation'], 0, 1, False


@pytest.fixture
def scenes(monkeypatch):
    for name in SHORT:
        monkeypatch.setitem(q.SETUP, name, dict(fill=26, side='short', A=(STOP, 96, {}), B=(STOP, 90, {})))
    base = q.base_frame
    monkeypatch.setattr(q, 'base_frame', lambda name: frame(name) if name in MIRROR else base(name))


@pytest.mark.parametrize('sizing', q.SIZINGS)
@pytest.mark.parametrize('scenario', q.SCENARIOS)
@pytest.mark.parametrize('name', SHORT)
def test_isolated_scenarios_follow_t2_2b(monkeypatch, tmp_path, scenes, name, scenario, sizing):
    calls, closes = [], []
    arbitrate, close = p._FixedRiskMixin._risk_isolated_exit, bb.Position.close

    def isolated_exit(self, stop=None):
        calls.append((len(self.data) - 1, stop))
        return arbitrate(self, stop)

    def position_close(self, portion=1.0):
        strategy = sys._getframe(1).f_locals['self']
        closes.append((len(strategy.data) - 1, strategy._risk_exit_reason))
        return close(self, portion)
    monkeypatch.setattr(p._FixedRiskMixin, '_risk_isolated_exit', isolated_exit)
    monkeypatch.setattr(bb.Position, 'close', position_close)
    body, data, k = q.post_scenario(monkeypatch, tmp_path, name, scenario, sizing)
    assert body['result_status'] == 'success', body
    reason, price, offset, count, gap = expected(scenario, sizing)
    [trade] = body['trades']
    assert (q.bar_of(data, trade['opened_at']), trade['side'], trade['entry_price']) == (
        k - 1, 'short', '90' if scenario.startswith('B') else '96')
    assert (trade['exit_price'], q.bar_of(data, trade['closed_at'])) == (price, k + offset)
    assert closes == [(k, reason)]
    report = body['raw_report']['isolated_risk']
    assert report['liquidation_count'] == count
    if count:
        [row] = report['liquidations']
        assert (row['fill_price'], row['liquidation_gap']) == (price, gap)
    # Every arbitration call carries the frozen stop; fill bar and crash bar are both arbitrated.
    assert calls and {stop for _, stop in calls} == {Decimal(STOP)}
    assert sorted({bar for bar, _ in calls}) == [k - 1, k]


@pytest.mark.parametrize('sizing', q.SIZINGS)
@pytest.mark.parametrize('name', SHORT)
def test_signal_on_liquidation_bar_places_no_order(monkeypatch, tmp_path, scenes, name, sizing):
    # Short mirror of test_pliq1_pattern_liquidation's outcome pin: no entry on the liquidation bar.
    # As on the long side this holds via next()'s position branch; the _isolated_blocked_bar guard is
    # unreachable defence and dropping it keeps this test green, so it does NOT prove the guard itself.
    import strategy_pattern_template as module
    data, k = q.scenario_frame(name, 'B1')
    signal_bar = q.SETUP[name]['fill'] - 1
    make, sells, sell = module.make_short_pattern_strategy, [], bb.Strategy.sell

    def with_extra_signal(*args, **kwargs):
        class ExtraSignal(make(*args, **kwargs)):
            def init(self):
                super().init()
                at, source = self._warmup_bars + k, self._warmup_bars + signal_bar
                self._signals[at], self._anchors[at] = self._signals[source], self._anchors[source]
        return ExtraSignal

    def spy_sell(self, *args, **kwargs):
        sells.append(len(self.data) - 1)
        return sell(self, *args, **kwargs)
    monkeypatch.setattr(module, 'make_short_pattern_strategy', with_extra_signal)
    monkeypatch.setattr(bb.Strategy, 'sell', spy_sell)
    body = q.post(monkeypatch, tmp_path, name, q.scenario_params(name, 'B1', sizing), data, 'futures')
    assert body['result_status'] == 'success', body
    assert sells == [signal_bar]
    [trade] = body['trades']
    assert body['raw_report']['isolated_risk']['liquidation_count'] == 1
    assert q.bar_of(data, trade['closed_at']) == k


@pytest.mark.parametrize('name', SHORT)
def test_leverage_one_never_arbitrates(monkeypatch, tmp_path, name):
    monkeypatch.setattr(p._FixedRiskMixin, '_risk_isolated_exit', lambda *a, **k: pytest.fail('lev 1 arbitrated'))
    body, data = post(monkeypatch, tmp_path, name, dict(leverage=1))
    assert trades(body, data) == [(26, 'short', ENTRY[name], 69, FINAL[name])]
    assert 'T2-2b' not in json.dumps(body['assumptions'])


# SHORT-PAT-5 shape and position rules, mirrored from test_9t2_patterns (edits applied to the long frame,
# then reflected, so the long tests' hand numbers carry over).
def _mirrored_9t2(long_name, edit=None):
    from test_9t2_patterns import frame as long_frame
    data = long_frame(long_name)
    if edit:
        edit(data)
    return reflect(data)


@pytest.mark.parametrize('enabled', [False, True])
def test_crows_rise_filter_switch(monkeypatch, tmp_path, enabled):
    def edit(data):
        data.iloc[3, :4] = [96, 98, 95, 97]  # mirrored: first crow open 102 <= close 20 bars ago 103
    body, data = post(monkeypatch, tmp_path, 'three_black_crows', dict(position_filter=enabled),
                      _mirrored_9t2('three_white_soldiers', edit))
    assert [t[:3] for t in trades(body, data)] == ([] if enabled else [(26, 'short', '90')])


@pytest.mark.parametrize('close,valid', [(104, False), (104.001, True), (103.999, False)])
def test_doji_only_next_strict_close_below_low_confirms(monkeypatch, tmp_path, close, valid):
    def edit(data):
        data.iloc[25, :4] = [101, 109, 99, close]  # mirrored close vs doji low 96: 96 / 95.999 / 96.001
    body, data = post(monkeypatch, tmp_path, 'bearish_doji_reversal', {}, _mirrored_9t2('bullish_doji_reversal', edit))
    assert [t[:3] for t in trades(body, data)] == ([(26, 'short', '90')] if valid else [])


def test_doji_failed_next_confirmation_never_confirms_later(monkeypatch, tmp_path):
    def edit(data):
        data.iloc[25, :4] = [101, 104, 99, 103]
    body, data = post(monkeypatch, tmp_path, 'bearish_doji_reversal', {}, _mirrored_9t2('bullish_doji_reversal', edit))
    assert trades(body, data) == []


@pytest.mark.parametrize('enabled', [False, True])
def test_doji_rsi_above_70_at_candidate(monkeypatch, tmp_path, enabled):
    def edit(data):
        for i in range(24):
            data.iloc[i, :4] = [70+i, 72+i, 69+i, 71+i]  # long RSI 100 => mirrored RSI 0, never > 70
    body, data = post(monkeypatch, tmp_path, 'bearish_doji_reversal', dict(position_filter=enabled),
                      _mirrored_9t2('bullish_doji_reversal', edit))
    assert [t[:3] for t in trades(body, data)] == ([] if enabled else [(26, 'short', '90')])


@pytest.mark.parametrize('name,anchor', [('three_black_crows', 'first crow high'), ('bearish_doji_reversal', 'doji high')])
def test_crows_and_doji_assumption_names_their_stop(monkeypatch, tmp_path, name, anchor):
    body, _ = post(monkeypatch, tmp_path, name)
    assert anchor in body['assumptions']['pattern_execution']
    assert body['raw_report']['candle_pattern']['direction'] == 'short'
