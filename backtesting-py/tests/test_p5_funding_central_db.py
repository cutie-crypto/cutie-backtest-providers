"""P5 funding_settlement_reversal 取数改走中心库 /metrics 1h 序列：合成数据金样（不含任何真实费率）。

中心库序列：symbol BTC / exchange Binance / metric funding_rate / interval 1h，值单位 pct。
结算点 S 的判据 = 上一期 P=S-8h 的费率 = ts 为 P-3600 的那一行（按 pct 原值使用，不 ×100）；
本期那行（ts=S-3600）在 S 时刻才可用，既不读也不请求。
合成序列：90 个结算点（30 天 x 3 期）；判据行取互不相同的小值，其余整点填 FILLER（一旦被误读就会越过阈值），
另在若干结算点埋入阈值边界值。
"""
import json
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
from canonical_json import canonical_decimal_str
from strategy_funding_reversal import DEVIATION_NOTE, FundingReversalConfig

KIND = 'funding_settlement_reversal'
TOOL = f'local.backtesting_py.{KIND}'
HOUR = 3600
SLOT = 8 * HOUR
STEP = 900
START = int(datetime(2026, 3, 1, tzinfo=timezone.utc).timestamp())
POINTS = 90
END = START + POINTS * SLOT  # settlements START+8h .. END (90 points, the last one is END itself)
LABELS = ('BTC', 'Binance', 'funding_rate', '1h')
FILLER = '9.99'  # pct, would trade on any threshold if a non-judged hour were ever read
OWN_LAST = '5'  # the own-period row of the LAST settlement (ts = END - 1h); must never enter
ZH_SOURCE = ('费率来源：CoinGlass 结算前 1 小时收盘费率（与币安结算值有微小偏差，90 期样本最大绝对偏差 0.0017 pct）；'
             '仅覆盖中心库 1h 序列范围（当前自 2026-01-08 起）')

# settlement index j (1..90) -> judged pct (the row of period j-1), threshold boundaries at the default 0.05
BOUNDARY = {10: '0.05', 20: '0.050001', 30: '0.049999', 40: '-0.05', 50: '-0.050001', 60: '-0.049999'}
EXPECTED_DIRECTION = {10: 'short', 20: 'short', 30: None, 40: 'long', 50: 'long', 60: None}


def judged_ts(j):
    """Row read at the j-th settlement: P - 1h with P = S_j - 8h."""
    return START + (j - 1) * SLOT - HOUR


def judged_value(j):
    if j in BOUNDARY:
        return BOUNDARY[j]
    return canonical_decimal_str(Decimal((j * 7919) % 2001 - 1000) / Decimal(100000))  # |v| <= 0.01 pct, distinct-ish


def build_table(first=None, last=None):
    """Hourly central rows {ts: value string}, judged hours carry judged_value, every other hour FILLER."""
    first = START - SLOT - HOUR if first is None else first
    last = END + SLOT if last is None else last
    table = {ts: FILLER for ts in range(first, last + 1, HOUR)}
    for j in range(1, POINTS + 1):
        if judged_ts(j) in table:
            table[judged_ts(j)] = judged_value(j)
    table[END - HOUR] = OWN_LAST  # own-period row of the last settlement
    return table


class Central:
    """Mock of ``_fetch_artifact_metric_chunk`` (inclusive bounds) over an hourly table."""

    def __init__(self, table, honor_range=True):
        self.table, self.honor_range, self.calls = dict(table), honor_range, []

    def __call__(self, *, symbol, metric, interval, exchange, start_at, end_at, raise_on_transport_error=False):
        self.calls.append(dict(symbol=symbol, metric=metric, interval=interval, exchange=exchange,
                               start_at=start_at, end_at=end_at))
        if (symbol, exchange, metric, interval) != LABELS:
            return []
        return [{'ts': ts, 'value': v} for ts, v in sorted(self.table.items())
                if not self.honor_range or start_at <= ts <= end_at]


def bind(monkeypatch, table=None, *, start=START, end=END, honor_range=True, central=None):
    central = central or Central(build_table() if table is None else table, honor_range)
    monkeypatch.setattr(p, '_fetch_artifact_metric_chunk', central)
    config = FundingReversalConfig.parse({})
    failure = p._bind_funding_reversal('p5', config, 'BTCUSDT', start, end)
    return config, failure, central


def failure_limitations(response):
    body = json.loads(response.body)
    assert body['result_status'] == 'failed' and body['error_type'] == 'TIME_DATA_GAP'
    assert body['error_message'].startswith('TIME_DATA_GAP:')
    return body['limitations']


# ------------------------------------------------------------------ a / b: which row, which unit

