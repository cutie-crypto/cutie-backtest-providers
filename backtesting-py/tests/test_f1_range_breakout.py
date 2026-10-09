"""Registered F1 HTTP route and real next-open fills on hand-calculated 15m bars."""
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd
import pytest
from backtesting import Backtest
from fastapi.testclient import TestClient
from test_risk_layer import p
from strategy_time_layer import TimeContext


def market_frame(count=16, signal=4, start='2026-01-01T00:00', side='long'):
    rows = [[100., 110., 90., 100., 1.]]*count
    rows = [row.copy() for row in rows]
    for i in range(signal, count):
        rows[i] = [112., 113., 111., 112., 1.]
    rows[signal] = [100., 112., 100., 111., 1.]
    if side == 'short':
        rows = [[200-o, 200-lo, 200-hi, 200-c, v] for o, hi, lo, c, v in rows]
    return pd.DataFrame(rows, columns=['Open', 'High', 'Low', 'Close', 'Volume'],
                        index=pd.date_range(start, periods=count, freq='15min'))


def direct(data, params=None, tool='opening_range_breakout', subclass=None):
    built = p.TOOL_SPECS['local.backtesting_py.'+tool]['build'](params or {})
    cls = built['strategy']
    cls._range_timeframe = '15m'
    if cls._time_config is not None:
        cls._time_context = TimeContext.build(cls._time_config, '15m', data.index)
    if subclass is not None:
        cls = subclass(cls)
    return Backtest(data, cls, cash=10000, commission=0, exclusive_orders=True, finalize_trades=True).run()


def http_run(monkeypatch, tmp_path, data, params=None, tool='opening_range_breakout', market='futures', start=None):
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', Path(tmp_path))
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a: data.copy())
    monkeypatch.setattr(p, '_fetch_template_warmup', lambda *a: pytest.fail('F1 must use strict history'))
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **k: None)
    first = data.index[0] if start is None else pd.Timestamp(start)
    request = {'backtest': dict(run_id='f1_test', provider_tool_id='local.backtesting_py.'+tool,
        provider_params=params or {}, symbol='BTCUSDT', market=market, timeframe='15m',
        start_at=int(first.timestamp()), end_at=int(data.index[-1].timestamp())+900,
        initial_capital='10000', fee_bps='0', slippage_bps='0')}
    return TestClient(p.app).post('/cutie/backtest', json=request).json()


@pytest.mark.parametrize('tool,minutes,k,flatten', [('opening_range_breakout', 60, 2, '23:45'),
                                                  ('asia_range_breakout', 420, 1.5, '20:00')])
def test_registered_defaults(tool, minutes, k, flatten):
    built = p.TOOL_SPECS['local.backtesting_py.'+tool]['build']({})
    config = built['range_config']
    assert config.start == '00:00' and config.flatten == flatten and config.multiple == k
    assert config.direction == 'long'
    assert issubclass(built['strategy'], p._FixedRiskMixin)
    assert built['strategy']._time_config is None


@pytest.mark.parametrize('side,direction,size', [('long', 'long', 1), ('short', 'short', -1),
                                               ('long', 'both', 1), ('short', 'both', -1)])
def test_close_breakout_next_open_and_frozen_range(side, direction, size):
    data = market_frame(side=side)
    result = direct(data, {'direction': direction})
    trades = result['_trades']
    assert len(trades) == 1
    assert trades.iloc[0].EntryBar == 5
    assert trades.iloc[0].EntryPrice == (112 if size > 0 else 88)
    assert (trades.iloc[0].Size > 0) == (size > 0)
    daily = list(result['_strategy'].daily_ranges.values())[0]
    assert (daily['high'], daily['low']) == (110, 90)
    assert daily['freeze_utc'] == '2026-01-01T01:00:00+00:00'
    assert daily['stop_price'] == (90 if size > 0 else 110)
    assert daily['take_price'] == (152 if size > 0 else 48)


def test_one_trade_per_day_after_exit_and_opposite_breakout():
    data = market_frame()
    data.iloc[6] = [112, 113, 89, 95, 1]
    data.iloc[7] = [88, 113, 87, 112, 1]
    data.iloc[8] = [112, 114, 111, 113, 1]
    result = direct(data, {'direction': 'both'})
    assert len(result['_trades']) == 1
    assert result['_trades'].iloc[0].ExitBar == 7


@pytest.mark.parametrize('kind,high,low,close,exit_open,expected', [
    ('stop', 113, 89, 95, 88, 'stop_loss'),
    ('take', 153, 111, 151, 155, 'take_profit'),
    ('collision', 153, 89, 100, 100, 'stop_loss'),
])
def test_exit_prices_and_stop_priority(kind, high, low, close, exit_open, expected):
    data = market_frame()
    data.iloc[6] = [112, high, low, close, 1]
    data.iloc[7] = [exit_open, max(exit_open+1, 113), min(exit_open-1, 111), 112, 1]
    result = direct(data)
    trade = result['_trades'].iloc[0]
    assert trade.ExitBar == 7 and trade.ExitPrice == exit_open
    assert list(result['_strategy'].daily_ranges.values())[0]['exit_reason'] == expected


