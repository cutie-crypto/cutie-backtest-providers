"""Registered long-only templates: hand-calculated geometry, fills and exits."""
from pathlib import Path
import hashlib
import sys
from decimal import Decimal

import pandas as pd
import pytest
from backtesting import Backtest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as provider
from canonical_json import canonical_json
from strategy_time_layer import TimeConfig, TimeContext
from test_time_layer import DEFAULTS

NAMES = ('bullish_engulfing', 'hammer_pin_bar')


def frame(name, *, signal=25, size=70, background=100):
    data = pd.DataFrame(dict(Open=[float(background)]*size, High=[background+.5]*size,
                             Low=[background-.5]*size, Close=[float(background)]*size, Volume=[1]*size),
                        index=pd.date_range('2026-01-01', periods=size, freq='h'))
    if name == 'bullish_engulfing':
        data.iloc[signal-1, :4] = [102, 103, 98, 100]
        data.iloc[signal, :4] = [99, 105, 97, 104]
    else:
        data.iloc[signal, :4] = [102, 104, 97, 103]
    data.iloc[signal+1, :4] = [106, 107, 105, 106]
    return data


def compatibility_frame(name):
    # Signal at 05:00, entry 06:00, exit trigger 09:00, fill outside session 10:00.
    data = frame(name, signal=29, size=120)
    data.iloc[31:34, :4] = [110, 120, 105, 110]
    data.iloc[33, :4] = [110, 125, 105, 110]
    data.iloc[34:, :4] = [110, 111, 109, 110]
    return data


def run(name, data=None, params=None, *, warm=0, finalize=False):
    data = frame(name) if data is None else data
    built = provider.TOOL_SPECS['local.backtesting_py.'+name]['build'](params or {})
    cls = built['strategy']
    if warm:
        cls._warmup_bars = warm
        cls._warmup_cols = {c: data[c].iloc[:warm].to_numpy() for c in provider._WARMUP_COLUMNS}
        data = data.iloc[warm:].copy()
    if cls._time_config is not None:
        cls._time_context = TimeContext.build(cls._time_config, '1h', data.index)
    return Backtest(data, cls, cash=100000, exclusive_orders=True, finalize_trades=finalize).run()


def request(name, params, data):
    return {'backtest': dict(run_id='9t1', provider_tool_id='local.backtesting_py.'+name,
        provider_params=params, symbol='BTCUSDT', market='spot', timeframe='1h',
        start_at=int(data.index[0].timestamp()), end_at=int(data.index[-1].timestamp())+3600,
        initial_capital='10000', fee_bps='0', slippage_bps='0')}


def http(monkeypatch, tmp_path, name, data, params=None):
    monkeypatch.setattr(provider, 'AUTH_TOKEN', '')
    monkeypatch.setattr(provider, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(provider, '_fetch_ohlcv', lambda *a: data.copy())
    monkeypatch.setattr(provider, '_fetch_template_warmup', lambda *a: data.iloc[:0].copy())
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **kw: None)
    body = TestClient(provider.app).post('/cutie/backtest', json=request(name, params or {}, data)).json()
    assert body['result_status'] == 'success', body
    return body


@pytest.mark.parametrize('name', NAMES)
@pytest.mark.parametrize('valid', [True, False])
def test_hand_shape_and_next_open(name, valid):
    data = frame(name)
    if not valid:
        if name == 'bullish_engulfing':
            # Prior body 2; 2.399 < 2*1.2 despite enclosing the prior body.
            data.iloc[25, :4] = [99.7, 105, 97, 102.099]
        else:
            # Lower=5, body=1, upper=1.06 > 7.06*.15=1.059.
            data.iloc[25, :4] = [102, 104.06, 97, 103]
    result = run(name, data, {'position_filter': False})
    trades = result['_strategy'].trades
    assert len(trades) == int(valid)
    if valid:
        assert trades[0].entry_bar == 26
        assert trades[0].entry_price == 106
        assert trades[0].tag.stop == pytest.approx(96.903)


@pytest.mark.parametrize('name', NAMES)
@pytest.mark.parametrize('enabled', [True, False])
def test_position_filter_switch(name, enabled):
    # Prior lows near 89.5; BB/EMA remain below the pattern's [97, 105].
    result = run(name, frame(name, background=90), {'position_filter': enabled})
    assert len(result['_trades']) == int(not enabled)
    if not enabled:
        assert result['_trades'].iloc[0].EntryBar == 26


@pytest.mark.parametrize('name', NAMES)
def test_position_filter_alternative_indicator_touch(name):
    data = frame(name)
    data.loc[data.index[:24], 'Low'] = 90
    # The new low condition is false. EMA20 = 100 + 3*2/21 for hammer;
    # engulf BB20 lower = 100.2 - 2*sqrt(.76), both lie in the pattern range.
    result = run(name, data)
    assert result['_strategy'].trades[0].entry_bar == 26


