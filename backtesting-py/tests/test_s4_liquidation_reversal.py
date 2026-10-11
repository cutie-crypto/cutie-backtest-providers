"""S4 liquidation_reversal: hand-computed candles, mocked central 1d liquidation_long series (no network, synthetic data).

Day k starts at T0 + k * 86400 (UTC). Series value of day k defaults to BASE(k) = 500 - 10 k, a strictly decreasing
sequence, so a baseline day is always below its look-back percentile and never triggers on its own; the tests lift
single days. The window is 14 days (0..13): the judged days are D = 1..12, D is judged once at the close of the first
candle of day D + 1 and the order fills at the next open. Rows of day 13 and later hold FILLER, which would trade if
ever read.
"""
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from backtesting import Backtest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p
import strategy_liquidation_reversal as lr

KIND = 'liquidation_reversal'
TOOL = f'local.backtesting_py.{KIND}'
DAY = 86400
T0 = int(datetime(2026, 3, 2, tzinfo=timezone.utc).timestamp())
NDAYS = 14
FILLER = '99999999'
LABELS = ('BTC', 'AGGREGATED', 'liquidation_long', '1d')
STEPS = {'1h': 3600, '4h': 14400, '1d': 86400}


def base_value(k):
    return str(500 - 10 * k)


def flat(price):
    return [price, price + 0.5, price - 0.5, price]


def candles(timeframe, rows_by_day, overrides=None, n_days=NDAYS, t0=T0):
    """rows_by_day: {day: [O, H, L, C]} applied to every candle of the day; ``overrides``: {bar index: row}.

    Days without an entry keep flat 100. For 1h/4h the day's LAST candle carries the day close and the earlier
    candles of the day are flat at the day open, so the day-to-day close ratio is exactly the row's C / previous C.
    """
    per_day = DAY // STEPS[timeframe]
    rows = []
    for day in range(n_days):
        row = rows_by_day.get(day, flat(100))
        for i in range(per_day):
            rows.append(list(row))
    for index, row in (overrides or {}).items():
        rows[index] = list(row)
    frame = pd.DataFrame([r + [1.0] for r in rows], columns=['Open', 'High', 'Low', 'Close', 'Volume'],
                         index=pd.date_range(pd.Timestamp(t0, unit='s'), periods=len(rows),
                                             freq={'1h': '1h', '4h': '4h', '1d': '1D'}[timeframe]))
    return frame


def store_for(values, t0=T0, first_day=-40, last_day=NDAYS + 2):
    store = {}
    for k in range(first_day, last_day + 1):
        store[t0 + k * DAY] = FILLER if k >= NDAYS - 1 else base_value(k)
    for k, v in values.items():
        store[t0 + k * DAY] = str(v)
    return store


def install(monkeypatch, tmp_path, store, frame, *, fail=None, ohlcv_calls=None, returned=None):
    calls = []
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', tmp_path)

    def fetch_ohlcv(*a, **k):
        if ohlcv_calls is not None:
            ohlcv_calls.append(a)
        return frame.copy()

    def fetch_chunk(*, symbol, metric, interval, exchange, start_at, end_at, raise_on_transport_error=False):
        calls.append(dict(symbol=symbol, metric=metric, interval=interval, exchange=exchange,
                          start_at=start_at, end_at=end_at))
        if fail:
            raise fail
        if (symbol, exchange, metric, interval) != LABELS:
            return []
        out = [{'ts': ts, 'value': v} for ts, v in sorted(store.items()) if start_at <= ts <= end_at]
        if returned is not None:
            returned.extend(row['ts'] for row in out)
        return out

    def no_warmup(*a, **k):
        pytest.fail('liquidation_reversal must fetch zero warmup')

    monkeypatch.setattr(p, '_fetch_ohlcv', fetch_ohlcv)
    monkeypatch.setattr(p, '_fetch_template_warmup', no_warmup)
    monkeypatch.setattr(p, '_fetch_artifact_metric_chunk', fetch_chunk)
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **k: None)
    return calls