def test_flatten_uses_decision_clock():
    result = direct(market_frame(), {'flatten_at': '03:00'})
    assert result['_trades'].iloc[0].ExitBar == 12
    assert list(result['_strategy'].daily_ranges.values())[0]['exit_reason'] == 'time_expiry'


def test_time_expiry_wins_take_and_stop_wins_both():
    data = market_frame()
    data.iloc[11] = [112, 153, 111, 151, 1]
    result = direct(data, {'flatten_at': '03:00'})
    assert list(result['_strategy'].daily_ranges.values())[0]['exit_reason'] == 'time_expiry'
    data.iloc[11] = [112, 153, 89, 100, 1]
    result = direct(data, {'flatten_at': '03:00'})
    assert list(result['_strategy'].daily_ranges.values())[0]['exit_reason'] == 'stop_loss'


def test_stop_uses_entry_frozen_snapshot_after_latest_range_changes():
    data = market_frame()
    data.iloc[5] = [112, 113, 100, 112, 1]
    data.iloc[6] = [112, 113, 89, 95, 1]
    def drift(base):
        class Drift(base):
            def next(self):
                super().next()
                if len(self.data) == 5:
                    for key, frozen in list(self._snapshots.items()):
                        self._snapshots[key] = replace(frozen, high=116, low=105)
        return Drift
    result = direct(data, subclass=drift)
    assert result['_trades'].iloc[0].ExitBar == 7
    assert list(result['_strategy'].daily_ranges.values())[0]['stop_price'] == 90


def test_asia_default_window_and_take_multiple():
    data = market_frame(count=84, signal=28)
    data.iloc[30] = [112, 143, 111, 142, 1]
    data.iloc[31] = [144, 145, 143, 144, 1]
    result = direct(data, tool='asia_range_breakout')
    trade = result['_trades'].iloc[0]
    assert trade.EntryBar == 29 and trade.ExitBar == 31 and trade.ExitPrice == 144
    daily = list(result['_strategy'].daily_ranges.values())[0]
    assert daily['freeze_utc'] == '2026-01-01T07:00:00+00:00'
    assert daily['take_price'] == 142


def test_asia_cutoff_default_20h():
    result = direct(market_frame(count=84, signal=28), tool='asia_range_breakout')
    assert result['_trades'].iloc[0].ExitBar == 80
    assert list(result['_strategy'].daily_ranges.values())[0]['exit_reason'] == 'time_expiry'


def test_changed_timezone():
    data = market_frame(start='2026-01-01T05:00')
    result = direct(data, dict(time_layer_enabled=True, time_timezone='America/New_York', flatten_at='03:00'))
    trade = result['_trades'].iloc[0]
    assert trade.EntryTime == pd.Timestamp('2026-01-01T06:15')
    assert trade.ExitTime == pd.Timestamp('2026-01-01T08:00')


@pytest.mark.parametrize('tool', ['opening_range_breakout', 'asia_range_breakout'])
@pytest.mark.parametrize('direction', ['short', 'both'])
def test_spot_short_rejected_before_fetch(monkeypatch, tool, direction):
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a: pytest.fail('spot short fetched'))
    req = {'backtest': dict(provider_tool_id='local.backtesting_py.'+tool, provider_params={'direction': direction},
        symbol='BTCUSDT', market='spot', timeframe='15m', start_at=1767225600, end_at=1767232800)}
    assert TestClient(p.app).post('/cutie/backtest', json=req).json()['error_type'] == 'INVALID_PARAMS'


def test_range_cut_through_bar_rejected_before_fetch(monkeypatch):
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a: pytest.fail('invalid window fetched'))
    req = {'backtest': dict(provider_tool_id='local.backtesting_py.opening_range_breakout',
        provider_params={'range_start': '00:07'}, symbol='BTCUSDT', market='spot', timeframe='15m',
        start_at=1767225600, end_at=1767232800)}
    assert TestClient(p.app).post('/cutie/backtest', json=req).json()['error_type'] == 'INVALID_PARAMS'


@pytest.mark.parametrize('missing', [0, 2, 8])
def test_registered_missing_history_fails(monkeypatch, tmp_path, missing):
    data = market_frame()
    response = http_run(monkeypatch, tmp_path, data.drop(data.index[missing]))
    assert response['error_type'] == 'INSUFFICIENT_DATA'
    assert response['limitations']['reason'] == 'time_history_incomplete'


def test_strict_prefix_never_trades_before_start(monkeypatch, tmp_path):
    data = market_frame()
    response = http_run(monkeypatch, tmp_path, data, start='2026-01-01T02:00')
    assert response['result_status'] == 'success', response
    assert response['trades']
    assert all(t['opened_at'] >= 1767232800 for t in response['trades'])
    daily = response['raw_report']['range_breakout_days'][0]
    assert (daily['high'], daily['low']) == (110, 90)
    assert response['assumptions']['range_breakout']['fill'] == 'next_bar_open_market'
    assert set(response['metrics']) == {'total_return', 'max_drawdown', 'trade_count'}


