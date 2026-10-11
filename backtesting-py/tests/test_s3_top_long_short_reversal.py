"""S3 top_long_short_reversal: hand-computed candles, mocked central daily top-trader ratio series (no network).

Grid: bars from T0 (2026-01-01 00:00 UTC). The value of day D (ts = D 00:00) is readable from D+1 00:00, i.e.
at the CLOSE of the last bar of day D, so the earliest fill is the first open of day D+1. With 1d bars bar k
(= day k) closes at day k+1 and reads the value of day k. Neutral days hold FILLER (outside every trigger
zone); a stop exit fills at the next open (same mechanics as the funding template).
"""
import sys
from pathlib import Path

import pandas as pd
import pytest
from backtesting import Backtest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p

TOOL = 'local.backtesting_py.top_long_short_reversal'
KEY = 'top_long_short_reversal'
T0 = 1767225600  # 2026-01-01 00:00 UTC
DAY = 86400
FILLER = '0.85'  # above long_threshold 0.7, below exit_band_low 1.0: neither enters nor exits
LABELS = ('BTC', 'Binance', 'top_long_short_ratio', '1d')
TF_SECONDS = {'1h': 3600, '4h': 14400, '1d': 86400}


def daily_frame(days, start=T0):
    """1d bars: Open 100 + 10k, High +2, Low -2, Close +1."""
    opens = [100. + 10 * k for k in range(days)]
    return pd.DataFrame(dict(Open=opens, High=[o + 2 for o in opens], Low=[o - 2 for o in opens],
                             Close=[o + 1 for o in opens], Volume=[1.] * days),
                        index=pd.date_range(pd.to_datetime(start, unit='s'), periods=days, freq='D'))


def flat_frame(bars, freq):
    return pd.DataFrame(dict(Open=[100.] * bars, High=[100.5] * bars, Low=[99.5] * bars, Close=[100.] * bars,
                             Volume=[1.] * bars),
                        index=pd.date_range('2026-01-01', periods=bars, freq=freq))


def install(monkeypatch, tmp_path, values, data, *, drop=(), min_day=-3, max_day=40, fail=None,
            ohlcv_calls=None, chunk_calls=None, origin=T0):
    """values: {day index: ratio string}; every other day in [min_day, max_day] holds FILLER."""
    store = {origin + k * DAY: values.get(k, FILLER) for k in range(min_day, max_day + 1) if k not in drop}
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', tmp_path)

    def fetch_ohlcv(*a, **k):
        if ohlcv_calls is not None:
            ohlcv_calls.append(a)
        return data.copy()

    def fetch_chunk(*, symbol, metric, interval, exchange, start_at, end_at, raise_on_transport_error=False):
        if chunk_calls is not None:
            chunk_calls.append(dict(symbol=symbol, metric=metric, interval=interval, exchange=exchange,
                                    start_at=start_at, end_at=end_at))
        if fail:
            raise fail
        if (symbol, exchange, metric, interval) != LABELS:
            return []
        return [{'ts': ts, 'value': v} for ts, v in sorted(store.items()) if start_at <= ts <= end_at]

    def no_warmup(*a, **k):
        pytest.fail('top_long_short_reversal must fetch zero warmup')

    monkeypatch.setattr(p, '_fetch_ohlcv', fetch_ohlcv)
    monkeypatch.setattr(p, '_fetch_template_warmup', no_warmup)
    monkeypatch.setattr(p, '_fetch_artifact_metric_chunk', fetch_chunk)
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **k: None)


def post(values=None, *, market='spot', timeframe='1d', symbol='BTCUSDT', bars=8, start=T0, **extra):
    request = dict(run_id='s3', provider_tool_id=TOOL, provider_params=values if values is not None else {},
                   symbol=symbol, market=market, timeframe=timeframe, start_at=start,
                   end_at=start + bars * TF_SECONDS.get(timeframe, 86400),
                   initial_capital='10000', fee_bps='0', slippage_bps='0', **extra)
    response = TestClient(p.app).post('/cutie/backtest', json={'backtest': request})
    assert response.status_code == 200
    return response.json()