def test_binding_reads_the_p_minus_3600_row_as_pct_unchanged(monkeypatch):
    config, failure, _ = bind(monkeypatch)
    assert failure is None and len(config.events) == POINTS
    for j, (entry_ts, name, expected, actual, info) in enumerate(config.events, start=1):
        settlement = START + j * SLOT
        assert info['settlement_utc'] == datetime.fromtimestamp(settlement, timezone.utc).isoformat()
        assert info['judged_rate_row_ts'] == settlement - SLOT - HOUR == judged_ts(j)
        assert info['judged_rate_period_utc'] == datetime.fromtimestamp(settlement - SLOT, timezone.utc).isoformat()
        # pct as stored: no x100 (neither the string nor the numeric value)
        assert actual == judged_value(j) == info['judged_rate_pct']
        assert Decimal(actual) == Decimal(judged_value(j))
        if Decimal(actual) != 0:
            assert Decimal(actual) != Decimal(judged_value(j)) * 100
        assert Decimal(info['judged_rate']) == Decimal(judged_value(j)) / 100  # the fraction view, derived /100
        assert info['rate_basis'] == 'previous_period_pre_settlement_1h_close'
        assert expected == '0'


def test_filler_hours_and_own_period_rows_are_never_judged(monkeypatch):
    # Even when the mock hands back the whole table (own-period rows, FILLER hours, the huge row of the last own
    # period), only the P-1h rows are bound.
    config, failure, _ = bind(monkeypatch, honor_range=False)
    assert failure is None
    assert [e[3] for e in config.events] == [judged_value(j) for j in range(1, POINTS + 1)]
    assert FILLER not in [e[3] for e in config.events] and OWN_LAST not in [e[3] for e in config.events]


def test_own_period_row_of_the_last_settlement_is_not_even_requested(monkeypatch):
    _, failure, central = bind(monkeypatch)
    assert failure is None and len(central.calls) == 1
    call = central.calls[0]
    assert (call['symbol'], call['exchange'], call['metric'], call['interval']) == LABELS
    assert call['start_at'] == judged_ts(1) == START - HOUR
    assert call['end_at'] == END - SLOT - 1  # inclusive end: stops right after the last judged row (END-8h-1h)
    assert call['end_at'] < END - HOUR  # the own-period row of the last settlement
    returned = [ts for ts in central.table if call['start_at'] <= ts <= call['end_at']]
    assert max(returned) == judged_ts(POINTS) and END - HOUR not in returned


# ------------------------------------------------------------------ c: fail closed on gaps

def test_missing_row_in_the_middle_is_rejected(monkeypatch):
    for ts, label in ((judged_ts(40), 'judged hour'), (judged_ts(40) + 3 * HOUR, 'filler hour')):
        table = build_table()
        del table[ts]
        config, failure, _ = bind(monkeypatch, table)
        assert failure is not None and config.events == (), label
        limits = failure_limitations(failure)
        assert limits['reason'] == 'funding_data_gap'
        assert limits['missing_row_utc'] == datetime.fromtimestamp(ts, timezone.utc).isoformat()
        assert limits['required_first_period_utc'] == datetime.fromtimestamp(START, timezone.utc).isoformat()
        assert limits['required_last_period_utc'] == datetime.fromtimestamp(END - SLOT, timezone.utc).isoformat()


def test_window_earlier_than_the_first_row_is_rejected(monkeypatch):
    table_first = judged_ts(1) + 7 * SLOT  # the table starts 7 periods after what the window needs
    config, failure, _ = bind(monkeypatch, build_table(first=table_first))
    assert config.events == ()
    limits = failure_limitations(failure)
    assert limits['reason'] == 'funding_data_gap'
    assert limits['missing_period_utc'] == datetime.fromtimestamp(START, timezone.utc).isoformat()
    assert limits['settlement_utc'] == datetime.fromtimestamp(START + SLOT, timezone.utc).isoformat()
    # first row exactly on the first needed row is accepted; one hour later is not (derived from the fetched
    # first row, no hard-coded series start)
    config, failure, _ = bind(monkeypatch, build_table(first=judged_ts(1)))
    assert failure is None and len(config.events) == POINTS
    config, failure, _ = bind(monkeypatch, build_table(first=judged_ts(1) + HOUR))
    assert failure_limitations(failure)['reason'] == 'funding_data_gap' and config.events == ()


def test_series_ending_before_the_last_needed_row_is_rejected(monkeypatch):
    config, failure, _ = bind(monkeypatch, build_table(last=judged_ts(POINTS) - HOUR))
    limits = failure_limitations(failure)
    assert limits['reason'] == 'funding_data_gap' and config.events == ()
    assert limits['missing_period_utc'] == datetime.fromtimestamp(END - SLOT, timezone.utc).isoformat()
    assert limits['settlement_utc'] == datetime.fromtimestamp(END, timezone.utc).isoformat()


