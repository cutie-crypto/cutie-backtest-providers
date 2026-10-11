"""P2 funding_settlement_reversal: 15m hand-computed candles, mocked central 1h funding series (no network).

Grid: 15m candles from T0 (2026-01-01 00:00 UTC). Funding period k starts at T0 + k * 8h; since P5 its rate
is the 1h row opened at T0 + k * 8h - 1h (value in pct). The settlement point S_k = T0 + k * 8h judges on
period k-1's row (the previous period) and, with lead_minutes=30, enters at the open of bar 32k - 2.
The 1h hours that are not a judged row hold FILLER (a huge rate that would trade if ever read).
"""
import sys
from pathlib import Path

from decimal import Decimal

import pandas as pd
import pytest
from backtesting import Backtest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p

KIND = 'funding_settlement_reversal'
TOOL = f'local.backtesting_py.{KIND}'
T0 = 1767225600
STEP = 900
SLOT = 28800
BARS = 192  # two days; settlements k=1..6 have their entry inside the window


def bar_of(k, lead=30):
    return (k * SLOT - lead * 60) // STEP


def frame(n=BARS):
    return pd.DataFrame(dict(Open=[100.] * n, High=[100.5] * n, Low=[99.5] * n, Close=[100.] * n,
                             Volume=[1.] * n),
                        index=pd.date_range('2026-01-01', periods=n, freq='15min'))


HOUR = 3600
FILLER = '9.99'  # pct: any read of a non-judged hour would flip the direction / pass the threshold
LABELS = ('BTC', 'Binance', 'funding_rate', '1h')


def central_rows(rates):
    """Hourly central rows covering the judged rows of ``rates`` ({k: pct string}); other hours FILLER."""
    first, last = min(rates) * SLOT - HOUR, max(rates) * SLOT - HOUR
    rows = {}
    for ts in range(T0 + first, T0 + last + 1, HOUR):
        rows[ts] = FILLER
    for k, pct in rates.items():
        rows[T0 + k * SLOT - HOUR] = pct
    return rows


def install(monkeypatch, tmp_path, rates, data=None, *, drop=(), honor_range=True, fail=None, ohlcv_calls=None,
            last_period=None):
    """rates: {period index k: rate string as a decimal FRACTION}. Periods missing from ``rates`` default to 0.

    The mock central series holds the same rate in pct (x100, the unit of the central table).
    """
    full = {k: str(Decimal(rates.get(k, '0.00000000')) * 100) for k in range(-1, 8)}
    store = central_rows(full)
    for k in drop:  # the whole hour row of that period is absent (a hole in the 1h series)
        del store[T0 + k * SLOT - HOUR]
    if last_period is not None:  # the central table ends with that period's row (series ends early)
        store = {ts: v for ts, v in store.items() if ts <= T0 + last_period * SLOT - HOUR}
    if len(drop) == len(full):  # nothing left in the central table
        store = {}
    calls = []
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', tmp_path)
    frame_data = frame() if data is None else data

    def fetch_ohlcv(*a, **k):
        if ohlcv_calls is not None:
            ohlcv_calls.append(a)
        return frame_data.copy()

    def fetch_chunk(*, symbol, metric, interval, exchange, start_at, end_at, raise_on_transport_error=False):
        calls.append(dict(symbol=symbol, metric=metric, interval=interval, exchange=exchange,
                          start_at=start_at, end_at=end_at))
        if fail:
            raise fail
        if (symbol, exchange, metric, interval) != LABELS:
            return []
        return [{'ts': ts, 'value': v} for ts, v in sorted(store.items())
                if not honor_range or start_at <= ts <= end_at]

    def no_warmup(*a, **k):
        pytest.fail('funding_settlement_reversal must fetch zero warmup')

    monkeypatch.setattr(p, '_fetch_ohlcv', fetch_ohlcv)
    monkeypatch.setattr(p, '_fetch_template_warmup', no_warmup)
    monkeypatch.setattr(p, '_fetch_artifact_metric_chunk', fetch_chunk)
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **k: None)
    return calls


def post(values=None, *, market='futures', timeframe='15m', symbol='BTCUSDT', n=BARS, **extra):
    request = dict(run_id='p2', provider_tool_id=TOOL, provider_params=values if values is not None else {},
                   symbol=symbol, market=market, timeframe=timeframe, start_at=T0, end_at=T0 + n * STEP,
                   initial_capital='10000', fee_bps='0', slippage_bps='0', **extra)
    response = TestClient(p.app).post('/cutie/backtest', json={'backtest': request})
    assert response.status_code == 200
    return response.json()