def post(values=None, *, market='spot', timeframe='1d', symbol='BTCUSDT', t0=T0, n=None):
    step = STEPS.get(timeframe, 900)
    n = NDAYS * DAY // step if n is None else n
    request = dict(run_id='s4', provider_tool_id=TOOL, provider_params=values if values is not None else {},
                   symbol=symbol, market=market, timeframe=timeframe, start_at=t0, end_at=t0 + n * step,
                   initial_capital='10000', fee_bps='0', slippage_bps='0')
    response = TestClient(p.app).post('/cutie/backtest', json={'backtest': request})
    assert response.status_code == 200
    return response.json()


def run(monkeypatch, tmp_path, values_by_day, params=None, rows=None, timeframe='1d', overrides=None, **kw):
    frame = candles(timeframe, rows or {}, overrides)
    calls = install(monkeypatch, tmp_path, store_for(values_by_day), frame, **kw)
    body = post(params, timeframe=timeframe)
    assert body['result_status'] == 'success', body
    return body, calls


def legs(body, step=DAY):
    return [((t['opened_at'] - T0) // step, t['entry_price'], (t['closed_at'] - T0) // step,
             t['exit_price'], t['side']) for t in body['trades']]


def events(body):
    return body['raw_report'][KIND]['events']


def event_of(body, day):
    return next(r for r in events(body) if r['day_utc'] == lr._iso(T0 + day * DAY))


# Day 5 spikes (1000; p99 of the 30 days -25..4 is 740 + 10 * 0.71 = 747.1) and falls 4 % (100 -> 96); day 6 decides, day 7 opens the trade.
ROWS = {5: [100, 100.5, 95, 96], 6: [96, 96.5, 95.5, 96], 7: [97, 98, 96, 97.5],
        **{d: [98, 98.5, 97.5, 98] for d in range(8, NDAYS)}}
SPIKE = {5: 1000}


# ------------------------------------------------------------------ percentile pins

def test_percentile_linear_hand_values():
    values = [Decimal(v) for v in ('70', '10', '50', '20', '60', '30', '40')]
    assert lr.percentile_linear(values, 50) == Decimal('40')
    assert lr.percentile_linear(values, 99) == Decimal('69.4')  # a[5] = 60 + (70 - 60) * 0.94
    assert lr.percentile_linear(values, 90) == Decimal('64')  # rank 5.4: 60 + 10 * 0.4
    assert lr.percentile_linear(values, 100) == Decimal('70')
    assert lr.percentile_linear([Decimal('1.5'), Decimal('2.5')], 75) == Decimal('2.25')


PIN_WINDOW = {-6: 70, -5: 10, -4: 50, -3: 20, -2: 60, -1: 30, 0: 40}  # days -6..0 are the window of D = 1
PIN_ROWS = {1: [100, 100.5, 95, 96], **{d: flat(96) for d in range(2, NDAYS)}}


@pytest.mark.parametrize('percentile,value,traded', [
    (99, '69.4', True),    # D equals p exactly: triggers
    (99, '69.39', False),
    (99, '70', True),
    (50, '40', True),      # p50 of the window is 40
    (50, '39.99', False),
])
def test_threshold_pin_and_equality_triggers(monkeypatch, tmp_path, percentile, value, traded):
    body, _ = run(monkeypatch, tmp_path, {**PIN_WINDOW, 1: value}, dict(percentile=percentile, lookback_days=7),
                  rows=PIN_ROWS)
    record = event_of(body, 1)
    assert record['threshold'] == ('69.4' if percentile == 99 else '40')
    assert (record['status'] == 'entered') is traded
    if traded:
        # decided at day 2's first (only) close, buy at day 3's open
        assert legs(body)[0][:2] == (3, '96')
    else:
        assert record['reason'] == 'liquidation_below_threshold'


def test_percentile_window_excludes_day_d(monkeypatch, tmp_path):
    body, _ = run(monkeypatch, tmp_path, dict(SPIKE), rows=ROWS)
    record = event_of(body, 5)
    # days -25..4 hold 750..460 (step 10): rank 0.99 * 29 = 28.71 -> 740 + 10 * 0.71; day 5 itself is not in it
    assert record['threshold'] == '747.1' and record['liquidation_long'] == '1000'


