"""6b: actual fills, shared expiry arbitration, DST and registered template wiring."""
from datetime import datetime
import math

import pandas as pd
import pytest
from backtesting import Backtest, Strategy
from fastapi.testclient import TestClient

from test_risk_layer import p, bars_frame
import test_risk_overlay_compatibility as compat
import test_time_layer_compatibility as time_compat
from strategy_time_layer import TimeConfig, TimeContext, expiry_due

NEW = {'time_max_holding_minutes': 0, 'time_flatten_at': '', 'time_flatten_weekdays': 127}
FLAT = [100, 101, 99, 100]


@pytest.mark.parametrize('key,value', list(NEW.items()))
def test_defaults(key, value):
    assert TimeConfig.parse({key: value}) == TimeConfig()


@pytest.mark.parametrize('extra', [
    {'time_max_holding_minutes': 0}, {'time_max_holding_minutes': 1},
    {'time_max_holding_minutes': 525600}, {'time_flatten_at': '00:00'},
    {'time_flatten_at': '23:59'}, {'time_flatten_at': '12:00', 'time_flatten_weekdays': 1},
    {'time_flatten_at': '12:00', 'time_flatten_weekdays': 127},
])
def test_valid_boundaries(extra):
    assert TimeConfig.parse(dict(time_layer_enabled=True, **extra)).enabled


BAD = [
    *[{'time_max_holding_minutes': v} for v in (-1, 525601, True, False, 1.0, 0.0, '60', None)],
    *[{'time_flatten_at': v} for v in ('24:00', '1:00', '12:60', ' 12:00', '12:00:00', 1200, True, None)],
    *[{'time_flatten_at': '12:00', 'time_flatten_weekdays': v}
      for v in (0, 128, -1, True, False, 1.0, 127.0, '1', None)],
    {'time_flatten_weekdays': 1},
]


@pytest.mark.parametrize('extra', BAD)
def test_invalid_before_market_fetch(extra, monkeypatch):
    def forbidden(*a, **k):
        pytest.fail('invalid time parameters fetched market data')
    monkeypatch.setattr(p, '_fetch_ohlcv', forbidden)
    monkeypatch.setattr(p, '_fetch_template_warmup', forbidden)
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    body = time_compat.capture.request_body('ema_cross', dict(time_layer_enabled=True, **extra))
    result = TestClient(p.app).post('/cutie/backtest', json=body).json()
    assert result['error_type'] == 'INVALID_PARAMS'


@pytest.mark.parametrize('extra', [{'time_max_holding_minutes': 1}, {'time_flatten_at': '12:00'}])
def test_time_nondefault_requires_time_gate(extra):
    with pytest.raises(ValueError, match='INVALID_PARAMS:'):
        TimeConfig.parse(dict(risk_layer_enabled=True, **extra))


@pytest.mark.parametrize('risk,time,accepted', [(False, False, False), (True, False, True),
                                               (False, True, True), (True, True, True)])
def test_shared_bar_gate(risk, time, accepted):
    params = dict(risk_layer_enabled=risk, time_layer_enabled=time, max_holding_bars=3)
    if accepted:
        cls = p.TOOL_SPECS['local.backtesting_py.ema_cross']['build'](params)['strategy']
        assert cls._risk['max_holding_bars'] == 3
        assert bool(cls._risk.get('risk_layer_enabled')) == risk
    else:
        with pytest.raises(ValueError, match='max_holding_bars requires risk_layer_enabled=true'):
            p._parse_fixed_risk_params(params)


def run(extra, *, rows=None, start='2026-01-01', period='1h', side='long', signal=False, repeat=True):
    rows = rows or [FLAT] * 16
    if side == 'short':
        rows = [[200-o, 200-lo, 200-hi, 200-c] for o, hi, lo, c in rows]
    data = bars_frame(rows)
    data.index = pd.date_range(start, periods=len(data), freq=period.replace('m', 'min'))
    params = dict(time_layer_enabled=True, **extra)
    class Manual(p._FixedRiskMixin, Strategy):
        _risk = p._parse_fixed_risk_params(params)
        _time_config = TimeConfig.parse(params)
        _time_context = TimeContext.build(_time_config, period, data.index)
        def init(self):
            self._risk_init()
            self.snapshots = []
            self.signal_orders = 0
        def _risk_entry_size(self):
            return 11
        def next(self):
            i = len(self.data) - 1
            if i == 1:
                (self._risk_buy if side == 'long' else self._risk_sell)()
            if not self.position:
                return
            done = self._risk_check_exit()
            reason = getattr(self, '_risk_exit_reason', None)
            self.snapshots.append((i, done, reason))
            if done:
                if repeat:
                    count = len(self.orders)
                    assert self._risk_check_exit()
                    assert len(self.orders) == count == 1
                return
            if signal and i >= 3:
                self.signal_orders += 1
                self.position.close()
    return Backtest(data, Manual, cash=100000, exclusive_orders=True, finalize_trades=True).run()