def run(monkeypatch, tmp_path, rates, values=None, data=None, **kw):
    calls = install(monkeypatch, tmp_path, rates, data, **kw)
    body = post(values)
    assert body['result_status'] == 'success', body
    return body, calls


def legs(body):
    return [((t['opened_at'] - T0) // STEP, t['entry_price'], (t['closed_at'] - T0) // STEP,
             t['exit_price'], t['side']) for t in body['trades']]


def events(body):
    return body['raw_report'][KIND]['events']


# ------------------------------------------------------------------ direction and threshold

def test_positive_rate_opens_short_and_exits_on_hold(monkeypatch, tmp_path):
    body, _ = run(monkeypatch, tmp_path, {0: '0.00060000'})
    assert legs(body) == [(bar_of(1), '100', bar_of(1) + 2, '100', 'short')]
    first = events(body)[0]
    assert first['status'] == 'entered' and first['direction'] == 'short'
    assert first['exit_reason'] == 'time_expiry'
    assert first['settlement_utc'] == '2026-01-01T08:00:00+00:00'
    assert first['judged_rate_period_utc'] == '2026-01-01T00:00:00+00:00'
    assert first['judged_rate'] == '0.0006' and first['judged_rate_pct'] == '0.06'


def test_negative_rate_opens_long(monkeypatch, tmp_path):
    body, _ = run(monkeypatch, tmp_path, {0: '-0.00060000'})
    assert legs(body) == [(bar_of(1), '100', bar_of(1) + 2, '100', 'long')]


def test_threshold_boundary_triggers_and_below_does_not(monkeypatch, tmp_path):
    at_edge, _ = run(monkeypatch, tmp_path, {0: '0.00050000', 2: '-0.00050000'})
    assert [(leg[0], leg[4]) for leg in legs(at_edge)] == [(bar_of(1), 'short'), (bar_of(3), 'long')]
    below, _ = run(monkeypatch, tmp_path, {0: '0.00049999', 2: '-0.00049999'})
    assert below['trades'] == []
    assert {r['reason'] for r in events(below)} == {'rate_below_threshold'}
    assert all(r['status'] == 'skipped' for r in events(below))


def test_custom_threshold_and_lead_and_hold(monkeypatch, tmp_path):
    values = dict(rate_threshold_pct=0.1, lead_minutes=60, hold_minutes=60)
    body, _ = run(monkeypatch, tmp_path, {0: '0.00090000', 1: '0.00100000'}, values)
    # 0.09% < 0.1% skips S1; period 1 = 0.10% reaches the threshold for S2.
    assert legs(body) == [(bar_of(2, 60), '100', bar_of(2, 60) + 4, '100', 'short')]


# ------------------------------------------------------------------ no look-ahead

def test_current_period_rate_never_triggers(monkeypatch, tmp_path):
    # Period 1 (S2's judged rate) is huge, period 0 (S1's judged rate) is tiny: S1 must NOT trade on
    # period 1's rate even though that row is sitting in the history the mock hands back.
    body, calls = run(monkeypatch, tmp_path, {0: '0.00010000', 1: '0.00090000'}, honor_range=False)
    assert [(leg[0], leg[4]) for leg in legs(body)] == [(bar_of(2), 'short')]
    assert events(body)[0]['status'] == 'skipped' and events(body)[0]['reason'] == 'rate_below_threshold'
    assert events(body)[0]['judged_rate_pct'] == '0.01'
    assert events(body)[1]['judged_rate_pct'] == '0.09'
    # Every event judged on the period right before its settlement, never its own.
    for record in events(body):
        settlement = pd.Timestamp(record['settlement_utc'])
        assert pd.Timestamp(record['judged_rate_period_utc']) == settlement - pd.Timedelta(hours=8)


def test_previous_period_rate_triggers_even_if_current_is_flat(monkeypatch, tmp_path):
    body, _ = run(monkeypatch, tmp_path, {0: '0.00090000', 1: '0.00010000'})
    assert [(leg[0], leg[4]) for leg in legs(body)] == [(bar_of(1), 'short')]