@pytest.mark.parametrize('name', NAMES)
@pytest.mark.parametrize('r', [2, 3])
@pytest.mark.parametrize('hit', [True, False])
def test_r_target_from_actual_entry(name, r, hit):
    # Entry 106; stop 97*.999=96.903; distance 9.097.
    target = 124.194 if r == 2 else 133.291
    data = frame(name)
    data.iloc[27, :4] = [110, target + (.001 if hit else -.001), 105, 110]
    data.iloc[28, :4] = [112, 113, 111, 112]
    result = run(name, data, {'reward_r': r})
    trades = result['_trades']
    assert len(trades) == int(hit)
    if hit:
        assert trades.iloc[0].EntryBar == 26
        assert trades.iloc[0].ExitBar == 28
        assert trades.iloc[0].ExitPrice == 112  # market fill, never target
    else:
        assert result['_strategy'].position


@pytest.mark.parametrize('name', NAMES)
@pytest.mark.parametrize('low,hit', [(96.902, True), (96.904, False), (96.95, False)])
def test_buffered_frozen_stop(name, low, hit):
    data = frame(name)
    data.iloc[27, :4] = [106, 107, low, 106]
    data.iloc[28, :4] = [101, 102, 100, 101]
    result = run(name, data)
    assert len(result['_trades']) == int(hit)
    if hit:
        assert result['_trades'].iloc[0].ExitBar == 28
        assert result['_trades'].iloc[0].ExitPrice == 101


@pytest.mark.parametrize('name', NAMES)
@pytest.mark.parametrize('opening,skip', [(96.8, True), (96.903, True), (96.904, False)])
def test_gap_skips_before_building_position(name, opening, skip):
    data = frame(name)
    data.iloc[26, :4] = [opening, 110, opening, 106]
    result = run(name, data)
    report = result['_strategy'].pattern_report
    assert report['skipped_entry_count'] == int(skip)
    assert len(result['_trades']) == int(not skip)
    if not skip:
        assert result['_trades'].iloc[0].EntryBar == 26
        assert result['_trades'].iloc[0].ExitBar == 27  # tiny risk makes this bar hit 2R
    if skip:
        assert report['skipped_entries'] == [dict(reason='entry_open_at_or_below_frozen_stop',
            signal_bar=25, entry_bar=26, entry_open=opening, frozen_stop=pytest.approx(96.903))]


@pytest.mark.parametrize('name', NAMES)
def test_prefix_and_warmup_do_not_read_future(name):
    data = frame(name)
    prefix = run(name, data.iloc[:27])['_strategy'].trades[0]
    data.iloc[35:, :4] = [1, 2, .5, 1]
    full = run(name, data)['_trades'].iloc[0]
    assert (prefix.entry_bar, prefix.entry_price, prefix.tag.stop) == (full.EntryBar, full.EntryPrice, full.Tag.stop)
    warm = run(name, frame(name), warm=24)['_strategy'].trades[0]
    assert (warm.entry_bar, warm.entry_price, warm.tag.stop) == (2, 106, pytest.approx(96.903))


@pytest.mark.parametrize('name', NAMES)
def test_tail_never_backfills_new_entry(name):
    result = run(name, frame(name).iloc[:26], finalize=True)
    assert result['_trades'].empty and not result['_strategy'].position


@pytest.mark.parametrize('name', NAMES)
@pytest.mark.parametrize('params', [dict(direction='short'), dict(direction='both'), dict(stop_loss_pct=5),
    dict(take_profit_pct=5), dict(risk_layer_enabled=True, take_profit_r=2),
    dict(risk_layer_enabled=True, trailing_stop_pct=3), dict(reward_r=0), dict(reward_r=21),
    dict(reward_r=True), dict(reward_r='2'), dict(position_filter=1),
    dict(time_session_start='06:00'), dict(atr_stop_multiplier=1)])
def test_invalid_params_before_fetch(monkeypatch, name, params):
    monkeypatch.setattr(provider, 'AUTH_TOKEN', '')
    def forbidden(*a):
        pytest.fail('invalid parameters fetched market data')
    monkeypatch.setattr(provider, '_fetch_ohlcv', forbidden)
    monkeypatch.setattr(provider, '_fetch_template_warmup', forbidden)
    body = TestClient(provider.app).post('/cutie/backtest', json=request(name, params, frame(name))).json()
    assert body['error_type'] == 'INVALID_PARAMS', body


@pytest.mark.parametrize('name', NAMES)
def test_http_report_and_frozen_v2_keys(monkeypatch, tmp_path, name):
    data = frame(name)
    data.iloc[26, :4] = [96.8, 110, 96.8, 106]
    body = http(monkeypatch, tmp_path, name, data)
    assert not body['trades']
    assert body['raw_report']['candle_pattern']['skipped_entry_count'] == 1
    assert 'next bar open' in body['assumptions']['pattern_execution']
    assert set(body['metrics']) == {'total_return', 'max_drawdown', 'trade_count'}
    normal = http(monkeypatch, tmp_path, name, compatibility_frame(name))
    assert len(normal['trades']) == 1
    # Compare canonical result.v2 against the same existing serializer, not a new contract.
    result = run(name, compatibility_frame(name), finalize=True)
    v2 = provider._build_result_v2(stats_trades=result['_trades'], equity_scale_dec=Decimal('.1'),
        fee_bps=Decimal(0), slippage_bps=Decimal(0), initial_capital=Decimal(10000),
        start_at=int(data.index[0].timestamp()), end_at=int(data.index[-1].timestamp())+3600,
        symbol='BTCUSDT', market='spot', timeframe='1h', exchange_id='binance', df=data)
    assert set(normal['metrics']) == set(v2['metrics'])
    assert set(normal['trades'][0]) == set(v2['trades'][0])
    assert set(normal['data_manifest']) == set(v2['data_manifest'])


