"""ORB fills through the real broker; prices and bars also audited against pin14."""
from decimal import Decimal

import pytest

from test_f1_range_breakout import market_frame, direct, http_run


SCENARIOS = {
    'stop': (6, [112, 113, 89, 112, 1], 90, 'stop_loss'),
    'stop_gap': (6, [80, 81, 79, 80, 1], 80, 'stop_loss'),
    'both': (6, [112, 153, 89, 112, 1], 90, 'stop_loss'),
    'take': (6, [112, 153, 111, 112, 1], 152, 'take_profit'),
    'take_gap': (6, [160, 161, 159, 160, 1], 160, 'take_profit'),
    'entry_stop': (5, [112, 113, 89, 112, 1], 90, 'stop_loss'),
    'entry_take': (5, [112, 153, 111, 112, 1], 152, 'take_profit'),
    'entry_both': (5, [112, 153, 89, 112, 1], 90, 'stop_loss'),
}


def scenario_frame(name, side='long'):
    data = market_frame()
    bar, row, price, reason = SCENARIOS[name]
    data.iloc[bar] = row
    if side == 'short':
        data[['Open', 'High', 'Low', 'Close']] = [
            [200-o, 200-lo, 200-hi, 200-c]
            for o, hi, lo, c in data[['Open', 'High', 'Low', 'Close']].to_numpy()
        ]
    return data, bar, (price if side == 'long' else 200-price), reason


@pytest.mark.parametrize('side', ['long', 'short'])
@pytest.mark.parametrize('name', SCENARIOS)
def test_orb_native_same_bar_fill(name, side):
    data, bar, price, reason = scenario_frame(name, side)
    stats = direct(data, {'direction': side})
    assert len(stats['_trades']) == 1
    trade = stats['_trades'].iloc[0]
    assert (trade.EntryBar, trade.ExitBar) == (5, bar)
    assert trade.EntryPrice == (112 if side == 'long' else 88)
    assert trade.ExitPrice == pytest.approx(price)
    day = next(iter(stats['_strategy'].daily_ranges.values()))
    assert day['exit_reason'] == reason
    assert day['stop_price'] == (90 if side == 'long' else 110)
    assert day['take_price'] == (152 if side == 'long' else 48)


def test_orb_target_tracks_actual_fill_with_same_range_formula():
    data = market_frame()
    data.iloc[5] = [120, 121, 119, 120, 1]
    data.iloc[6] = [120, 161, 119, 120, 1]
    stats = direct(data)
    trade = stats['_trades'].iloc[0]
    day = next(iter(stats['_strategy'].daily_ranges.values()))
    assert (trade.EntryPrice, trade.ExitPrice, trade.ExitBar) == (120, 160, 6)
    assert Decimal(str(day['take_price'])) == Decimal(str(trade.EntryPrice)) + (
        Decimal(str(day['high'])) - Decimal(str(day['low']))) * 2
    assert day['stop_price'] == day['low'] == 90


@pytest.mark.parametrize('holding', [False, True])
@pytest.mark.parametrize('touch', ['take', 'stop', 'both', 'none'])
@pytest.mark.parametrize('side', ['long', 'short'])
def test_orb_stop_then_time_then_take_priority(holding, touch, side):
    data = market_frame()
    data.iloc[11] = [112, 153 if touch in ('take', 'both') else 113,
                     89 if touch in ('stop', 'both') else 111, 112, 1]
    data.iloc[12] = [117, 118, 116, 117, 1]
    params = ({'risk_layer_enabled': True, 'max_holding_bars': 7}
              if holding else {'flatten_at': '03:00'})
    if side == 'short':
        data[['Open', 'High', 'Low', 'Close']] = [
            [200-o, 200-lo, 200-hi, 200-c]
            for o, hi, lo, c in data[['Open', 'High', 'Low', 'Close']].to_numpy()
        ]
    params['direction'] = side
    stats = direct(data, params)
    trade = stats['_trades'].iloc[0]
    day = next(iter(stats['_strategy'].daily_ranges.values()))
    if touch in ('stop', 'both'):
        assert (trade.ExitBar, trade.ExitPrice) == (11, 90 if side == 'long' else 110)
        assert day['exit_reason'] == 'stop_loss'
    else:
        assert (trade.ExitBar, trade.ExitPrice) == (12, 117 if side == 'long' else 83)
        assert day['exit_reason'] == 'time_expiry'