def test_fetch_never_requests_the_settlement_period_row(monkeypatch, tmp_path):
    _, calls = run(monkeypatch, tmp_path, {0: '0.00090000'})
    assert len(calls) == 1 and calls[0]['symbol'] == 'BTC'
    # Last settlement in the window is k=6, whose previous period is k=5 (row ts T0 + 5*SLOT - 1h): the
    # request (inclusive end) stops right after it, so the own-period row of S_6 is never asked for.
    assert calls[0]['start_at'] == T0 - HOUR
    assert calls[0]['end_at'] == T0 + 5 * SLOT - 1
    assert calls[0]['end_at'] < T0 + 6 * SLOT - HOUR


# ------------------------------------------------------------------ exits and one trade per settlement

def test_stop_loss_exits_next_open_and_does_not_reenter(monkeypatch, tmp_path):
    data = frame()
    entry = bar_of(1)
    data.iloc[entry + 1] = [100, 102, 99.5, 100, 1]  # short entry 100, stop 101 hit intrabar
    data.iloc[entry + 2:] = [98, 98.5, 97.5, 98, 1]
    body, _ = run(monkeypatch, tmp_path, {0: '0.00090000'}, data=data)
    assert legs(body) == [(entry, '100', entry + 2, '98', 'short')]
    assert events(body)[0]['exit_reason'] == 'stop_loss'
    assert len(body['trades']) == 1


def test_wider_stop_lets_the_same_move_ride_to_hold_expiry(monkeypatch, tmp_path):
    data = frame()
    entry = bar_of(1)
    data.iloc[entry + 1] = [100, 102, 99.5, 100, 1]
    body, _ = run(monkeypatch, tmp_path, {0: '0.00090000'}, dict(stop_loss_pct=3), data=data)
    assert events(body)[0]['exit_reason'] == 'time_expiry'


def test_one_trade_per_settlement(monkeypatch, tmp_path):
    rates = {k: '0.00090000' for k in range(0, 6)}
    body, _ = run(monkeypatch, tmp_path, rates, dict(lead_minutes=120, hold_minutes=240))
    # S1..S5 each trade once; S6's 4h hold would run past the last candle, so it is skipped, not shortened.
    assert [leg[0] for leg in legs(body)] == [bar_of(k, 120) for k in range(1, 6)]
    assert len(events(body)) == 6 and len(body['trades']) == 5
    assert [r['status'] for r in events(body)] == ['entered'] * 5 + ['skipped_out_of_range']
    assert events(body)[5]['reason'] == 'window_past_data_end'
    assert len({r['settlement_utc'] for r in events(body)}) == 6


def test_schema_caps_keep_settlements_independent():
    # lead <= 120 and hold <= 240 so a trade entered for S_k is flat long before S_{k+1}'s entry (8h later).
    spec = p.TOOL_SPECS[TOOL]['param_schema_properties']
    assert spec['hold_minutes']['maximum'] + spec['lead_minutes']['maximum'] < 8 * 60


# ------------------------------------------------------------------ fail-closed funding series

def test_missing_period_fails_closed_before_ohlcv(monkeypatch, tmp_path):
    ohlcv = []
    install(monkeypatch, tmp_path, {0: '0.00090000'}, drop=(2,), ohlcv_calls=ohlcv)
    body = post()
    assert body['result_status'] == 'failed' and body['error_type'] == 'TIME_DATA_GAP'
    assert body['limitations']['reason'] == 'funding_data_gap'
    assert body['limitations']['missing_row_utc'] == '2026-01-01T15:00:00+00:00'
    assert body['limitations']['required_first_period_utc'] == '2026-01-01T00:00:00+00:00'
    assert body['error_message'].startswith('TIME_DATA_GAP:')
    assert ohlcv == []


def test_empty_series_and_unavailable_history_fail_closed(monkeypatch, tmp_path):
    install(monkeypatch, tmp_path, {}, honor_range=True, drop=tuple(range(-1, 8)))
    body = post()
    assert body['error_type'] == 'TIME_DATA_GAP' and body['limitations']['reason'] == 'funding_data_gap'
    install(monkeypatch, tmp_path, {}, fail=ValueError('funding range exceeds page budget'))
    body = post()
    assert body['error_type'] == 'TIME_DATA_GAP' and body['limitations']['reason'] == 'funding_history_unavailable'