# ------------------------------------------------------------------ 1d hand-computed scenarios

def test_entry_and_time_expiry_exit(monkeypatch, tmp_path):
    body, _ = run(monkeypatch, tmp_path, dict(SPIKE), rows=ROWS)
    # decision at day 6's close, buy at day 7's open 97; hold 24h = 1 candle -> decided at day 7's close, filled at
    # day 8's open 98 (the funding-reversal hold_minutes rule)
    assert legs(body) == [(7, '97', 8, '98', 'long')]
    record = event_of(body, 5)
    assert record['status'] == 'entered' and record['exit_reason'] == 'time_expiry'
    assert record['decision_bar_utc'] == lr._iso(T0 + 6 * DAY)
    assert record['drop_pct'] == '-4'
    assert Decimal(record['stop_price']) == Decimal('94.09') and Decimal(record['take_price']) == Decimal('99.91')


def test_take_profit_exit(monkeypatch, tmp_path):
    rows = {**ROWS, 7: [97, 100, 96, 99.5]}  # High 100 >= 99.91
    body, _ = run(monkeypatch, tmp_path, dict(SPIKE), dict(hold_hours=72), rows=rows)
    assert legs(body) == [(7, '97', 8, '98', 'long')]
    assert event_of(body, 5)['exit_reason'] == 'take_profit'


def test_stop_loss_exit(monkeypatch, tmp_path):
    rows = {**ROWS, 7: [97, 98, 94, 95], 8: [95, 96, 94.5, 95]}  # Low 94 <= 94.09
    body, _ = run(monkeypatch, tmp_path, dict(SPIKE), dict(hold_hours=72), rows=rows)
    assert legs(body) == [(7, '97', 8, '95', 'long')]
    assert event_of(body, 5)['exit_reason'] == 'stop_loss'


def test_time_expiry_after_n_candles(monkeypatch, tmp_path):
    rows = {**ROWS, 10: [98.2, 98.7, 97.7, 98.2]}
    body, _ = run(monkeypatch, tmp_path, dict(SPIKE), dict(hold_hours=72), rows=rows)
    # candles 7, 8, 9 are the three held candles; the close after candle 9 fills at day 10's open
    assert legs(body) == [(7, '97', 10, '98.2', 'long')]
    assert event_of(body, 5)['exit_reason'] == 'time_expiry'


@pytest.mark.parametrize('hold', [24, 72])
def test_stop_and_take_in_the_same_candle_stop_wins(monkeypatch, tmp_path, hold):
    rows = {**ROWS, 7: [97, 100, 94, 96], 8: [95, 96, 94.5, 95]}
    body, _ = run(monkeypatch, tmp_path, dict(SPIKE), dict(hold_hours=hold), rows=rows)
    assert legs(body) == [(7, '97', 8, '95', 'long')]
    assert event_of(body, 5)['exit_reason'] == 'stop_loss'


def test_time_expiry_beats_take_profit_in_the_same_candle(monkeypatch, tmp_path):
    rows = {**ROWS, 7: [97, 100, 96, 99.5]}  # hold 24h expires on this candle and take-profit also touched
    body, _ = run(monkeypatch, tmp_path, dict(SPIKE), rows=rows)
    assert event_of(body, 5)['exit_reason'] == 'time_expiry'


# ------------------------------------------------------------------ 1h

def test_1h_hold_24_is_24_candles_one_judgement_per_day(monkeypatch, tmp_path):
    rows = {5: [100, 100.2, 99.8, 100], 6: [96, 96.2, 95.8, 96], **{d: [96, 96.2, 95.8, 96] for d in range(7, NDAYS)}}
    overrides = {24 * 5 + 23: [100, 100.2, 95.5, 96]}  # day 5's last close 96 vs day 4's 100
    body, _ = run(monkeypatch, tmp_path, dict(SPIKE), rows=rows, timeframe='1h', overrides=overrides)
    entry = 24 * 6 + 1  # decided at the close of day 6's first candle (00:00), bought at 01:00
    assert legs(body, 3600) == [(entry, '96', entry + 24, '96', 'long')]
    record = event_of(body, 5)
    assert record['decision_bar_utc'] == lr._iso(T0 + 6 * DAY) and record['status'] == 'entered'
    assert body['assumptions'][KIND]['hold_bars'] == 24
    assert body['assumptions'][KIND]['signal_counts']['liquidation_below_threshold'] >= 1