def test_empty_series_is_a_data_gap_but_a_transport_error_is_unavailable(monkeypatch):
    _, failure, _ = bind(monkeypatch, {})
    assert failure_limitations(failure)['reason'] == 'funding_data_gap'

    def broken(**kwargs):
        raise OSError('central /metrics unreachable')
    _, failure, _ = bind(monkeypatch, central=broken)
    assert failure_limitations(failure)['reason'] == 'funding_history_unavailable'


# ------------------------------------------------------------------ evidence

def test_assumptions_carry_the_new_basis_source_sentence_and_both_granularities(monkeypatch):
    config, failure, _ = bind(monkeypatch)
    assert failure is None
    a = config.assumptions
    assert a['deviation_note'].startswith(ZH_SOURCE) and a['judgement'] == a['deviation_note']
    assert a['deviation_note'] == DEVIATION_NOTE and ' (' in a['deviation_note']
    assert a['rate_basis'] == 'previous_period_pre_settlement_1h_close'
    assert a['data_source'] == 'central_metrics:coinglass_funding_rate_1h'
    assert '已结算' not in a['deviation_note'] and '预测费率' not in a['deviation_note']
    series = a['funding_series']
    assert series['series_interval'] == '1h' and series['series_interval_hours'] == 1
    assert series['settlement_interval_hours'] == 8 and 'interval_hours' not in series
    assert series['central_symbol'] == 'BTC' and series['symbol'] == 'BTCUSDT' and series['unit'] == 'pct'
    assert series['count'] == POINTS and series['gap_policy'] == 'fail_closed'
    assert series['current_period_rate_used'] is False and len(series['rows_sha256']) == 64
    assert series['first_ts'] == judged_ts(1) and series['last_ts'] == judged_ts(POINTS)
    assert a['settlement_points']['count'] == POINTS


def test_assumptions_without_a_settlement_point_use_the_new_values():
    config = FundingReversalConfig.parse({})
    config.unbound_assumptions()
    assert config.assumptions['deviation_note'].startswith(ZH_SOURCE)
    assert config.assumptions['rate_basis'] == 'previous_period_pre_settlement_1h_close'
    assert config.assumptions['data_source'] == 'central_metrics:coinglass_funding_rate_1h'


# ------------------------------------------------------------------ end to end: pct thresholds on the central unit

def test_threshold_boundaries_in_pct_end_to_end(monkeypatch, tmp_path):
    n = POINTS * SLOT // STEP
    data = pd.DataFrame(dict(Open=[100.] * n, High=[100.5] * n, Low=[99.5] * n, Close=[100.] * n, Volume=[1.] * n),
                        index=pd.date_range(datetime.fromtimestamp(START, timezone.utc), periods=n, freq='15min'))
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a, **k: data.copy())
    monkeypatch.setattr(p, '_fetch_artifact_metric_chunk', Central(build_table()))
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **k: None)
    request = dict(run_id='p5', provider_tool_id=TOOL, provider_params={}, symbol='BTCUSDT', market='futures',
                   timeframe='15m', start_at=START, end_at=END, initial_capital='10000', fee_bps='0',
                   slippage_bps='0')
    response = TestClient(p.app).post('/cutie/backtest', json={'backtest': request})
    assert response.status_code == 200
    body = response.json()
    assert body['result_status'] == 'success', body
    records = body['raw_report'][KIND]['events']
    assert len(records) == POINTS
    by_settlement = {r['settlement_utc']: r for r in records}
    for j, direction in EXPECTED_DIRECTION.items():
        record = by_settlement[datetime.fromtimestamp(START + j * SLOT, timezone.utc).isoformat()]
        assert record['judged_rate_pct'] == BOUNDARY[j]
        if direction is None:
            assert record['status'] == 'skipped' and record['reason'] == 'rate_below_threshold'
        else:
            assert record['status'] == 'entered' and record['direction'] == direction
    # the ordinary synthetic rows (|v| <= 0.01 pct) never trade: exactly the four boundary hits enter
    assert body['raw_report'][KIND]['counts']['entered'] == 4
    assert len(body['trades']) == 4


# ------------------------------------------------------------------ transport failure vs. empty series (real HTTP layer)

import io
import urllib.error

CENTRAL_URL = 'https://central.test/v1/internal/market-data'


class FakeOpener:
    """Stands in for ``_CENTRAL_HTTP_OPENER`` so the real ``_fetch_artifact_metric_chunk`` runs."""

    def __init__(self, outcome):
        self.outcome, self.urls = outcome, []

    def open(self, request, timeout=None):
        self.urls.append(request.full_url)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return io.BytesIO(json.dumps(self.outcome).encode())


