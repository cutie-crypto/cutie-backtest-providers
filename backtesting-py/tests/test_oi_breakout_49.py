"""OI49: hand-calculated HTTP fills and causal central-series boundaries (no network)."""
import sys
from pathlib import Path
import pandas as pd
from backtesting import Backtest
from fastapi.testclient import TestClient
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p
import strategy_oi_breakout as oi

TOOL = 'local.backtesting_py.oi_breakout'
T0 = 1768435200  # 2026-01-15 UTC
PARAMS = dict(breakout_lookback_bars=5, oi_lookback_bars=2, exit_ema_period=2)
STEPS = {'1d': 86400, '1h': 3600}


def hand_frame(timeframe='1d', start=T0, stop=False, boundary=False):
    rows = [[100, 101, 99, 100, 1] for _ in range(10)]
    if boundary:
        rows[4] = [100, 106, 99, 105, 1]
    rows[5] = [100, 111, 99, 110, 1]
    rows[6] = [120, 122, 116 if stop else 119, 121, 1]
    rows[7] = [90 if stop else 120, 122, 89 if stop else 109, 110, 1]
    rows[8] = [105, 106, 104, 105, 1]
    return pd.DataFrame(rows, columns=['Open', 'High', 'Low', 'Close', 'Volume'],
                        index=pd.date_range(pd.to_datetime(start, unit='s'), periods=len(rows),
                                            freq=pd.Timedelta(seconds=STEPS[timeframe])))


def install(monkeypatch, tmp_path, data, values=None, *, timeframe='1d', drop=(), fail=False, warm=False):
    step = STEPS[timeframe]
    start = int(data.index[0].timestamp())
    values = {5: '105'} if values is None else values
    calls = []
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a, **k: data.copy())
    prefix = pd.DataFrame([[100, 101, 99, 100, 1]] * 5, columns=data.columns,
                          index=pd.date_range(pd.to_datetime(start - 5 * step, unit='s'), periods=5,
                                              freq=pd.Timedelta(seconds=step)))
    monkeypatch.setattr(p, '_fetch_template_warmup', lambda *a, **k: prefix.copy() if warm else data.iloc[:0].copy())
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **k: None)
    def fetch(**kwargs):
        calls.append(kwargs)
        assert (kwargs['symbol'], kwargs['exchange'], kwargs['metric'], kwargs['interval']) == (
            'BTC', 'Binance', 'open_interest', timeframe)
        if fail:
            raise RuntimeError('central offline')
        return [dict(ts=start + k * step, value=values.get(k, '100')) for k in range(-200, 11)
                if k not in drop and kwargs['start_at'] <= start + k * step <= kwargs['end_at']]
    monkeypatch.setattr(p, '_fetch_artifact_metric_chunk', fetch)
    return calls


def post(params=None, *, timeframe='1d', start=T0, symbol='BTCUSDT', market='spot'):
    res = TestClient(p.app).post('/cutie/backtest', json={'backtest': dict(
        run_id='oi49', provider_tool_id=TOOL, provider_params={**PARAMS, **(params or {})},
        symbol=symbol, market=market, timeframe=timeframe, start_at=start,
        end_at=start + 10 * STEPS.get(timeframe, 3600), initial_capital='10000', fee_bps='0', slippage_bps='0')})
    assert res.status_code == 200
    return res.json()


def run(monkeypatch, tmp_path, params=None, **kwargs):
    timeframe = kwargs.pop('timeframe', '1d')
    data = kwargs.pop('data', hand_frame(timeframe))
    install(monkeypatch, tmp_path, data, timeframe=timeframe, **kwargs)
    body = post(params, timeframe=timeframe, start=int(data.index[0].timestamp()))
    assert body['result_status'] == 'success', body
    return body


def test_entry_requires_price_breakout_and_increasing_oi(monkeypatch, tmp_path):
    for tf in STEPS:
        body = run(monkeypatch, tmp_path, timeframe=tf)
        assert len(body['trades']) == 1
        trade = body['trades'][0]
        assert trade['opened_at'] == T0 + 6 * STEPS[tf] and trade['entry_price'] == '120'
        for value in ('104.99', '95'):
            assert run(monkeypatch, tmp_path, timeframe=tf, values={5: value})['trades'] == []
        data = hand_frame(tf)
        data.iloc[5, :4] = [100, 101, 99, 101]  # equality is not a breakout
        data.iloc[6:, :4] = [100, 101, 99, 100]
        assert run(monkeypatch, tmp_path, timeframe=tf, data=data)['trades'] == []


def test_close_below_ema_exits_at_next_open(monkeypatch, tmp_path):
    body = run(monkeypatch, tmp_path, dict(stop_loss_pct=50))
    trade = body['trades'][0]
    assert (trade['closed_at'], trade['exit_price']) == (T0 + 8 * 86400, '105')
    assert body['raw_report']['oi_breakout']['executions'][0]['reason'] == 'close_below_ema'
    assert body['raw_report']['trade_exit_kinds'][0]['exit_kind'] == 'signal_exit'


def test_low_touch_stop_is_frozen_at_fill_and_wins_over_ema(monkeypatch, tmp_path):
    body = run(monkeypatch, tmp_path, data=hand_frame(stop=True))
    trade = body['trades'][0]
    # Actual fill 120 => stop 117.6; signal close 110 would incorrectly give stop 107.8.
    assert (trade['closed_at'], trade['exit_price']) == (T0 + 7 * 86400, '90')
    assert body['raw_report']['oi_breakout']['executions'][0]['reason'] == 'stop_loss'
    assert body['raw_report']['trade_exit_kinds'][0]['exit_kind'] == 'stop_loss'
    both = hand_frame()
    both.iloc[6, :4] = [120, 122, 108, 110]
    body = run(monkeypatch, tmp_path, data=both)
    assert body['raw_report']['oi_breakout']['executions'][0]['reason'] == 'stop_loss'