def test_1h_stop_out_does_not_reenter_the_same_day_value(monkeypatch, tmp_path):
    rows = {5: [100, 100.2, 99.8, 100], **{d: [93, 93.2, 92.8, 93] for d in range(6, NDAYS)}}
    entry = 24 * 6 + 1
    overrides = {24 * 5 + 23: [100, 100.2, 95.5, 96], 24 * 6: [96, 96.2, 95.8, 96],
                 entry: [96, 96.2, 92, 93]}  # stop 93.12 hit on the entry candle itself
    body, _ = run(monkeypatch, tmp_path, dict(SPIKE), rows=rows, timeframe='1h', overrides=overrides)
    assert legs(body, 3600) == [(entry, '96', entry + 1, '93', 'long')]
    assert len(body['trades']) == 1
    assert [r['status'] for r in events(body)].count('entered') == 1
    assert event_of(body, 5)['exit_reason'] == 'stop_loss'
    # every judged day has exactly one record and exactly one verdict
    assert len(events(body)) == 12 and all('status' in r for r in events(body))


def test_4h_hold_hours_must_be_a_multiple_of_the_candle(monkeypatch, tmp_path):
    frame = candles('4h', {})
    calls = install(monkeypatch, tmp_path, store_for({}), frame)
    body = post(dict(hold_hours=6), timeframe='4h')
    assert body['error_type'] == 'INVALID_PARAMS' and 'multiple of the candle' in body['error_message']
    assert calls == []
    body = post(dict(hold_hours=8), timeframe='4h')
    assert body['result_status'] == 'success' and body['assumptions'][KIND]['hold_bars'] == 2


# ------------------------------------------------------------------ one condition is not enough

def test_big_liquidation_but_small_drop_does_not_enter(monkeypatch, tmp_path):
    rows = {**ROWS, 5: [100, 100.5, 97, 98]}  # -2 %
    body, _ = run(monkeypatch, tmp_path, dict(SPIKE), rows=rows)
    assert body['trades'] == []
    assert event_of(body, 5)['reason'] == 'drop_below_min'


def test_big_drop_but_small_liquidation_does_not_enter(monkeypatch, tmp_path):
    body, _ = run(monkeypatch, tmp_path, {}, rows=ROWS)
    assert body['trades'] == []
    assert event_of(body, 5)['reason'] == 'liquidation_below_threshold'


def test_drop_exactly_at_min_enters_and_missing_previous_day_is_no_signal(monkeypatch, tmp_path):
    rows = {**ROWS, 5: [100, 100.5, 95, 97]}  # exactly -3 %
    body, _ = run(monkeypatch, tmp_path, dict(SPIKE), rows=rows)
    assert event_of(body, 5)['status'] == 'entered'
    # a window whose first candles are missing: day 1 has no day-0 close -> no signal, not a failure
    frame = candles('1d', ROWS).iloc[1:]
    install(monkeypatch, tmp_path, store_for({1: 1000}), frame)
    body = post(dict(), t0=T0)
    assert body['result_status'] == 'success'
    assert event_of(body, 1)['reason'] == 'no_prior_day_candle'


# ------------------------------------------------------------------ fixed mode

def test_fixed_mode_uses_the_amount_and_fetches_no_lookback(monkeypatch, tmp_path):
    body, calls = run(monkeypatch, tmp_path, {5: 600}, dict(threshold_mode='fixed', fixed_threshold_usd=500),
                      rows=ROWS)
    assert event_of(body, 5)['threshold'] == '500' and event_of(body, 5)['status'] == 'entered'
    assert calls[0]['start_at'] == T0 + DAY  # first judged day D = 1: no look-back days before it
    assert calls[-1]['end_at'] == T0 + 13 * DAY - 1  # last judged day 12 -> half-open end at day 13
    body, _ = run(monkeypatch, tmp_path, {5: 499}, dict(threshold_mode='fixed', fixed_threshold_usd=500), rows=ROWS)
    assert body['trades'] == []
    a = body['assumptions'][KIND]
    assert a['threshold_mode'] == 'fixed' and a['lookback_days'] == 0


