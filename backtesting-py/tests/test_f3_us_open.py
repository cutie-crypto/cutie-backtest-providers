"""Registered-route proof with independently specified UTC boundaries."""
from pathlib import Path
import sys
from unittest.mock import patch
import tempfile

import pandas as pd
import pytest
from backtesting import Backtest
from fastapi.testclient import TestClient
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p

NAME = 'us_open_momentum'


def frame(day='2026-03-09', start='13:30', side='long'):
    data = pd.DataFrame(dict(Open=100., High=102., Low=98., Close=100., Volume=100.),
        index=pd.date_range(day, periods=96, freq='15min'))
    t = pd.Timestamp(day+' '+start)
    data.loc[t, ['Open', 'Close']] = [100, 101 if side == 'long' else 99]
    data.loc[t+pd.Timedelta(minutes=15), 'Close'] = 100.5 if side == 'long' else 99.5
    data.loc[t+pd.Timedelta(minutes=30):, ['High', 'Low']] = [101, 99]
    # The first Close gives opposite/no momentum: the baseline must be first Open.
    return data


def run(data, values=None):
    built = p.TOOL_SPECS['local.backtesting_py.'+NAME]['build'](values or {})
    return Backtest(data, built['strategy'], cash=10000, exclusive_orders=True,
                    finalize_trades=True).run()


def response(data, values=None, name=NAME, timeframe='15m', market='futures'):
    body = {'backtest': dict(run_id='calendar-proof', provider_tool_id='local.backtesting_py.'+name,
        provider_params=values or {}, symbol='BTCUSDT', market=market, timeframe=timeframe,
        start_at=int(data.index[0].timestamp()), end_at=int(data.index[-1].timestamp())+900,
        initial_capital='10000', fee_bps='0', slippage_bps='0')}
    with tempfile.TemporaryDirectory() as reports, patch.object(p, 'AUTH_TOKEN', ''), \
         patch.object(p, 'REPORTS_DIR', Path(reports)), \
         patch.object(p, '_fetch_ohlcv', return_value=data) as fetch, \
         patch.object(p, '_fetch_template_warmup', return_value=data.iloc[:0]), \
         patch.object(Backtest, 'plot', return_value=None):
        result = TestClient(p.app).post('/cutie/backtest', json=body).json()
        if name == 'cme_weekend_gap':
            assert fetch.call_args.args[1] == 'spot'
        return result


@pytest.mark.parametrize('day,start,entry,exit_', [
    ('2026-03-09', '13:30', '14:00', '20:00'),
    ('2026-11-02', '14:30', '15:00', '21:00'),
])
@pytest.mark.parametrize('side', ['long', 'short'])
def test_dst_window_first_open_and_close_flatten(day, start, entry, exit_, side):
    result = response(frame(day, start, side))
    assert result['result_status'] == 'success', result
    trades = result['trades']
    assert len(trades) == 1
    assert trades[0]['opened_at'] == int(pd.Timestamp(day+' '+entry).timestamp())
    assert trades[0]['closed_at'] == int(pd.Timestamp(day+' '+exit_).timestamp())
    assert trades[0]['side'] == side
    evidence = result['raw_report'][NAME]['entries'][0]
    assert evidence['window_start'] == int(pd.Timestamp(day+' '+start).timestamp())
    assert evidence['stop'] == (98 if side == 'long' else 102)
    assert 'regular_calendar' in result['assumptions']['calendar_template']


@pytest.mark.parametrize('day', ['2026-03-07', '2026-03-08'])
def test_weekend_no_trade(day):
    assert run(frame(day))['_trades'].empty


def test_opposite_extreme_stop_high_low_and_no_second_entry():
    data = frame()
    data.loc['2026-03-09 14:15', 'Low'] = 97
    stats = run(data)
    assert len(stats['_trades']) == 1
    assert stats['_trades'].iloc[0].ExitTime == pd.Timestamp('2026-03-09 14:30')
    assert stats['_strategy']._risk_exit_reason == 'window_stop'


@pytest.mark.parametrize('side,opening', [('long', 97), ('short', 103)])
def test_gap_wrong_side_skips_at_fill(side, opening):
    data = frame(side=side)
    data.loc['2026-03-09 14:00', ['Open', 'High', 'Low', 'Close']] = [opening, opening+1, opening-1, opening]
    result = response(data)
    assert result['result_status'] == 'success', result
    assert not result['trades']
    assert result['raw_report'][NAME]['skipped'][0]['reason'] == 'stop_wrong_side_of_fill'