def test_orb_entry_bar_expiry_preserves_stop_before_time():
    data = market_frame()
    data.iloc[5] = [112, 153, 89, 112, 1]
    stats = direct(data, {'risk_layer_enabled': True, 'max_holding_bars': 1})
    trade = stats['_trades'].iloc[0]
    assert (trade.EntryBar, trade.ExitBar, trade.ExitPrice) == (5, 5, 90)
    assert next(iter(stats['_strategy'].daily_ranges.values()))['exit_reason'] == 'stop_loss'


def test_orb_entry_bar_due_defers_target_to_next_open_time_exit():
    data = market_frame()
    data.iloc[5] = [112, 153, 111, 112, 1]
    data.iloc[6] = [117, 180, 89, 117, 1]
    stats = direct(data, {'risk_layer_enabled': True, 'max_holding_bars': 1})
    trade = stats['_trades'].iloc[0]
    assert (trade.EntryBar, trade.ExitBar, trade.ExitPrice) == (5, 6, 117)
    assert next(iter(stats['_strategy'].daily_ranges.values()))['exit_reason'] == 'time_expiry'


@pytest.mark.parametrize('touch', ['none', 'take', 'stop', 'both'])
@pytest.mark.parametrize('due', [False, True])
def test_orb_market_signal_close_stays_after_protection_and_time(touch, due):
    data = market_frame()
    data.iloc[11] = [112, 153 if touch in ('take', 'both') else 113,
                     89 if touch in ('stop', 'both') else 111, 112, 1]
    data.iloc[12] = [117, 118, 116, 117, 1]
    def signal(base):
        class Signal(base):
            def next(self):
                super().next()
                if len(self.data) == 12 and self.position and self._risk_exit_reason is None:
                    self._risk_exit_reason = 'template_signal'
                    self.position.close()
        return Signal
    stats = direct(data, {'flatten_at': '03:00'} if due else {}, subclass=signal)
    trade = stats['_trades'].iloc[0]
    if touch in ('stop', 'both'):
        assert (trade.ExitBar, trade.ExitPrice) == (11, 90)
        assert stats['_strategy']._risk_exit_reason == 'stop_loss'
    elif due:
        assert (trade.ExitBar, trade.ExitPrice) == (12, 117)
        assert stats['_strategy']._risk_exit_reason == 'time_expiry'
    elif touch == 'take':
        assert (trade.ExitBar, trade.ExitPrice) == (11, 152)
        assert stats['_strategy']._risk_exit_reason == 'take_profit'
    else:
        assert (trade.ExitBar, trade.ExitPrice) == (12, 117)
        assert stats['_strategy']._risk_exit_reason == 'template_signal'


def test_asia_protective_fill_stays_next_open():
    data = market_frame(count=40, signal=28)
    data.iloc[30] = [112, 113, 89, 112, 1]
    data.iloc[31] = [85, 86, 84, 85, 1]
    stats = direct(data, tool='asia_range_breakout')
    trade = stats['_trades'].iloc[0]
    assert (trade.EntryBar, trade.ExitBar, trade.ExitPrice) == (29, 31, 85)


def test_orb_http_reports_distinct_protective_and_market_fills(monkeypatch, tmp_path):
    data, _, _, _ = scenario_frame('stop')
    result = http_run(monkeypatch, tmp_path, data)
    assert result['result_status'] == 'success', result
    trade = result['trades'][0]
    assert trade['closed_at'] == int(data.index[6].timestamp())
    assert float(trade['exit_price']) == 90
    assumption = result['assumptions']['range_breakout']
    assert assumption['fill'] == 'native_protection_stop_limit'
    assert assumption['market_fill'] == 'next_bar_open_market'
    assert assumption['entry_trigger'] == 'closed_bar_breakout'
    assert assumption['trigger'] == 'protective_intrabar_high_low'


@pytest.mark.parametrize('enabled', [False, True])
def test_orb_intrinsic_protection_does_not_depend_on_optional_risk_switch(enabled):
    data, bar, price, reason = scenario_frame('take_gap')
    stats = direct(data, {'risk_layer_enabled': enabled})
    trade = stats['_trades'].iloc[0]
    assert (trade.EntryBar, trade.ExitBar, trade.ExitPrice) == (5, bar, price)
    day = next(iter(stats['_strategy'].daily_ranges.values()))
    assert (day['stop_price'], day['take_price'], day['exit_reason']) == (90, 152, reason)