def test_percentile_mode_fetches_the_lookback_and_never_reads_day_after_the_last_judged(monkeypatch, tmp_path):
    returned = []
    body, calls = run(monkeypatch, tmp_path, dict(SPIKE), rows=ROWS, returned=returned)
    assert calls[0]['start_at'] == T0 + DAY - 30 * DAY
    assert max(returned) == T0 + 12 * DAY  # FILLER rows of day 13 and later are never requested
    series = body['assumptions'][KIND]['liquidation_series']
    assert series['last_date'] == lr.utc_date(T0 + 12 * DAY)


def test_fixed_threshold_is_rejected_in_percentile_mode(monkeypatch, tmp_path):
    ohlcv = []
    calls = install(monkeypatch, tmp_path, store_for({}), candles('1d', {}), ohlcv_calls=ohlcv)
    body = post(dict(fixed_threshold_usd=500))
    assert body['error_type'] == 'INVALID_PARAMS' and 'fixed_threshold_usd' in body['error_message']
    assert calls == [] and ohlcv == []


# ------------------------------------------------------------------ fail-closed series

def test_missing_day_fails_closed_before_ohlcv(monkeypatch, tmp_path):
    ohlcv = []
    store = store_for({})
    del store[T0 + 3 * DAY]
    install(monkeypatch, tmp_path, store, candles('1d', {}), ohlcv_calls=ohlcv)
    body = post()
    assert body['result_status'] == 'failed' and body['error_type'] == 'TIME_DATA_GAP'
    assert body['error_message'].startswith('TIME_DATA_GAP:')
    lim = body['limitations']
    assert lim['reason'] == 'liquidation_data_gap' and lim['series_metric'] == 'liquidation_long'
    assert lim['required_first_date'] == lr.utc_date(T0 + DAY - 30 * DAY)
    assert lim['required_last_date'] == lr.utc_date(T0 + 12 * DAY)
    assert ohlcv == []


def test_series_ending_early_names_the_missing_date(monkeypatch, tmp_path):
    store = {ts: v for ts, v in store_for({}).items() if ts <= T0 + 9 * DAY}
    install(monkeypatch, tmp_path, store, candles('1d', {}))
    body = post()
    lim = body['limitations']
    assert body['error_type'] == 'TIME_DATA_GAP' and lim['reason'] == 'liquidation_data_gap'
    assert lim['missing_date'] == lr.utc_date(T0 + 10 * DAY)
    assert set(lim) >= {'series_metric', 'required_first_date', 'required_last_date'}


def test_empty_series_and_unavailable_central_fail_closed(monkeypatch, tmp_path):
    install(monkeypatch, tmp_path, {}, candles('1d', {}))
    body = post()
    assert body['error_type'] == 'TIME_DATA_GAP' and body['limitations']['reason'] == 'liquidation_data_gap'
    install(monkeypatch, tmp_path, store_for({}), candles('1d', {}), fail=ValueError('central down'))
    body = post()
    assert body['error_type'] == 'TIME_DATA_GAP'
    assert body['limitations']['reason'] == 'liquidation_history_unavailable'


def test_lookback_before_2020_is_refused_before_any_fetch(monkeypatch, tmp_path):
    t0 = int(datetime(2020, 1, 3, tzinfo=timezone.utc).timestamp())  # L=7: D0 = 01-04, look-back needs 2019-12-28
    ohlcv = []
    calls = install(monkeypatch, tmp_path, store_for({}, t0=t0), candles('1d', {}), ohlcv_calls=ohlcv)
    body = post(dict(lookback_days=7), t0=t0)
    assert body['error_type'] == 'INSUFFICIENT_DATA'
    assert body['limitations']['reason'] == 'liquidation_history_unavailable'
    assert body['limitations']['earliest_available_date'] == '2020-01-01'
    assert body['limitations']['series_metric'] == 'liquidation_long'
    assert calls == [] and ohlcv == []