def run_daily(monkeypatch, tmp_path, ratios, values=None, data=None, bars=8, **kw):
    install(monkeypatch, tmp_path, ratios, daily_frame(bars) if data is None else data, **kw)
    body = post(values, bars=bars)
    assert body['result_status'] == 'success', body
    return body


def legs(body, step=DAY):
    return [((t['opened_at'] - T0) // step, t['entry_price'], (t['closed_at'] - T0) // step, t['exit_price'])
            for t in body['trades']]


def decisions(body):
    return body['raw_report'][KEY]['decisions']


def assert_no_lookahead(body, bar_seconds):
    """Every judged value was fully published (ts + 1 day) at the close of the bar that judged it."""
    for record in decisions(body):
        assert record['value_ts'] + DAY <= record['decision_bar_open'] + bar_seconds


# ------------------------------------------------------------------ hand-computed 1d scenario

def test_hand_computed_daily_round_trips(monkeypatch, tmp_path):
    # Bar k (day k): Open 100 + 10k. Day 1 = 0.65 -> bar 1 close reads it -> buy at bar 2 open (120).
    # Day 3 = 1.10 inside [1.0, 1.5] -> sell at bar 4 open (140), ratio_back_in_band.
    # Day 4 = 0.50 -> buy at bar 5 open (150). Day 5 = 1.80 above the band -> sell at bar 6 open (160).
    body = run_daily(monkeypatch, tmp_path, {1: '0.65', 3: '1.10', 4: '0.50', 5: '1.80'})
    assert legs(body) == [(2, '120', 4, '140'), (5, '150', 6, '160')]
    first, second = body['trades']
    assert (first['side'], second['side']) == ('long', 'long')
    assert p.Decimal(first['pnl']) == 20 * p.Decimal(first['qty'])
    assert p.Decimal(second['pnl']) == 10 * p.Decimal(second['qty'])
    assert [(d['value_date'], d['value'], d['action'], d['reason']) for d in decisions(body)] == [
        ('2026-01-02', '0.65', 'entry', 'ratio_at_or_below_threshold'),
        ('2026-01-04', '1.1', 'exit', 'ratio_back_in_band'),
        ('2026-01-05', '0.5', 'entry', 'ratio_at_or_below_threshold'),
        ('2026-01-06', '1.8', 'exit', 'ratio_above_band'),
    ]
    assert_no_lookahead(body, DAY)
    counts = body['raw_report'][KEY]['counts']
    assert counts == dict(judged_values=7, entry_signals=2, exit_signals_in_band=1, exit_signals_above_band=1,
                          stop_loss_exits=0, values_consumed_by_stop=0, entries_blocked=0)


def test_threshold_and_band_edges_are_inclusive(monkeypatch, tmp_path):
    # ratio == long_threshold (0.7) enters; ratio == exit_band_low (1.0) exits (back in band).
    body = run_daily(monkeypatch, tmp_path, {1: '0.70', 3: '1.00'})
    assert legs(body) == [(2, '120', 4, '140')]
    assert decisions(body)[1]['reason'] == 'ratio_back_in_band'
    # ratio == exit_band_high (1.5) is still inside the band; just above it is ratio_above_band.
    edge = run_daily(monkeypatch, tmp_path, {1: '0.65', 3: '1.5'})
    assert decisions(edge)[1]['reason'] == 'ratio_back_in_band'
    above = run_daily(monkeypatch, tmp_path, {1: '0.65', 3: '1.51'})
    assert legs(above) == legs(edge) and decisions(above)[1]['reason'] == 'ratio_above_band'
    # a value just over the threshold does not enter
    none = run_daily(monkeypatch, tmp_path, {1: '0.71'})
    assert none['trades'] == []


def test_ratio_above_band_exit_counts(monkeypatch, tmp_path):
    body = run_daily(monkeypatch, tmp_path, {1: '0.65', 3: '2.40'})
    assert legs(body) == [(2, '120', 4, '140')]
    counts = body['raw_report'][KEY]['counts']
    assert (counts['exit_signals_above_band'], counts['exit_signals_in_band']) == (1, 0)
    assert decisions(body)[1]['reason'] == 'ratio_above_band'


def test_custom_parameters(monkeypatch, tmp_path):
    values = dict(long_threshold=0.5, exit_band_low=1.2, exit_band_high=1.3, stop_loss_pct=10)
    body = run_daily(monkeypatch, tmp_path, {1: '0.6', 2: '0.45', 4: '1.1', 5: '1.25'}, values)
    # 0.6 > 0.5 no entry; 0.45 enters (day 2 -> bar 3 open 130); 1.1 is below the band low: hold; 1.25 exits.
    assert legs(body) == [(3, '130', 6, '160')]


# ------------------------------------------------------------------ no look-ahead

def test_value_is_not_readable_before_the_next_day(monkeypatch, tmp_path):
    # Day 2 holds the trigger. Readable only from day 3 00:00 (bar 2 close) -> fill at bar 3 open, never earlier.
    body = run_daily(monkeypatch, tmp_path, {2: '0.60'})
    assert [leg[:2] for leg in legs(body)] == [(3, '130')]
    assert decisions(body)[0]['value_date'] == '2026-01-03'
    assert_no_lookahead(body, DAY)


def test_hourly_value_published_at_midnight(monkeypatch, tmp_path):
    # Day 1 = 0.65 is published at day 2 00:00 = the close of bar 47; the fill is bar 48's open (not bar 24).
    install(monkeypatch, tmp_path, {1: '0.65'}, flat_frame(96, 'h'))
    body = post(timeframe='1h', bars=96)
    assert body['result_status'] == 'success', body
    assert [leg[:2] for leg in legs(body, 3600)] == [(48, '100')]
    assert_no_lookahead(body, 3600)


# ------------------------------------------------------------------ 1h: one judgement per daily value

def hourly_stop_frame():
    data = flat_frame(120, 'h')
    data.iloc[50] = [100, 100.5, 96, 99, 1]  # low 96 touches the 97 stop (entry 100, 3%)
    data.iloc[51:] = [95, 95.5, 94.5, 95, 1]
    return data


def test_hourly_judges_each_value_once_and_stop_blocks_same_day_reentry(monkeypatch, tmp_path):
    # Day 1 = 0.65 -> buy at bar 48 (day 2 00:00). Bar 50 low touches the stop -> sell at bar 51 open (95).
    # Bars 51..71 still read day 1's value (<= threshold) but it was judged once: no re-entry that day.
    # Day 2 = 0.60 is published at bar 71's close -> buy at bar 72. Day 3 = 1.10 -> sell at bar 96.
    install(monkeypatch, tmp_path, {1: '0.65', 2: '0.60', 3: '1.10'}, hourly_stop_frame())
    body = post(timeframe='1h', bars=120)
    assert body['result_status'] == 'success', body
    assert legs(body, 3600) == [(48, '100', 51, '95'), (72, '95', 96, '95')]
    counts = body['raw_report'][KEY]['counts']
    # judged values: days -1..4 (bar 1 reads day -1; bar 119 reads day 4) -> 6, each exactly once
    assert counts['judged_values'] == 6 and counts['entry_signals'] == 2
    assert counts['stop_loss_exits'] == 1 and counts['exit_signals_in_band'] == 1
    assert_no_lookahead(body, 3600)


def test_hourly_first_value_is_judged_once(monkeypatch, tmp_path):
    # Day -1 (readable already at bar 0/1) holds the trigger: judged once at bar 1 close -> fill at bar 2.
    install(monkeypatch, tmp_path, {-1: '0.60'}, flat_frame(30, 'h'))
    body = post(timeframe='1h', bars=30)
    assert [leg[:2] for leg in legs(body, 3600)] == [(2, '100')]
    assert body['raw_report'][KEY]['counts']['entry_signals'] == 1


# ------------------------------------------------------------------ stop-loss

def test_intrabar_stop_fills_at_next_open(monkeypatch, tmp_path):
    data = daily_frame(8)
    data.iloc[2] = [120, 122, 115, 121, 1]  # entry 120 -> stop 116.4; low 115 touches
    body = run_daily(monkeypatch, tmp_path, {1: '0.65'}, data=data)
    # detected on the entry bar's own candle, filled at the next open (130), not at the 116.4 stop price
    assert legs(body)[0] == (2, '120', 3, '130')
    assert body['raw_report'][KEY]['counts']['stop_loss_exits'] == 1


def test_gap_below_stop_fills_at_following_open(monkeypatch, tmp_path):
    data = daily_frame(8)
    data.iloc[3] = [110, 112, 108, 109, 1]  # bar 3 gaps below the 116.4 stop
    data.iloc[4] = [105, 107, 103, 106, 1]
    body = run_daily(monkeypatch, tmp_path, {1: '0.65'}, data=data)
    assert legs(body)[0] == (2, '120', 4, '105')
    assert body['trades'][0]['exit_price'] == '105'


def test_stop_wins_over_signal_exit_on_the_same_bar(monkeypatch, tmp_path):
    data = daily_frame(8)
    data.iloc[3] = [130, 132, 110, 131, 1]  # touches the stop on bar 3 ...
    # ... and day 3's value (1.10, in band) is also judged at bar 3's close: the stop is recorded.
    body = run_daily(monkeypatch, tmp_path, {1: '0.65', 3: '1.10'}, data=data)
    counts = body['raw_report'][KEY]['counts']
    assert (counts['stop_loss_exits'], counts['exit_signals_in_band'], counts['values_consumed_by_stop']) == (1, 0, 1)
    consumed = [d for d in decisions(body) if d['action'] == 'stop_exit_consumed_value']
    assert consumed and consumed[0]['reason'] == 'stop_loss' and consumed[0]['value_date'] == '2026-01-04'
    assert legs(body)[0][:2] == (2, '120')


def test_stop_percent_is_a_parameter_frozen_at_the_fill(monkeypatch, tmp_path):
    data = daily_frame(8)
    data.iloc[2] = [120, 122, 115, 121, 1]  # 4.2% below the fill: a 3% stop fires, a 10% stop does not
    wide = run_daily(monkeypatch, tmp_path, {1: '0.65', 3: '1.10'}, dict(stop_loss_pct=10), data=data)
    assert wide['raw_report'][KEY]['counts']['stop_loss_exits'] == 0
    assert legs(wide) == [(2, '120', 4, '140')]


# ------------------------------------------------------------------ fail-closed series

def test_missing_day_fails_with_gap_details(monkeypatch, tmp_path):
    chunk_calls = []
    install(monkeypatch, tmp_path, {1: '0.65'}, daily_frame(8), drop=(3,), chunk_calls=chunk_calls)
    body = post()
    assert body['result_status'] == 'failed' and body['error_type'] == 'TIME_DATA_GAP'
    assert body['limitations']['reason'] == 'long_short_ratio_data_gap'
    assert body['limitations']['series_metric'] == 'top_long_short_ratio'
    assert body['limitations']['required_first_date'] == '2026-01-01'
    assert body['limitations']['required_last_date'] == '2026-01-08'
    assert body['error_message'].startswith('TIME_DATA_GAP:')
    assert len(chunk_calls) == 1 and chunk_calls[0]['symbol'] == 'BTC'


def test_series_ending_early_and_empty_series_fail(monkeypatch, tmp_path):
    install(monkeypatch, tmp_path, {}, daily_frame(8), max_day=5)
    body = post()
    assert body['error_type'] == 'TIME_DATA_GAP' and body['limitations']['reason'] == 'long_short_ratio_data_gap'
    install(monkeypatch, tmp_path, {}, daily_frame(8), drop=tuple(range(-3, 41)))
    body = post()
    assert body['error_type'] == 'TIME_DATA_GAP' and body['limitations']['reason'] == 'long_short_ratio_data_gap'


def test_unreachable_central_db_fails_closed(monkeypatch, tmp_path):
    install(monkeypatch, tmp_path, {}, daily_frame(8), fail=ValueError('page budget'))
    body = post()
    assert body['error_type'] == 'TIME_DATA_GAP'
    assert body['limitations']['reason'] == 'long_short_ratio_history_unavailable'


def test_window_before_the_earliest_day_is_rejected_before_any_fetch(monkeypatch, tmp_path):
    floor = p.TOP_LSR_EARLIEST_TS
    assert floor == 1589587200  # 2020-05-16 00:00 UTC
    chunk_calls = []
    start = floor - DAY  # the first decision would need the 2020-05-15 value
    install(monkeypatch, tmp_path, {}, daily_frame(6, start), origin=start, chunk_calls=chunk_calls)
    body = post(start=start, bars=6)
    assert (body['result_status'], body['error_type']) == ('failed', 'INSUFFICIENT_DATA')
    assert body['limitations']['reason'] == 'long_short_ratio_history_unavailable'
    assert body['limitations']['earliest_available_date'] == '2020-05-16'
    assert body['limitations']['series_metric'] == 'top_long_short_ratio'
    assert chunk_calls == []
    # starting exactly on the earliest day is allowed
    install(monkeypatch, tmp_path, {1: '0.65'}, daily_frame(6, floor), origin=floor, chunk_calls=chunk_calls)
    body = post(start=floor, bars=6)
    assert body['result_status'] == 'success' and len(chunk_calls) == 1


# ------------------------------------------------------------------ parameter and market validation

@pytest.mark.parametrize('values', [
    dict(long_threshold=0.05), dict(long_threshold=3.5), dict(long_threshold='0.7'), dict(long_threshold=True),
    dict(exit_band_low=0.05), dict(exit_band_low=5.5), dict(exit_band_high=0.05), dict(exit_band_high=10.5),
    dict(stop_loss_pct=0.05), dict(stop_loss_pct=21), dict(stop_loss_pct=True), dict(stop_loss_pct='3'),
    # ordering: long_threshold < exit_band_low <= exit_band_high
    dict(long_threshold=1.0), dict(long_threshold=1.2), dict(exit_band_low=1.6), dict(exit_band_low=0.7),
    dict(exit_band_low=2, exit_band_high=1.9),
    # keys the template does not consume
    dict(max_holding_bars=4), dict(time_layer_enabled=True), dict(atr_stop_multiplier=2), dict(take_profit_pct=5),
    dict(direction='short'), dict(hold_minutes=30),
])
def test_invalid_params_are_rejected_before_any_fetch(monkeypatch, tmp_path, values):
    chunk_calls, ohlcv = [], []
    install(monkeypatch, tmp_path, {}, daily_frame(8), chunk_calls=chunk_calls, ohlcv_calls=ohlcv)
    body = post(values)
    assert body['result_status'] == 'failed' and body['error_type'] == 'INVALID_PARAMS', body
    assert chunk_calls == [] and ohlcv == []


def test_ordering_message_is_direct(monkeypatch, tmp_path):
    install(monkeypatch, tmp_path, {}, daily_frame(8))
    body = post(dict(long_threshold=1.2))
    assert 'long_threshold < exit_band_low <= exit_band_high' in body['error_message']


def test_equal_band_edges_are_allowed(monkeypatch, tmp_path):
    body = run_daily(monkeypatch, tmp_path, {1: '0.65', 3: '1.2'}, dict(exit_band_low=1.2, exit_band_high=1.2))
    assert legs(body) == [(2, '120', 4, '140')]


def test_futures_market_and_other_timeframes_are_rejected(monkeypatch, tmp_path):
    chunk_calls = []
    install(monkeypatch, tmp_path, {}, daily_frame(8), chunk_calls=chunk_calls)
    body = post(market='futures')
    assert body['error_type'] == 'INVALID_PARAMS' and 'spot' in body['error_message']
    for timeframe in ('15m', '30m', '1w'):
        body = post(timeframe=timeframe)
        assert body['error_type'] == 'TIMEFRAME_UNSUPPORTED', (timeframe, body)
    body = post(symbol='BTCEUR')
    assert body['error_type'] == 'INVALID_PARAMS'
    assert chunk_calls == []


def test_candle_exchange_is_free_and_series_stays_binance(monkeypatch, tmp_path):
    chunk_calls = []
    body = run_daily(monkeypatch, tmp_path, {1: '0.65'}, dict(exchange='okx'), chunk_calls=chunk_calls)
    assert body['assumptions']['long_short_ratio_series']['exchange'] == 'Binance'
    assert chunk_calls[0]['exchange'] == 'Binance'


# ------------------------------------------------------------------ evidence and catalog

def test_assumptions_series_block(monkeypatch, tmp_path):
    chunk_calls = []
    body = run_daily(monkeypatch, tmp_path, {1: '0.65'}, chunk_calls=chunk_calls)
    series = body['assumptions']['long_short_ratio_series']
    assert {k: series[k] for k in ('symbol', 'exchange', 'metric', 'interval')} == dict(
        symbol='BTC', exchange='Binance', metric='top_long_short_ratio', interval='1d')
    assert (series['first_date'], series['last_date'], series['count']) == ('2026-01-01', '2026-01-08', 8)
    assert (series['first_ts'], series['last_ts']) == (T0, T0 + 7 * DAY)
    assert series['earliest_available_date'] == '2020-05-16' and series['gap_policy'] == 'fail_closed'
    assert 'available_at = ts + 86400s' in series['available_at_rule'] and 'D+1 open' in series['available_at_rule']
    assert len(series['revision']) >= 32
    assert chunk_calls == [dict(symbol='BTC', metric='top_long_short_ratio', interval='1d', exchange='Binance',
                                start_at=T0, end_at=T0 + 8 * DAY - 1)]
    a = body['assumptions'][KEY]
    assert (a['long_threshold'], a['exit_band_low'], a['exit_band_high'], a['stop_loss_pct']) == (0.7, 1.0, 1.5, 3)
    assert a['judged_values'] == 7 and a['entry_signals'] == 1


def test_symbol_label_strips_the_quote_suffix(monkeypatch, tmp_path):
    chunk_calls = []
    install(monkeypatch, tmp_path, {}, daily_frame(8), chunk_calls=chunk_calls)
    body = post(symbol='BTC/USDC')
    assert body['result_status'] == 'success'  # LABELS only know BTC: the quote suffix was stripped
    assert chunk_calls[0]['symbol'] == 'BTC'
    chunk_calls.clear()
    post(symbol='ETHBUSD')
    assert chunk_calls[0]['symbol'] == 'ETH'


def test_catalog_entry(monkeypatch):
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    tools = {t['tool_id']: t for t in TestClient(p.app).get('/catalog').json()['tools']}
    tool = tools[TOOL]
    assert tool['markets'] == ['spot'] and tool['timeframes'] == ['1h', '4h', '1d']
    props = tool['param_schema']['properties']
    assert props['long_threshold'] == dict(type='number', default=0.7, minimum=0.1, maximum=3)
    assert props['exit_band_low'] == dict(type='number', default=1.0, minimum=0.1, maximum=5)
    assert props['exit_band_high'] == dict(type='number', default=1.5, minimum=0.1, maximum=10)
    assert props['stop_loss_pct'] == dict(type='number', default=3, minimum=0.1, maximum=20)
    assert 'exchange' in props
    assert not {'max_holding_bars', 'time_layer_enabled', 'time_calendar', 'atr_stop_multiplier',
                'take_profit_pct'} & set(props)
    description = tool['description']
    assert 'Binance' in description and '2020-05-16' in description and 'one missing day' in description.lower()