@pytest.mark.parametrize('risk', [False, True])
@pytest.mark.parametrize('side', ['long', 'short'])
@pytest.mark.parametrize('n', [2, 3, 4])
def test_holding_n_minus_one_n_n_plus_one(risk, side, n):
    result = run(dict(risk_layer_enabled=risk, max_holding_bars=n), side=side)
    assert list(result['_trades'].EntryBar) == [2]
    assert list(result['_trades'].ExitBar) == [2+n]
    snapshots = result['_strategy'].snapshots
    assert all(not done and reason is None for i, done, reason in snapshots if i < n+1)
    assert snapshots[-1] == (n+1, True, 'time_expiry')


@pytest.mark.parametrize('side', ['long', 'short'])
def test_partial_exit_keeps_original_holding_clock(side):
    rows = [FLAT]*3 + [[100,111,99,110], [111,112,100,110], [112,113,101,112], FLAT]
    result = run(dict(risk_layer_enabled=True, stop_loss_pct=10, trailing_stop_pct=20,
                      tp1_r=1, tp1_close_pct=30, tp2_r=2, tp2_close_pct=30,
                      tp3_r=3, tp3_close_pct=40, max_holding_bars=3), rows=rows, side=side)
    assert list(result['_trades'].EntryBar) == [2,2]
    assert list(result['_trades'].ExitBar) == [4,5]
    assert list(abs(result['_trades'].Size)) == [3,8]
    assert result['_strategy'].snapshots[-1] == (4, True, 'time_expiry')


@pytest.mark.parametrize('risk', [False, True])
@pytest.mark.parametrize('period,start,minutes', [
    ('4h', '2026-03-07 22:00', 480), ('4h', '2026-10-31 21:00', 480),
    ('15m', '2026-03-08 06:00', 90), ('15m', '2026-11-01 05:00', 90),
])
def test_minutes_actual_fill_utc_across_dst(risk, period, start, minutes):
    result = run(dict(risk_layer_enabled=risk, time_timezone='America/New_York',
                      time_max_holding_minutes=minutes), period=period, start=start)
    trade = result['_trades'].iloc[0]
    expected = math.ceil(minutes / (240 if period == '4h' else 15))
    assert trade.EntryBar == 2 and trade.ExitBar == 2+expected
    assert (trade.ExitTime - trade.EntryTime).total_seconds() == minutes*60
    assert result['_strategy'].snapshots[-1][2] == 'time_expiry'


@pytest.mark.parametrize('risk', [False, True])
@pytest.mark.parametrize('cutoff,delay', [('11:00', 0.0), ('10:30', 0.5)])
def test_flatten_close_boundary_or_mid_bar(risk, cutoff, delay):
    result = run(dict(risk_layer_enabled=risk, time_flatten_at=cutoff), start='2026-01-01 08:00')
    assert list(result['_trades'].ExitBar) == [3]
    context = result['_strategy']._time_context
    assert context.flatten_delays == [delay]  # repeated pending check cannot double count
    assert context.assumptions()['time_layer']['holding']['flatten_delay_bars']['max'] == delay


@pytest.mark.parametrize('risk', [False, True])
def test_nonexistent_dst_cutoff_skips_entire_day(risk):
    result = run(dict(risk_layer_enabled=risk, time_timezone='America/New_York', time_flatten_at='02:30'),
                 start='2026-03-08 04:00', rows=[FLAT]*40)
    assert result['_trades'].iloc[0].ExitTime == pd.Timestamp('2026-03-09 07:00')
    assert result['_strategy']._time_context.flatten_delays == [0.5]


@pytest.mark.parametrize('entry,open_at,expected', [
    ('2026-11-01T05:00+00:00', '2026-11-01T05:15+00:00', True),
    ('2026-11-01T05:45+00:00', '2026-11-01T06:15+00:00', False),
    ('2026-11-01T06:30+00:00', '2026-11-02T06:15+00:00', True),
])
def test_repeated_dst_cutoff_only_first_instant(entry, open_at, expected):
    config = TimeConfig.parse(dict(time_layer_enabled=True, time_timezone='America/New_York', time_flatten_at='01:30'))
    ctx = TimeContext.build(config, '15m', [datetime.fromisoformat(open_at)])
    fact = expiry_due(holding_bars=0, entry_bar=0, bar=1, entry_utc=datetime.fromisoformat(entry),
                      bar_open=datetime.fromisoformat(open_at), context=ctx)
    assert fact.due is expected