@pytest.mark.parametrize('name', NAMES)
def test_time_decorator_gates_real_entries(name):
    data = compatibility_frame(name)
    allowed = run(name, data, dict(time_layer_enabled=True, time_session_start='06:00', time_session_end='10:00'))
    denied = run(name, data, dict(time_layer_enabled=True, time_session_start='07:00', time_session_end='10:00'))
    assert allowed['_trades'].iloc[0].EntryBar == 30
    assert allowed['_trades'].iloc[0].ExitBar == 34
    assert denied['_trades'].empty and not denied['_strategy'].position


@pytest.mark.parametrize('name', NAMES)
def test_disabled_defaults_exact_trades_equity_v2(name):
    def fingerprint(extra):
        data = compatibility_frame(name)
        result = run(name, data, extra, finalize=True)
        assert len(result['_trades']) == 1
        v2 = provider._build_result_v2(stats_trades=result['_trades'], equity_scale_dec=Decimal('.1'),
            fee_bps=Decimal(0), slippage_bps=Decimal(0), initial_capital=Decimal(10000),
            start_at=int(data.index[0].timestamp()), end_at=int(data.index[-1].timestamp())+3600,
            symbol='BTCUSDT', market='spot', timeframe='1h', exchange_id='binance', df=data)
        return (result['_trades'].to_json(), result['_equity_curve'].to_json(), canonical_json(v2))
    assert fingerprint({}) == fingerprint({**DEFAULTS, 'risk_layer_enabled': False,
        'atr_stop_multiplier': 0, 'risk_atr_period': 0, 'take_profit_r': 0, 'leverage': 1})


@pytest.mark.parametrize('name', NAMES)
def test_extreme_filter_excludes_future_lows(name):
    data = frame(name, background=120)
    data.loc[data.index[:25], 'Low'] = 100
    if name == 'bullish_engulfing':
        data.iloc[24, :4] = [122, 123, 100, 120]
        data.iloc[25, :4] = [119, 125, 119, 124]
        data.iloc[26:, :4] = [126, 127, 125, 126]
        # Anchor 100 from prior candle; BB20 lower=118.456 < current Low=119.
        expected_stop = 99.9
    else:
        # Prior EMA20/60 remain above 104; only the strict new low can pass.
        expected_stop = 96.903
    prefix = run(name, data.iloc[:27])['_strategy'].trades[0]
    data.iloc[40:, :4] = [1, 2, .5, 1]
    full = run(name, data)['_trades'].iloc[0]
    assert prefix.tag.stop == pytest.approx(expected_stop)
    assert (full.EntryBar, full.EntryPrice, full.Tag.stop) == (prefix.entry_bar, prefix.entry_price, prefix.tag.stop)


@pytest.mark.parametrize('name', NAMES)
def test_stop_priority_and_holding_expiry(name):
    data = frame(name)
    data.iloc[27, :4] = [110, 130, 96, 110]  # both frozen levels touched
    result = run(name, data)
    assert result['_trades'].iloc[0].ExitBar == 28
    assert result['_strategy']._risk_exit_reason == 'stop_loss'
    expiry = run(name, compatibility_frame(name),
                 dict(risk_layer_enabled=True, max_holding_bars=1))
    assert expiry['_trades'].iloc[0].ExitBar == 31
    assert expiry['_strategy']._risk_exit_reason == 'time_expiry'


@pytest.mark.parametrize('name', NAMES)
@pytest.mark.parametrize('warm', [False, True])
@pytest.mark.parametrize('disabled', [False, True])
def test_frozen_off_http_bytes(name, warm, disabled):
    import test_time_layer_compatibility as time_compat
    capture = time_compat.capture
    expected = time_compat.PATTERN_BASELINE['single'][name][str(int(warm))]
    params = {**DEFAULTS, 'risk_layer_enabled': False, 'atr_stop_multiplier': 0,
              'risk_atr_period': 0, 'take_profit_r': 0} if disabled else {}
    body = capture.response(name, params, warm)
    assert len(body['trades']) == expected['trade_count'] == 1
    assert capture.digest(body['trades']) == expected['trades_sha256']
    assert capture.digest(body['equity_curve']) == expected['equity_sha256']
    assert hashlib.sha256(canonical_json({key: body[key] for key in capture.V2_KEYS}).encode()).hexdigest() == expected['result_v2_sha256']