def test_series_ending_early_names_the_missing_period(monkeypatch, tmp_path):
    # Periods 4 and 5 absent at the END of the series (no mid-series gap): bind names the period/settlement.
    install(monkeypatch, tmp_path, {0: '0.00090000'}, last_period=3)
    body = post()
    assert body['limitations']['reason'] == 'funding_data_gap'
    assert body['limitations']['missing_period_utc'] == '2026-01-02T08:00:00+00:00'
    assert body['limitations']['settlement_utc'] == '2026-01-02T16:00:00+00:00'


# ------------------------------------------------------------------ parameter and market validation

@pytest.mark.parametrize('values', [
    dict(lead_minutes=20), dict(lead_minutes=40), dict(lead_minutes=5), dict(lead_minutes=0),
    dict(lead_minutes=135), dict(lead_minutes=30.0), dict(lead_minutes=True), dict(lead_minutes='30'),
    dict(hold_minutes=50), dict(hold_minutes=10), dict(hold_minutes=255), dict(hold_minutes=0),
    dict(hold_minutes=30.5),
    dict(rate_threshold_pct=0.001), dict(rate_threshold_pct=1.5), dict(rate_threshold_pct='0.05'),
    dict(stop_loss_pct=0.05), dict(stop_loss_pct=11), dict(stop_loss_pct=True),
    dict(max_holding_bars=4), dict(time_layer_enabled=True), dict(atr_stop_multiplier=2),
    dict(take_profit_pct=2), dict(unknown=1), dict(events=[]),
])
def test_invalid_params_reject_before_any_fetch(monkeypatch, tmp_path, values):
    ohlcv = []
    calls = install(monkeypatch, tmp_path, {0: '0.0009'}, ohlcv_calls=ohlcv)
    body = post(values)
    assert body['result_status'] == 'failed' and body['error_type'] == 'INVALID_PARAMS'
    assert calls == [] and ohlcv == []


def test_alignment_message_names_the_15_minute_grid(monkeypatch, tmp_path):
    install(monkeypatch, tmp_path, {})
    assert 'multiple of 15' in post(dict(lead_minutes=20))['error_message']
    assert 'multiple of 15' in post(dict(hold_minutes=50))['error_message']


@pytest.mark.parametrize('values', [dict(lead_minutes=15, hold_minutes=15), dict(lead_minutes=120, hold_minutes=240),
                                    dict(rate_threshold_pct=0.005, stop_loss_pct=0.1),
                                    dict(rate_threshold_pct=1, stop_loss_pct=10)])
def test_range_edges_are_accepted(monkeypatch, tmp_path, values):
    run(monkeypatch, tmp_path, {0: '0.00090000'}, values)


def test_spot_is_rejected(monkeypatch, tmp_path):
    calls = install(monkeypatch, tmp_path, {0: '0.0009'})
    body = post(market='spot')
    assert body['error_type'] == 'INVALID_PARAMS' and 'futures' in body['error_message']
    assert calls == []


def test_only_15m_and_binance(monkeypatch, tmp_path):
    calls = install(monkeypatch, tmp_path, {0: '0.0009'})
    assert post(timeframe='5m')['error_type'] == 'TIMEFRAME_UNSUPPORTED'
    assert post(timeframe='1h')['error_type'] == 'TIMEFRAME_UNSUPPORTED'
    body = post(dict(exchange='okx'))
    assert body['error_type'] == 'INVALID_PARAMS' and 'binance' in body['error_message']
    assert post(symbol='BTCUSD')['error_type'] == 'INVALID_PARAMS'
    assert calls == []


def test_exchange_binance_and_perp_symbol_forms_are_accepted(monkeypatch, tmp_path):
    install(monkeypatch, tmp_path, {0: '0.00090000'})
    assert post(dict(exchange='binance'))['result_status'] == 'success'
    calls = install(monkeypatch, tmp_path, {0: '0.00090000'})
    assert post(symbol='BTC/USDT:USDT')['result_status'] == 'success'
    assert calls[0]['symbol'] == 'BTC'


def test_leverage_key_is_consumed(monkeypatch, tmp_path):
    body, _ = run(monkeypatch, tmp_path, {0: '0.00090000'}, dict(leverage=2))
    assert body['result_status'] == 'success' and len(body['trades']) == 1