@pytest.mark.parametrize('mask,exit_time', [(32, '2026-10-10 00:00'), (64, '2026-10-11 00:00')])
def test_flatten_weekday_mask_and_overnight(mask, exit_time):
    result = run(dict(time_flatten_at='00:00', time_flatten_weekdays=mask),
                 start='2026-10-09 21:00', rows=[FLAT]*36)
    assert result['_trades'].iloc[0].ExitTime == pd.Timestamp(exit_time)


@pytest.mark.parametrize('risk', [False, True])
@pytest.mark.parametrize('side', ['long', 'short'])
@pytest.mark.parametrize('mode,expected', [('stop', 'stop_loss'), ('take', 'time_expiry'), ('signal', 'time_expiry')])
def test_same_bar_priority_and_signal_suppression(risk, side, mode, expected):
    row = [100,121,89,89] if mode == 'stop' else [100,121,99,120]
    extra = dict(risk_layer_enabled=risk, max_holding_bars=2, stop_loss_pct=10)
    if mode != 'signal':
        extra['take_profit_pct'] = 10
    result = run(extra, rows=[FLAT]*3+[row,FLAT,FLAT], side=side, signal=True)
    assert result['_strategy'].snapshots[-1] == (3, True, expected)
    assert result['_strategy'].signal_orders == 0
    assert list(result['_trades'].ExitBar) == [4]


def test_time_only_retains_close_price_stop_semantics():
    rows = [FLAT]*2 + [[100,121,89,100], FLAT, FLAT, FLAT]
    result = run(dict(risk_layer_enabled=False, stop_loss_pct=10, take_profit_pct=10, max_holding_bars=2), rows=rows)
    assert result['_strategy'].snapshots[0] == (2, False, None)
    assert result['_strategy'].snapshots[-1] == (3, True, 'time_expiry')


@pytest.mark.parametrize('name', [name for name in compat.enumerate_mixin_cases()
    if 'max_holding_bars' in p.TOOL_SPECS['local.backtesting_py.' + compat.tool_name(name)]['param_schema_properties']])
def test_every_registered_single_template_consumes_holding_bars(name):
    if name in ('bullish_engulfing', 'hammer_pin_bar'):
        from test_9t1_engulf_pin import compatibility_frame
        data = compatibility_frame(name)
    elif name in ('double_top', 'head_shoulders'):
        from test_short_pat1 import compatibility_frame
        data = compatibility_frame(name)
    elif name in p._SHORT_CANDLE_TOOL_NAMES.values():
        from test_short_pat4_candles import compatibility_frame
        data = compatibility_frame(name)
    elif name in ('double_bottom', 'inverse_head_shoulders'):
        from test_9t3_patterns import compatibility_frame
        data = compatibility_frame(name)
    elif name in p._CANDLE_TOOL_NAMES.values():
        from test_9t2_patterns import compatibility_frame
        data = compatibility_frame(name)
    else:
        data = pd.concat([compat.frame()]*3, ignore_index=True)
    data.index = pd.date_range('2026-01-01', periods=len(data), freq='h')
    params = dict(compat.PARAMS.get(name, {}), time_layer_enabled=True, max_holding_bars=3)
    if name == 'opening_range_breakout':
        params['flatten_at'] = '23:00'  # 1h test grid
    if name == 'calendar_schedule':
        params.update(time_entry_at='02:00', time_max_holding_minutes=60*24, calendar_stop_enabled=False)
    if name in ('red_streak_rsi', 'vwap_reversion'):
        params['risk_layer_enabled'] = False  # their default stops would turn the layer on (Q42-C); this test is time-only
    if name.endswith('_short'):
        params['direction'] = 'short'
    if name.endswith('_bullish_divergence'):
        from test_9t4_divergence import hand_frame, hand_params
        data = hand_frame()
        params.update(hand_params(name))
        if name.startswith('rsi'):
            params['rsi_exit_above'] = 100
    if name.endswith('_bearish_divergence'):
        from test_short_pat2 import hand_frame, hand_params
        data = hand_frame()
        params.update(hand_params(name))
        if name.startswith('rsi'):
            params['rsi_exit_below'] = 0
    if name == 'ema_rsi_pullback':
        params['rsi_exit'] = 85
    if name == 'ichimoku_cloud_breakout':
        params.update(tenkan_period=5, kijun_period=10, senkou_b_period=20)
    if name == 'event_window':
        params.update(compat.required_params(name))  # P-EVENT0: inline events, no default list
    if name == 'chan_3buy':
        from test_9t6_chan_3buy import frame as chan_frame
        data = chan_frame()
    tf = '1h'
    if name == 'us_open_momentum':
        data.index = pd.date_range('2026-01-01', periods=len(data), freq='15min')
        tf = '15m'
    if name == 'cme_weekend_gap':
        from test_f4_cme_gap import frame as cme_frame
        data = cme_frame()
    cls = p.TOOL_SPECS['local.backtesting_py.'+compat.tool_name(name)]['build'](params)['strategy']
    assert not cls._risk.get('risk_layer_enabled')
    cls._time_context = TimeContext.build(cls._time_config, tf, data.index)
    reasons = []
    original = cls._record_holding_expiry
    def record(self, fact):
        reasons.append('time_expiry')
        original(self, fact)
    cls._record_holding_expiry = record
    trades = Backtest(data, cls, cash=100000, exclusive_orders=True, finalize_trades=True).run()['_trades']
    assert len(trades) and reasons, name
    assert all(trades.ExitBar-trades.EntryBar <= 3), name