@pytest.mark.parametrize('values,tf,market', [
    ({'window_minutes': 20}, '15m', 'futures'), ({'window_minutes': 60}, '1h', 'futures'),
    ({}, '4h', 'futures'), ({'direction': 'short'}, '15m', 'spot'),
    ({'direction': 'both'}, '15m', 'spot'), ({}, '15m', 'spot'),
    ({'filter_layer_enabled': True, 'filter_ema_enabled': True}, '15m', 'futures'),
])
def test_invalid_before_fetch(monkeypatch, values, tf, market):
    def forbidden(*args):
        pytest.fail('invalid params fetched data')
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, '_fetch_ohlcv', forbidden)
    body = {'backtest': dict(run_id='reject', provider_tool_id='local.backtesting_py.'+NAME,
        provider_params=values, symbol='BTCUSDT', market=market, timeframe=tf,
        start_at=1773014400, end_at=1773100800, initial_capital='10000')}
    assert TestClient(p.app).post('/cutie/backtest', json=body).json()['error_type'] == 'INVALID_PARAMS'


def test_missing_window_and_last_bar_no_entry():
    data = frame().drop(pd.Timestamp('2026-03-09 13:30'))
    result = response(data)
    assert not result['trades']
    assert result['raw_report'][NAME]['skipped'][0]['reason'] == 'window_bar_missing'
    assert run(frame().loc[:'2026-03-09 13:45'])['_trades'].empty


def test_direction_long_spot_and_filter_and_time_gates():
    data = frame()
    assert response(data, {'direction': 'long'}, market='spot')['trades']
    assert not response(data, dict(direction='long', time_layer_enabled=True,
        time_session_start='15:00', time_session_end='16:00'))['trades']
    assert not response(data, dict(direction='long', filter_layer_enabled=True,
        filter_ema_enabled=True, filter_ema_period=2))['trades']


def test_leverage_one_off_state_bytes():
    from canonical_json import canonical_json
    data = frame()
    before = response(data, {'direction': 'long'}, market='spot')
    after = response(data, {'direction': 'long', 'leverage': 1}, market='spot')
    keys = ('schema_version', 'trades', 'equity_curve', 'metrics', 'data_manifest')
    assert before['trades']
    assert canonical_json({k: before[k] for k in keys}) == canonical_json({k: after[k] for k in keys})
    import json
    for key in ('assumptions', 'raw_report'):
        assert json.dumps(before[key], sort_keys=True) == json.dumps(after[key], sort_keys=True)


def test_user_stop_closer_than_liquidation_wins_with_wide_window():
    data = frame()
    data.loc['2026-03-09 13:30':'2026-03-09 13:45', 'Low'] = 80
    data.loc['2026-03-09 14:00', ['Open','High','Low','Close']] = [100,101,90,100]
    built = p.TOOL_SPECS['local.backtesting_py.'+NAME]['build'](
        {'leverage': 10, 'stop_loss_pct': 2, 'risk_layer_enabled': True, 'position_size_pct': 20})
    stats = Backtest(data, built['strategy'], cash=10000, margin=.1,
        exclusive_orders=True, finalize_trades=True).run()
    assert len(stats['_trades']) == 1
    assert stats['_strategy']._risk_exit_reason == 'stop_loss'
    assert not stats['_strategy']._isolated_liquidations
    assert stats['_trades'].iloc[0].ExitTime == pd.Timestamp('2026-03-09 14:15')


@pytest.mark.parametrize('side', ['long', 'short'])
@pytest.mark.parametrize('missing', [True, False])
def test_entry_fill_bar_must_exist_without_postponement(side, missing):
    # NY DST: window13:30–14:00 UTC; expected fill1773064800 (14:00).
    # With that bar removed, the broker first sees1773065700 (14:15).
    data = frame(side=side)
    if missing:
        data = data.drop(pd.Timestamp('2026-03-09 14:00'))
    result = response(data)
    assert result['result_status'] == 'success', result
    report = result['raw_report'][NAME]
    assert len(report['entries']) == 1
    assert report['entries'][0]['decision_at'] == 1773064800
    if missing:
        assert result['trades'] == []
        assert report['skipped'] == [dict(reason='entry_fill_bar_missing', at=1773065700)]
    else:
        assert len(result['trades']) == 1
        assert result['trades'][0]['opened_at'] == 1773064800
        assert report['skipped'] == []