def install_opener(monkeypatch, outcome, *, configured=True):
    monkeypatch.setattr(p, 'CENTRAL_MARKET_DATA_URL', CENTRAL_URL if configured else '')
    monkeypatch.setattr(p, 'CENTRAL_MARKET_DATA_TOKEN', 't' if configured else '')
    opener = FakeOpener(outcome)
    monkeypatch.setattr(p, '_CENTRAL_HTTP_OPENER', opener)
    return opener


def bind_real_http(monkeypatch, outcome, *, configured=True):
    opener = install_opener(monkeypatch, outcome, configured=configured)
    config = FundingReversalConfig.parse({})
    failure = p._bind_funding_reversal('p5', config, 'BTCUSDT', START, END)
    return failure, opener


HTTP_ERROR = urllib.error.HTTPError(CENTRAL_URL, 502, 'bad gateway', None, None)


@pytest.mark.parametrize('outcome,configured', [
    (HTTP_ERROR, True),
    (urllib.error.URLError('connection refused'), True),
    (TimeoutError('timed out'), True),
    ({'err_code': 500, 'data': {'items': []}}, True),
    ({'err_code': 100, 'data': {'items': []}}, False),
], ids=['http_error', 'url_error', 'timeout', 'err_code_not_100', 'not_configured'])
def test_transport_failure_is_unavailable_not_a_data_gap(monkeypatch, outcome, configured):
    failure, opener = bind_real_http(monkeypatch, outcome, configured=configured)
    body = json.loads(failure.body)
    assert body['error_type'] == 'TIME_DATA_GAP'
    assert body['limitations']['reason'] == 'funding_history_unavailable'
    assert body['limitations']['symbol'] == 'BTCUSDT'
    assert 'missing_row_utc' not in body['limitations']
    assert bool(opener.urls) is configured  # the real HTTP layer was reached unless unconfigured


def test_http_200_with_empty_items_is_still_a_data_gap(monkeypatch):
    failure, opener = bind_real_http(monkeypatch, {'err_code': 100, 'data': {'items': []}})
    assert opener.urls
    assert failure_limitations(failure)['reason'] == 'funding_data_gap'


def test_default_fetch_keeps_returning_an_empty_list_on_transport_errors(monkeypatch):
    """P1 / generic feature streams rely on the historical swallow-to-empty behaviour."""
    kwargs = dict(symbol='BTC', metric='funding_rate', interval='1h', exchange='Binance', start_at=START, end_at=END)
    for outcome, configured in ((HTTP_ERROR, True), ({'err_code': 500}, True), ({}, False)):
        install_opener(monkeypatch, outcome, configured=configured)
        assert p._fetch_artifact_metric_chunk(**kwargs) == []
        with pytest.raises(p.CentralMetricTransportError):
            p._fetch_artifact_metric_chunk(**kwargs, raise_on_transport_error=True)


# (result_status, error_type, limitations.reason, error_message) of the fear & greed path under the same HTTPError,
# recorded before the opt-in flag existed (base commit 69f34a4).
P1_EXPECTED = (
    'failed', 'TIME_DATA_GAP', 'fear_greed_data_gap',
    'TIME_DATA_GAP:fear_greed_index series is incomplete for the backtest window 2024-01-01..2024-01-06 '
    '(declared feature stream is empty or contains duplicate timestamps); '
    'this template does not run on a partial series',
)


def test_p1_fear_greed_with_http_error_is_unchanged(monkeypatch, tmp_path):
    day = 86400
    start = 1704067200  # 2024-01-01
    times = [start - day + i * day for i in range(7)]
    frame = pd.DataFrame(dict(Open=[100] * 7, Close=[100] * 7, High=[101] * 7, Low=[99] * 7, Volume=[10] * 7),
                         index=pd.to_datetime(times, unit='s'))
    monkeypatch.setattr(p, 'REPORTS_DIR', tmp_path / 'reports')

    def fetch_ohlcv(exchange, market, symbol, timeframe, start_at, end_at):
        return frame.loc[(frame.index >= pd.to_datetime(start_at, unit='s'))
                         & (frame.index < pd.to_datetime(end_at, unit='s'))].copy()

    monkeypatch.setattr(p, '_fetch_ohlcv', fetch_ohlcv)
    install_opener(monkeypatch, HTTP_ERROR)
    payload = dict(run_id='fg_http', provider_tool_id='local.backtesting_py.fear_greed_scale_in',
                   provider_params={'exchange': 'binance', 'buy_notional': 100}, symbol='BTCUSDT', market='spot',
                   timeframe='1d', start_at=start, end_at=start + 6 * day, initial_capital='10000',
                   fee_bps='0', slippage_bps='0')
    body = TestClient(p.app).post('/cutie/backtest', json={'backtest': payload}).json()
    got = (body['result_status'], body['error_type'], body['limitations'].get('reason'), body['error_message'])
    print('P1_BASELINE', repr(got))
    assert got == P1_EXPECTED
