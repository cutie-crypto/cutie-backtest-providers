"""F6: hand expectations, actual engine and registered HTTP route (offline OHLCV)."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from backtesting import Backtest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as provider
from strategy_time_layer import TimeContext

TOOL = 'local.backtesting_py.red_streak_rsi'


def frame():
    # Four red closes at 40..43. No earlier changes: RSI=50 before 40,
    # then all gains=0, losses>0 => RSI=0. Signal 43, next-open entry 44.
    close = [100.] * 40 + [98., 96., 94., 92.] + [92.] * 26
    opens = close.copy()
    for i in range(40, 44):
        opens[i] = close[i] + 1
    opens[44] = 93.
    return pd.DataFrame(dict(Open=opens, Close=close,
        High=[max(o,c)+.1 for o,c in zip(opens,close)],
        Low=[min(o,c)-.1 for o,c in zip(opens,close)], Volume=[1]*70),
        index=pd.date_range('2026-01-01', periods=70, freq='h'))


def run(params=None, data=None, warm=None):
    data = frame() if data is None else data
    built = provider.TOOL_SPECS[TOOL]['build'](params or {})
    cls = built['strategy']
    if warm is not None:
        cls._warmup_bars = len(warm)
        cls._warmup_cols = {c: warm[c].to_numpy() for c in provider._WARMUP_COLUMNS}
    if cls._time_config:
        cls._time_context = TimeContext.build(cls._time_config, '1h', data.index)
    return Backtest(data, cls, cash=100000, exclusive_orders=True, finalize_trades=False).run()


def response(monkeypatch, tmp_path, params=None, data=None, market='spot'):
    data = frame() if data is None else data
    monkeypatch.setattr(provider, 'AUTH_TOKEN', '')
    monkeypatch.setattr(provider, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(provider, '_fetch_ohlcv', lambda *a: data)
    monkeypatch.setattr(provider, '_fetch_template_warmup', lambda *a: data.iloc[:0])
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **kw: None)
    request = dict(run_id='f6', provider_tool_id=TOOL, provider_params=params or {},
        symbol='BTCUSDT', market=market, timeframe='1h', start_at=int(data.index[0].timestamp()),
        end_at=int(data.index[-1].timestamp())+3600, initial_capital='10000', fee_bps='0', slippage_bps='0')
    return TestClient(provider.app).post('/cutie/backtest', json={'backtest': request}).json()


def test_default_hand_entry_exit_prices_and_rsi():
    stats = run()
    trades = stats['_trades']
    assert list(trades.EntryBar) == [44]
    assert list(trades.ExitBar) == [56]  # 44 is 1, 55 is 12; fill at 56.
    assert list(trades.EntryPrice) == [93]
    assert list(trades.ExitPrice) == [92]
    assert (trades.Size > 0).all()
    assert list(stats['_strategy'].rsi[:40]) == [50]*40
    assert list(stats['_strategy'].rsi[40:]) == [0]*30
    assert stats['_strategy']._risk_exit_reason == 'time_expiry'


@pytest.mark.parametrize('holding', [1, 2, 5, 12])
@pytest.mark.parametrize('enabled', [False, True])
def test_shared_holding_count(holding, enabled):
    stats = run({'max_holding_bars': holding, 'risk_layer_enabled': enabled})
    assert list(stats['_trades'].ExitBar) == [44+holding]


def test_nth_rsi_not_previous_and_strict_boundary(monkeypatch):
    values = np.full(70, 30.)
    values[43] = 29.
    monkeypatch.setattr(provider, '_rsi_series', lambda *a: values)
    assert list(run()['_trades'].EntryBar) == [44]
    values[43] = 30.
    assert run()['_trades'].empty


@pytest.mark.parametrize('broken_bar', [40, 41, 42, 43])
def test_doji_breaks_streak(broken_bar):
    data = frame()
    data.iloc[broken_bar, data.columns.get_loc('Open')] = data.Close.iloc[broken_bar]
    data.loc[data.index[44], 'Open'] = 92
    assert run(data=data)['_trades'].empty


def test_rsi_at_nth_only_does_not_wait_for_later_red_bar(monkeypatch):
    data = frame()
    data.loc[data.index[44], 'Open'] = 93
    values = np.full(70, 30.)
    values[44] = 29.
    monkeypatch.setattr(provider, '_rsi_series', lambda *a: values)
    assert run(data=data)['_trades'].empty


@pytest.mark.parametrize('red_bars,entry', [(3,43), (4,44), (8,48)])
def test_configured_streak_length(red_bars, entry):
    data = frame()
    if red_bars == 8:
        for i in range(44,48):
            data.loc[data.index[i], 'Open'] = 93
    assert list(run({'red_bars':red_bars}, data)['_trades'].EntryBar) == [entry]


def test_user_stop_suppresses_defaults_and_changes_actual_exit():
    data = frame()
    data.loc[data.index[45], ['Open','High','Low','Close']] = [92, 95, 91, 95]
    stats = run({'stop_loss_pct': 1}, data)
    assert stats['_strategy']._risk['stop_loss_pct'] == .01
    assert 'take_profit_pct' not in stats['_strategy']._risk
    assert list(stats['_trades'].ExitBar) == [56]


def test_user_target_suppresses_default_stop():
    risk = provider.TOOL_SPECS[TOOL]['build']({'take_profit_pct': 8})['strategy']._risk
    assert risk['take_profit_pct'] == .08
    assert 'stop_loss_pct' not in risk


@pytest.mark.parametrize('enabled', [False, True])
def test_frozen_signal_stop_used_instead_of_fill_stop(enabled):
    data = frame()
    data.loc[data.index[44], ['Open','High','Low','Close']] = [93,93,89.9,90]
    # Signal stop=92*.97=89.24. Fill-based stop=90.21 would exit early.
    stats = run({'risk_layer_enabled':enabled}, data)
    assert list(stats['_trades'].ExitBar) == [56]


@pytest.mark.parametrize('kind,reason', [('stop','stop_loss'), ('expiry','time_expiry'), ('target','take_profit')])
def test_same_bar_priority(kind, reason):
    data = frame()
    when = 44 if kind == 'target' else 45
    data.loc[data.index[when], ['High','Low']] = [96,88 if kind == 'stop' else 91]
    stats = run({'risk_layer_enabled': True, 'max_holding_bars': 2}, data)
    assert stats['_strategy']._risk_exit_reason == reason
    assert list(stats['_trades'].ExitBar) == [when+1]


@pytest.mark.parametrize('entry_open', [89, 89.24])
def test_gap_cancels_before_fill_and_records_http(monkeypatch,tmp_path,entry_open):
    data = frame()
    data.loc[data.index[44], ['Open','High','Low','Close']] = [entry_open,92,88,92]
    body = response(monkeypatch,tmp_path,data=data)
    assert body['result_status'] == 'success', body
    assert body['trades'] == []
    skip = body['raw_report']['red_streak_rsi']['skipped_entries']
    assert len(skip) == 1
    assert skip[0]['reason'] == 'frozen_stop_wrong_side_of_entry_open'
    assert float(skip[0]['frozen_stop']) == pytest.approx(89.24)


def test_http_catalog_result_keys_and_assumptions(monkeypatch,tmp_path):
    body = response(monkeypatch,tmp_path)
    assert body['result_status'] == 'success', body
    assert len(body['trades']) == 1
    assert body['trades'][0]['opened_at'] == int(frame().index[44].timestamp())
    assert body['trades'][0]['closed_at'] == int(frame().index[56].timestamp())
    layer = body['assumptions']['red_streak_rsi']
    assert layer['holding_bar_count_from'] == 'entry_fill_bar_is_1'
    assert layer['max_holding_bars'] == 12
    assert layer['entry_fill'] == layer['exit_fill'] == 'next_bar_open_market'
    assert body['raw_report']['red_streak_rsi']['skipped_entries'] == []
    import test_time_layer_compatibility as compat
    old = compat.capture.response('rsi_reversal', {})
    assert set(body['trades'][0]) == set(old['trades'][0])
    for key in ('metrics','data_manifest'):
        assert set(body[key]) == set(old[key])


def test_time_gate_and_shared_holding(monkeypatch,tmp_path):
    data=frame()
    # 43 closes at Jan 2 20:00 UTC; reject a disjoint session.
    rejected=response(monkeypatch,tmp_path,{'time_layer_enabled':True,
        'time_session_start':'06:00','time_session_end':'10:00'},data)
    assert rejected['trades'] == []
    accepted=response(monkeypatch,tmp_path,{'time_layer_enabled':True},data)
    assert len(accepted['trades']) == 1
    assert accepted['assumptions']['time_layer']['holding']['bars'] == 12


@pytest.mark.parametrize('params', [
    {'red_bars':2},{'red_bars':9},{'red_bars':4.0},{'red_bars':True},
    {'rsi_period':1},{'rsi_period':101},{'rsi_period':14.5},
    {'oversold':0},{'oversold':50},{'oversold':'30'},
    {'max_holding_bars':0},{'max_holding_bars':1.0},{'max_holding_bars':True},
    {'stop_loss_pct':0},{'stop_loss_pct':100},
    {'direction':'short'},{'direction':'both'},
    {'time_session_start':'10:00'}, {'atr_stop_multiplier':2},
])
def test_http_invalid_before_fetch(monkeypatch,params):
    monkeypatch.setattr(provider,'AUTH_TOKEN','')
    monkeypatch.setattr(provider,'_fetch_ohlcv',lambda *a: pytest.fail('invalid must fail before fetch'))
    request=dict(run_id='invalid',provider_tool_id=TOOL,provider_params=params,symbol='BTCUSDT',
        market='spot',timeframe='1h',start_at=1704067200,end_at=1704672000,
        initial_capital='10000',fee_bps='0',slippage_bps='0')
    body=TestClient(provider.app).post('/cutie/backtest',json={'backtest':request}).json()
    assert body['error_type']=='INVALID_PARAMS',body


def test_warmup_streak_counts_closed_prefix():
    data = frame()
    # The first 42 bars are fetched history; main bars 0,1 finish the four-red streak.
    stats = run(data=data.iloc[42:], warm=data.iloc[:42])
    assert list(stats['_trades'].EntryBar) == [2]
    assert list(stats['_trades'].ExitBar) == [14]


def test_no_signal_before_indicator_warmup():
    data = frame()
    data.loc[:, 'Open'] = data.Close
    for i in range(3,7):
        data.loc[data.index[i], 'Open'] = data.Close.iloc[i] + 1
    assert run(data=data)['_trades'].empty


def test_no_tail_entry_without_next_open(monkeypatch,tmp_path):
    body=response(monkeypatch,tmp_path,data=frame().iloc[:44])
    assert body['trades'] == []


def test_rsi_nonzero_independent_wilder_recurrence():
    data=frame()
    closes=[100.+((i*7)%13) for i in range(70)]
    data.loc[:, 'Close']=closes
    data.loc[:, 'Open']=closes
    data.loc[:, 'High']=[v+1 for v in closes]
    data.loc[:, 'Low']=[v-1 for v in closes]
    gain=loss=None
    expected=[50.]
    for a,b in zip(closes,closes[1:]):
        g,l=max(b-a,0),max(a-b,0)
        gain=g if gain is None else (13*gain+g)/14
        loss=l if loss is None else (13*loss+l)/14
        expected.append(100-100/(1+gain/loss) if loss else 100 if gain else 50)
    assert list(run(data=data)['_strategy'].rsi) == pytest.approx(expected,abs=1e-10)


def test_user_atr_and_r_target_defaults_not_injected():
    risk=provider.TOOL_SPECS[TOOL]['build']({'risk_layer_enabled':True,
        'risk_atr_period':14,'atr_stop_multiplier':2,'take_profit_r':3})['strategy']._risk
    assert 'stop_loss_pct' not in risk and 'take_profit_pct' not in risk
    assert risk['max_holding_bars']==12


def test_enabled_risk_discloses_signal_frozen_levels(monkeypatch,tmp_path):
    body=response(monkeypatch,tmp_path,{'risk_layer_enabled':True})
    assert body['assumptions']['risk_layer']['initial_levels_based_on']=='entry_signal_close'
    assert body['assumptions']['red_streak_rsi']['stop_loss_pct']==3