def test_earliest_day_itself_is_allowed_and_fixed_mode_needs_no_lookback(monkeypatch, tmp_path):
    t0 = int(datetime(2020, 1, 7, tzinfo=timezone.utc).timestamp())  # look-back starts exactly 2020-01-01
    calls = install(monkeypatch, tmp_path, store_for({}, t0=t0, first_day=-40), candles('1d', {}, t0=t0))
    assert post(dict(lookback_days=7), t0=t0)['result_status'] == 'success'
    assert calls[0]['start_at'] == int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp())
    t0 = int(datetime(2019, 12, 31, tzinfo=timezone.utc).timestamp())  # fixed: D0 = 2020-01-01
    calls = install(monkeypatch, tmp_path, store_for({}, t0=t0), candles('1d', {}, t0=t0))
    assert post(dict(threshold_mode='fixed', fixed_threshold_usd=1), t0=t0)['result_status'] == 'success'
    t0 = int(datetime(2019, 12, 30, tzinfo=timezone.utc).timestamp())
    calls = install(monkeypatch, tmp_path, store_for({}, t0=t0), candles('1d', {}, t0=t0))
    body = post(dict(threshold_mode='fixed', fixed_threshold_usd=1), t0=t0)
    assert body['error_type'] == 'INSUFFICIENT_DATA' and calls == []


# ------------------------------------------------------------------ parameters and markets

@pytest.mark.parametrize('values', [
    dict(percentile=49), dict(percentile=100.5), dict(percentile='99'), dict(percentile=True),
    dict(lookback_days=6), dict(lookback_days=91), dict(lookback_days=7.5), dict(lookback_days=True),
    dict(min_drop_pct=0.4), dict(min_drop_pct=31), dict(hold_hours=0), dict(hold_hours=241), dict(hold_hours=1.5),
    dict(take_profit_pct=0.05), dict(take_profit_pct=51), dict(stop_loss_pct=0.05), dict(stop_loss_pct=21),
    dict(threshold_mode='mean'), dict(threshold_mode='fixed'),
    dict(threshold_mode='fixed', fixed_threshold_usd=0), dict(threshold_mode='fixed', fixed_threshold_usd=-5),
    dict(threshold_mode='fixed', fixed_threshold_usd=True), dict(threshold_mode='fixed', fixed_threshold_usd='5'),
    dict(max_holding_bars=4), dict(time_layer_enabled=True), dict(atr_stop_multiplier=2), dict(unknown=1),
    dict(events=[]), dict(leverage=2),
])
def test_invalid_params_reject_before_any_fetch(monkeypatch, tmp_path, values):
    ohlcv = []
    calls = install(monkeypatch, tmp_path, store_for({}), candles('1d', {}), ohlcv_calls=ohlcv)
    body = post(values)
    assert body['result_status'] == 'failed' and body['error_type'] == 'INVALID_PARAMS'
    assert calls == [] and ohlcv == []


@pytest.mark.parametrize('values', [
    dict(percentile=50, lookback_days=7, min_drop_pct=0.5, hold_hours=24, take_profit_pct=0.1, stop_loss_pct=0.1),
    dict(percentile=100, lookback_days=90, min_drop_pct=30, hold_hours=240, take_profit_pct=50, stop_loss_pct=20),
])
def test_range_edges_are_accepted(monkeypatch, tmp_path, values):
    install(monkeypatch, tmp_path, store_for({}, first_day=-100), candles('1d', {}))
    assert post(values)['result_status'] == 'success'


def test_futures_and_15m_are_rejected_before_any_fetch(monkeypatch, tmp_path):
    ohlcv = []
    calls = install(monkeypatch, tmp_path, store_for({}), candles('1d', {}), ohlcv_calls=ohlcv)
    body = post(market='futures')
    assert body['error_type'] == 'INVALID_PARAMS' and 'spot' in body['error_message']
    body = post(timeframe='15m', n=96)
    assert body['error_type'] == 'TIMEFRAME_UNSUPPORTED'
    assert calls == [] and ohlcv == []


# ------------------------------------------------------------------ look-ahead