@pytest.mark.parametrize('entry_due', [False, True])
@pytest.mark.parametrize('touch', ['take', 'both', 'gap'])
def test_orb_due_target_removed_before_real_broker_and_preserves_other_orders(monkeypatch, entry_due, touch):
    from backtesting.backtesting import _Broker
    process = _Broker._process_orders
    events = []
    active = None
    bar = 5 if entry_due else 11
    def instrument(base):
        class Instrumented(base):
            def init(self):
                nonlocal active
                super().init()
                active = self
            def _risk_native_state(self, trade):
                before = [o for o in self.orders if o is not trade._tp_order]
                stop = trade.sl
                super()._risk_native_state(trade)
                assert before == [o for o in self.orders if o is not trade._tp_order]
                assert trade.sl == stop
                events.append(('state', len(self.data)-1, trade.tp))
            def next(self):
                events.append(('next', len(self.data)-1, None))
                super().next()
        return Instrumented
    def traced_process(broker):
        if len(broker._data)-1 == bar and broker.trades:
            assert active._holding_expiry().due
            for trade in broker.trades:
                assert trade.tp is None and trade.sl == 90
            assert not any(e[0] == 'next' and e[1] == bar for e in events)
            events.append(('broker', bar, None))
        return process(broker)
    monkeypatch.setattr(_Broker, '_process_orders', traced_process)
    data = market_frame()
    data.iloc[bar] = ([80, 81, 79, 80, 1] if touch == 'gap' else
                     [112, 153, 89 if touch == 'both' else 111, 112, 1])
    data.iloc[bar+1] = [117, 180, 89, 117, 1]
    params = ({'risk_layer_enabled': True, 'max_holding_bars': 1}
              if entry_due else {'flatten_at': '03:00'})
    stats = direct(data, params, subclass=instrument)
    assert len(stats['_trades']) == 1
    trade = stats['_trades'].iloc[0]
    assert trade.EntryBar == 5
    assert (trade.ExitBar, trade.ExitPrice) == (
        (bar+1, 117) if touch == 'take' else (bar, 80 if touch == 'gap' else 90))
    assert ('broker', bar, None) in events


@pytest.mark.parametrize('side', ['long', 'short'])
@pytest.mark.parametrize('settings,quantity', [
    ({'leverage': 2}, None),
    ({'position_size_risk_pct': 1, 'position_size_qty_step': 0.001}, Decimal('4.545')),
    ({'position_size_notional': 1000}, None),
])
def test_orb_native_with_supported_leverage_and_sizing(monkeypatch, tmp_path, side, settings, quantity):
    data, bar, price, reason = scenario_frame('stop', side)
    body = http_run(monkeypatch, tmp_path, data, {**settings, 'direction': side})
    assert body['result_status'] == 'success', body
    assert len(body['trades']) == 1
    trade = body['trades'][0]
    assert trade['opened_at'] == int(data.index[5].timestamp())
    assert trade['closed_at'] == int(data.index[bar].timestamp())
    assert float(trade['entry_price']) == (112 if side == 'long' else 88)
    assert float(trade['exit_price']) == price
    assert body['raw_report']['trade_exit_kinds'] == [{'seq': 1, 'exit_kind': reason}]
    if quantity is not None:
        assert Decimal(str(trade['qty'])) == quantity
    daily = body['raw_report']['range_breakout_days'][0]
    assert daily['stop_price'] == (90 if side == 'long' else 110)
    assert daily['take_price'] == (152 if side == 'long' else 48)


def test_orb_leveraged_end_settlement_remains_period_end(monkeypatch, tmp_path):
    data = market_frame()
    body = http_run(monkeypatch, tmp_path, data, {'leverage': 2})
    assert body['result_status'] == 'success', body
    assert len(body['trades']) == 1
    trade = body['trades'][0]
    assert trade['closed_at'] == int(data.index[-1].timestamp())
    assert float(trade['exit_price']) == data.Close.iloc[-1]
    assert body['raw_report']['trade_exit_kinds'] == [
        {'seq': 1, 'exit_kind': 'end_of_data'}]
    assert body['raw_report']['range_breakout_days'][0]['exit_reason'] == 'engine_finalize_trades_settlement'