@pytest.mark.parametrize('params', [{'range_minutes': 14}, {'range_minutes': 241}, {'range_minutes': True},
    {'range_start': '1:00'}, {'flatten_at': '01:00'}, {'direction': 'none'}, {'take_profit_multiple': 0},
    {'take_profit_multiple': float('nan')}, {'stop_loss_pct': 2}])
def test_invalid_config(params):
    with pytest.raises(ValueError, match='INVALID_PARAMS:'):
        p.TOOL_SPECS['local.backtesting_py.opening_range_breakout']['build'](params)


def test_no_lookahead_and_zero_height_no_entry():
    data = market_frame()
    changed = data.copy()
    changed.iloc[10] = [112, 999, 111, 998, 1]
    assert direct(data)['_trades'].iloc[0].EntryBar == direct(changed)['_trades'].iloc[0].EntryBar == 5
    data.iloc[:4] = [100, 100, 100, 100, 1]
    assert direct(data)['_trades'].empty


def test_daily_reset_allows_one_new_trade_next_day():
    first = market_frame(count=96)
    second = market_frame(count=96, start='2026-01-02T00:00')
    result = direct(pd.concat([first, second]), {'flatten_at': '03:00'})
    assert list(result['_trades'].EntryBar) == [5, 101]
    assert list(result['_trades'].ExitBar) == [12, 108]
    assert [row['date'] for row in result['_strategy'].daily_ranges.values()] == ['2026-01-01', '2026-01-02']


def test_breakout_at_cutoff_cannot_enter():
    assert direct(market_frame(signal=12), {'flatten_at': '03:00'})['_trades'].empty


def test_breakout_start_later_than_freeze():
    data = market_frame(count=40, signal=28)
    result = direct(data, {'breakout_start': '08:00'}, tool='asia_range_breakout')
    assert result['_trades'].iloc[0].EntryBar == 32


def test_http_full_raw_range_and_frozen_result_keys(monkeypatch, tmp_path):
    response = http_run(monkeypatch, tmp_path, market_frame(), market='spot')
    assert response['result_status'] == 'success', response
    row = response['raw_report']['range_breakout_days'][0]
    assert row['date'] == '2026-01-01' and row['available'] and row['triggered']
    assert row['exit_reason'] == 'engine_finalize_trades_settlement'
    assert row['stop_price'] == 90 and row['take_price'] == 152
    assert 'risk_layer' not in response['assumptions'] and 'time_layer' not in response['assumptions']
    assert not any(key.startswith('range_') for trade in response['trades'] for key in trade)


def test_http_strict_since_and_trim_main(monkeypatch, tmp_path):
    data = market_frame(count=20)
    calls = []
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', Path(tmp_path))
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **k: None)
    def fetch(*args):
        calls.append(args)
        return data
    monkeypatch.setattr(p, '_fetch_ohlcv', fetch)
    req = {'backtest': dict(run_id='f1_since', provider_tool_id='local.backtesting_py.opening_range_breakout',
        provider_params={}, symbol='BTCUSDT', market='spot', timeframe='15m',
        start_at=1767232800, end_at=1767240000, initial_capital='10000', fee_bps='0', slippage_bps='0')}
    response = TestClient(p.app).post('/cutie/backtest', json=req).json()
    assert response['result_status'] == 'success', response
    assert calls[0][-2:] == (1767225600, 1767240000)
    assert response['data_manifest']['kline_count'] == 8


@pytest.mark.parametrize('direction', ['long', 'short'])
def test_generic_holding_limit_still_consumed(direction):
    data = market_frame(side=direction)
    result = direct(data, dict(direction=direction, risk_layer_enabled=True, max_holding_bars=2))
    assert result['_trades'].iloc[0].ExitBar == 7
    assert list(result['_strategy'].daily_ranges.values())[0]['exit_reason'] == 'time_expiry'


def test_nonpositive_short_target_fails_instead_of_publishing_invalid_price():
    with pytest.raises(ValueError, match='INVALID_PARAMS:nonpositive frozen range exit price'):
        direct(market_frame(side='short'), {'direction': 'short', 'take_profit_multiple': 100})


@pytest.mark.parametrize('tool', ['opening_range_breakout', 'asia_range_breakout'])
@pytest.mark.parametrize('key', ['stop_loss_pct', 'take_profit_pct', 'atr_stop_multiplier',
                                'trailing_stop_pct', 'breakeven_stop', 'tp1_r', 'take_profit_r'])
def test_unconsumed_price_keys_not_declared_and_rejected_before_fetch(monkeypatch, tool, key):
    assert key not in p.TOOL_SPECS['local.backtesting_py.'+tool]['param_schema_properties']
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a: pytest.fail('unconsumed price key fetched'))
    req = {'backtest': dict(provider_tool_id='local.backtesting_py.'+tool,
        provider_params={key: True if key == 'breakeven_stop' else 2}, symbol='BTCUSDT', market='futures',
        timeframe='15m', start_at=1767225600, end_at=1767232800)}
    response = TestClient(p.app).post('/cutie/backtest', json=req).json()
    assert response['error_type'] == 'INVALID_PARAMS'
    assert key in response['error_message']