def test_any_oi_gap_fails_entire_run(monkeypatch, tmp_path):
    for tf in STEPS:
        for missing in (-2, 3, 9):
            install(monkeypatch, tmp_path, hand_frame(tf), timeframe=tf, drop=(missing,))
            body = post(timeframe=tf)
            assert body['error_type'] == 'TIME_DATA_GAP'
            assert body['limitations']['reason'] == 'open_interest_data_gap' and body['result_status'] == 'failed'
            limits = body['limitations']
            assert limits['required_first_date'] == p._utc_date(T0 - 2 * STEPS[tf])
            assert limits['required_last_date'] == p._utc_date(T0 + 9 * STEPS[tf])
            assert not {'required_first_ts', 'required_last_ts'} & set(limits)


def test_oi_unavailable_one_bar_before_publication(monkeypatch, tmp_path):
    for tf in STEPS:
        # ts=bar5 cannot confirm bar4, but is readable exactly at bar5 close.
        body = run(monkeypatch, tmp_path, timeframe=tf, data=hand_frame(tf, boundary=True), warm=True)
        assert body['trades'][0]['opened_at'] == T0 + 6 * STEPS[tf]
        entry = next(d for d in body['raw_report']['oi_breakout']['decisions'] if d['action'] == 'entry')
        assert entry['value_ts'] == T0 + 5 * STEPS[tf]
        assert entry['baseline_ts'] == T0 + 3 * STEPS[tf]
        assert entry['value_ts'] + STEPS[tf] == entry['decision_bar_open'] + STEPS[tf]


def test_schema_defaults_ranges_and_rejections(monkeypatch, tmp_path):
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    tool = next(t for t in TestClient(p.app).get('/catalog').json()['tools'] if t['tool_id'] == TOOL)
    assert (tool['name'], p.TOOL_SPECS[TOOL]['strategy_family'], tool['markets'], tool['timeframes']) == (
        'Local Spot Open-Interest Breakout', 'trend_following', ['spot'], ['1h', '1d'])
    assert '2026-01-08' in tool['description']
    expected = {
        'breakout_lookback_bars': ('integer', 20, 5, 200), 'oi_lookback_bars': ('integer', 20, 2, 200),
        'oi_increase_pct': ('number', 5, 0.1, 100), 'exit_ema_period': ('integer', 20, 2, 200),
        'stop_loss_pct': ('number', 2, 0.1, 50),
    }
    props = tool['param_schema']['properties']
    for key, (kind, default, lo, hi) in expected.items():
        assert props[key] == dict(type=kind, default=default, minimum=lo, maximum=hi)
        for bad in (lo - 0.01, hi + 1, True, '20'):
            assert post({key: bad})['error_type'] == 'INVALID_PARAMS'
    built = p._build_oi_breakout({})
    assert built['executed_name'] == 'Open Interest Breakout'
    assert built['oi_config'].values == {k: v[1] for k, v in expected.items()}
    for key in ('breakout_lookback_bars', 'oi_lookback_bars', 'exit_ema_period'):
        assert post({key: 5.5})['error_type'] == 'INVALID_PARAMS'
    assert '做空镜像未开放' in built['template_assumptions']['oi_breakout']['direction']


def test_illegal_market_symbol_timeframe_rejected_before_fetch(monkeypatch, tmp_path):
    calls = install(monkeypatch, tmp_path, hand_frame())
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a, **k: (_ for _ in ()).throw(AssertionError('must reject first')))
    for symbol in ('BTC', 'BTCUSD', 'BTCUSDT:USDT', ''):
        assert post(symbol=symbol)['error_type'] == 'INVALID_PARAMS'
    assert post(market='futures')['error_type'] == 'INVALID_PARAMS'
    assert post(timeframe='4h')['error_type'] == 'TIMEFRAME_UNSUPPORTED'
    assert post(dict(direction='short'))['error_type'] == 'INVALID_PARAMS'
    assert calls == []


def test_history_transport_and_sizing_evidence(monkeypatch, tmp_path):
    data = hand_frame('1h', start=oi.HOURLY_EARLIEST_TS)
    calls = install(monkeypatch, tmp_path, data, timeframe='1h')
    body = post(timeframe='1h', start=oi.HOURLY_EARLIEST_TS)
    assert body['limitations']['reason'] == 'open_interest_history_unavailable' and calls == []
    install(monkeypatch, tmp_path, hand_frame(), fail=True)
    assert post()['limitations']['reason'] == 'open_interest_history_unavailable'
    body = run(monkeypatch, tmp_path, dict(position_size_notional=1200))
    assert p.Decimal(body['trades'][0]['qty']) == 10
    s = body['assumptions']['oi_breakout']['open_interest_series']
    assert (s['symbol'], s['exchange'], s['metric'], s['unit'], s['interval']) == (
        'BTC', 'Binance', 'open_interest', 'usd', '1d')
    assert s['count'] == 12 and s['gap_policy'] == 'fail_closed'
    assert set(body['trades'][0]) == {'seq', 'side', 'opened_at', 'closed_at', 'entry_price', 'exit_price',
                                     'qty', 'slippage', 'fee', 'pnl'}