@pytest.mark.parametrize('values', [None, dict(leverage=1)], ids=['default', 'leverage_one'])
def test_leverage_one_off_state_skips_isolated_margin(monkeypatch, tmp_path, values):
    # test_isolated_liquidation_arbitration 的冻结基线早于本模板、无从逐字节比对，这里显式钉住 L=1 关态：
    # 不进逐仓强平仲裁、不装结算，assumptions / raw_report 不出现逐仓证据。
    def forbidden(*a, **k):
        pytest.fail('L=1 called liquidation arbitration or settlement installation')
    monkeypatch.setattr(p, '_isolated_liquidation_candidate', forbidden)
    monkeypatch.setattr(p._FixedRiskMixin, '_isolated_install_settlement', forbidden)
    body, _ = run(monkeypatch, tmp_path, {0: '0.00090000'}, values)
    assert len(body['trades']) == 1
    assert 'isolated_margin' not in body['assumptions'] and 'isolated_risk' not in body['raw_report']


# ------------------------------------------------------------------ evidence and catalog

def test_assumptions_state_basis_range_and_deviation(monkeypatch, tmp_path):
    body, _ = run(monkeypatch, tmp_path, {0: '0.00090000'})
    a = body['assumptions'][KIND]
    assert a['rate_basis'] == 'previous_period_pre_settlement_1h_close'
    assert a['data_source'] == 'central_metrics:coinglass_funding_rate_1h'
    assert '结算前 1 小时收盘费率' in a['deviation_note'] and '偏差' in a['deviation_note']
    assert '上一期已结算费率' not in a['deviation_note'] and a['judgement'] == a['deviation_note']
    series = a['funding_series']
    assert series['exchange'] == 'binance' and series['market'] == 'usdt_perpetual'
    assert series['symbol'] == 'BTCUSDT' and series['central_symbol'] == 'BTC'
    assert series['series_interval_hours'] == 1 and series['settlement_interval_hours'] == 8
    assert series['first_period_utc'] == '2026-01-01T00:00:00+00:00'
    assert series['last_period_utc'] == '2026-01-02T16:00:00+00:00'
    assert series['count'] == 6 and series['gap_policy'] == 'fail_closed'
    assert series['current_period_rate_used'] is False and len(series['rows_sha256']) == 64
    assert a['settlement_points'] == dict(count=6, first_utc='2026-01-01T08:00:00+00:00',
                                          last_utc='2026-01-03T00:00:00+00:00')
    assert (a['lead_minutes'], a['hold_minutes'], a['rate_threshold_pct'], a['stop_loss_pct']) == (30, 30, 0.05, 1)
    assert body['raw_report'][KIND]['counts'] == {'entered': 1, 'skipped': 5}


def test_catalog_entry(monkeypatch):
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    tools = {t['tool_id']: t for t in TestClient(p.app).get('/catalog').json()['tools']}
    tool = tools[TOOL]
    assert tool['markets'] == ['futures'] and tool['timeframes'] == ['15m']
    props = tool['param_schema']['properties']
    assert {'lead_minutes', 'hold_minutes', 'rate_threshold_pct', 'stop_loss_pct', 'exchange', 'leverage'} <= set(props)
    assert props['lead_minutes'] == dict(type='integer', default=30, minimum=15, maximum=120)
    assert props['hold_minutes'] == dict(type='integer', default=30, minimum=15, maximum=240)
    assert props['rate_threshold_pct'] == dict(type='number', default=0.05, minimum=0.005, maximum=1)
    assert props['stop_loss_pct'] == dict(type='number', default=1, minimum=0.1, maximum=10)
    assert props['exchange']['default'] == 'binance'
    assert not {'events', 'max_holding_bars', 'time_layer_enabled', 'time_calendar', 'atr_stop_multiplier',
                'take_profit_pct'} & set(props)
    assert 'required' not in tool['param_schema']
    assert len(tools) == 67  # S3 top_long_short_reversal +1, S4 liquidation_reversal +1


def test_window_without_settlement_point_fetches_nothing(monkeypatch, tmp_path):
    calls = install(monkeypatch, tmp_path, {0: '0.00090000'}, frame(28))
    body = post(n=28)  # 00:00..07:00, the first entry (07:30) lies past the window
    assert body['result_status'] == 'success' and body['trades'] == [] and calls == []
    a = body['assumptions'][KIND]
    assert a['settlement_points'] == dict(count=0) and a['funding_series']['count'] == 0