def test_day_value_is_not_readable_before_the_next_day(monkeypatch, tmp_path):
    rows = {5: [100, 100.2, 99.8, 100], **{d: [96, 96.2, 95.8, 96] for d in range(6, NDAYS)}}
    overrides = {24 * 5 + 23: [100, 100.2, 95.5, 96]}
    returned = []
    body, _ = run(monkeypatch, tmp_path, dict(SPIKE), rows=rows, timeframe='1h', overrides=overrides,
                  returned=returned)
    first_open = body['trades'][0]['opened_at']
    assert first_open >= T0 + 6 * DAY + 3600  # day 5's value (available at day 6 00:00) buys no earlier than 01:00
    # the judgement of day 5 happens at day 6 00:00's close, never inside day 5
    assert event_of(body, 5)['decision_bar_utc'] == lr._iso(T0 + 6 * DAY)
    assert max(returned) <= T0 + 12 * DAY


# ------------------------------------------------------------------ evidence and catalog

def test_assumptions_record_series_parameters_and_each_entry_threshold(monkeypatch, tmp_path):
    body, _ = run(monkeypatch, tmp_path, dict(SPIKE), rows=ROWS)
    a = body['assumptions'][KIND]
    s = a['liquidation_series']
    assert (s['symbol'], s['exchange'], s['metric'], s['interval'], s['unit']) == (
        'BTC', 'AGGREGATED', 'liquidation_long', '1d', 'usd')
    assert s['first_date'] == lr.utc_date(T0 + DAY - 30 * DAY) and s['last_date'] == lr.utc_date(T0 + 12 * DAY)
    assert s['count'] == 42 and s['gap_policy'] == 'fail_closed' and len(s['revision']) == 64
    assert s['earliest_available_date'] == '2020-01-01' and 'available_at' in s['available_at_rule']
    assert s['judged_days'] == dict(count=12, first_date=lr.utc_date(T0 + DAY), last_date=lr.utc_date(T0 + 12 * DAY))
    assert (a['threshold_mode'], a['percentile'], a['lookback_days'], a['min_drop_pct'], a['hold_hours'],
            a['take_profit_pct'], a['stop_loss_pct']) == ('percentile', 99, 30, 3, 24, 3, 3)
    assert [(e['day_utc'], e['threshold']) for e in a['entry_thresholds']] == [(lr._iso(T0 + 5 * DAY), '747.1')]
    counts = body['raw_report'][KIND]['counts']
    assert counts['entered'] == 1 and sum(counts.values()) == 12


def test_window_without_a_judged_day_fetches_nothing(monkeypatch, tmp_path):
    calls = install(monkeypatch, tmp_path, store_for({}), candles('1d', {}, n_days=2))
    body = post(n=2)
    assert body['result_status'] == 'success' and body['trades'] == [] and calls == []
    assert body['assumptions'][KIND]['liquidation_series']['count'] == 0


def test_catalog_entry(monkeypatch):
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    tools = {t['tool_id']: t for t in TestClient(p.app).get('/catalog').json()['tools']}
    tool = tools[TOOL]
    assert tool['markets'] == ['spot'] and tool['timeframes'] == ['1h', '4h', '1d']
    props = tool['param_schema']['properties']
    assert props['threshold_mode'] == dict(type='string', default='percentile', enum=['percentile', 'fixed'])
    assert props['percentile'] == dict(type='number', default=99, minimum=50, maximum=100)
    assert props['lookback_days'] == dict(type='integer', default=30, minimum=7, maximum=90)
    assert props['fixed_threshold_usd'] == dict(type='number')
    assert props['min_drop_pct'] == dict(type='number', default=3, minimum=0.5, maximum=30)
    assert props['hold_hours'] == dict(type='integer', default=24, minimum=1, maximum=240)
    assert props['take_profit_pct'] == dict(type='number', default=3, minimum=0.1, maximum=50)
    assert props['stop_loss_pct'] == dict(type='number', default=3, minimum=0.1, maximum=20)
    assert not {'events', 'max_holding_bars', 'time_layer_enabled', 'time_calendar', 'atr_stop_multiplier',
                'direction'} & set(props)
    assert 'required' not in tool['param_schema']
    assert 'daily' in tool['description'] and '2020-01-01' in tool['description']