def test_enabled_without_expiry_has_no_holding_assumptions():
    body = time_compat.capture.response('ema_cross', dict(time_layer_enabled=True))
    layer = body['assumptions']['time_layer']
    assert layer == dict(timezone='UTC', tzdata_version=layer['tzdata_version'], session_start='',
                        session_end='', weekdays=127, decision_time='bar_close', gate='entry_only', fill='next_bar_open')
    assert 'holding' not in layer


def test_expiry_assumptions_full_dictionary():
    body = time_compat.capture.response('ema_cross', dict(time_layer_enabled=True, max_holding_bars=3,
                                                        time_max_holding_minutes=120, time_flatten_at='12:30',
                                                        time_flatten_weekdays=31))
    holding = body['assumptions']['time_layer']['holding']
    assert holding == dict(bars=3, minutes=120, flatten_at='12:30', flatten_weekdays=31,
        bars_count_from='entry_fill_bar_is_1', minutes_count_from='actual_entry_fill_utc',
        same_bar_priority='stop_loss_before_time_expiry_before_take_profit_before_template_signal',
        final_bar='engine_finalize_trades_settlement', flatten_dst='nonexistent_skip_day_repeated_first_only',
        flatten_delay_bars=dict(
            definition='(submission_decision_utc - cutoff_utc) / bar_period; fractional bars; '
                       'only flatten-due facts winning expiry arbitration; excludes next-open fill wait',
            count=holding['flatten_delay_bars']['count'], max=holding['flatten_delay_bars']['max']))
    assert holding['flatten_delay_bars']['count'] >= 0 and holding['flatten_delay_bars']['max'] >= 0


@pytest.mark.parametrize('name', time_compat.EXCLUDED)
@pytest.mark.parametrize('key,value', NEW.items())
def test_unwired_runners_reject_new_keys(name, key, value, monkeypatch):
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a: pytest.fail('unwired template fetched data'))
    body = time_compat.capture.request_body(name, {key: value})
    body['backtest']['market'] = 'spot' if name in time_compat.capture.LEDGER_PARAMS else 'futures'
    assert TestClient(p.app).post('/cutie/backtest', json=body).json()['error_type'] == 'INVALID_PARAMS'
    assert key not in p.TOOL_SPECS['local.backtesting_py.'+name]['param_schema_properties']


@pytest.mark.parametrize('risk', [False, True])
def test_repeated_dst_first_cutoff_engine_fill(risk):
    result = run(dict(risk_layer_enabled=risk, time_timezone='America/New_York', time_flatten_at='01:30'),
                 start='2026-11-01 04:00', period='15m')
    assert result['_trades'].iloc[0].ExitTime == pd.Timestamp('2026-11-01 05:30')
    assert result['_strategy']._time_context.flatten_delays == [0.0]


@pytest.mark.parametrize('risk', [False, True])
def test_stop_wins_flatten_does_not_inflate_delay_statistics(risk):
    rows = [FLAT]*2 + [[100,101,89,89], FLAT, FLAT]
    result = run(dict(risk_layer_enabled=risk, stop_loss_pct=10, time_flatten_at='10:30'),
                 rows=rows, start='2026-01-01 08:00')
    assert result['_strategy'].snapshots[-1] == (2, True, 'stop_loss')
    assert result['_strategy']._time_context.flatten_delays == []
