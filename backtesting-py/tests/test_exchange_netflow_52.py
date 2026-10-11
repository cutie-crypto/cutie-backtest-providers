"""C52 hand-counted daily fixtures; synthetic central data, no network."""
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
import strategy_exchange_netflow as nf
TOOL = 'local.backtesting_py.exchange_netflow'
DAY = 86400
T0 = int(datetime(2026, 3, 2, tzinfo=timezone.utc).timestamp())
VALUES = {0: -3, 1: -2, 2: -1, 3: 1, 4: 2}
TRADE_KEYS = {'seq', 'side', 'opened_at', 'closed_at', 'entry_price', 'exit_price', 'qty', 'fee', 'slippage', 'pnl'}


def frame(n=8, overrides=None):
    rows = [[100, 101, 99.5, 100, 1] for _ in range(n)]
    for day, prices in (overrides or {}).items():
        rows[day] = list(prices) + [1]
    return pd.DataFrame(rows, columns=['Open', 'High', 'Low', 'Close', 'Volume'],
                        index=pd.date_range(pd.Timestamp(T0, unit='s'), periods=n, freq='D'))


def run(monkeypatch, tmp_path, values=None, params=None, *, data=None, missing=(), fail=None,
        symbol='BTCUSDT', market='spot', timeframe='1d', forbidden=False, extra=(), end_at=None):
    data = frame() if data is None else data
    store = {T0 + k * DAY: str((VALUES if values is None else values).get(k, 0)) for k in range(-40, len(data))}
    for day in missing:
        store.pop(T0 + day * DAY)
    calls = []
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **k: None)
    def ohlcv(*a, **k):
        if forbidden:
            pytest.fail('validation must precede all fetches')
        return data.copy()
    def chunk(**kw):
        if forbidden:
            pytest.fail('validation must precede all fetches')
        calls.append(kw)
        if fail:
            raise fail
        assert kw['symbol'] in ('BTC', 'ETH')
        assert (kw['exchange'], kw['metric'], kw['interval']) == ('AGGREGATED', 'exchange_netflow', '1d')
        assert kw['raise_on_transport_error'] is True
        return [{'ts': ts, 'value': value, 'fetched_at': 0} for ts, value in sorted(store.items())
                if kw['start_at'] <= ts <= kw['end_at']] + list(extra)
    monkeypatch.setattr(p, '_fetch_ohlcv', ohlcv)
    monkeypatch.setattr(p, '_fetch_artifact_metric_chunk', chunk)
    monkeypatch.setattr(p, '_fetch_template_warmup', lambda *a, **k: pytest.fail('C52 needs no candle prewarm'))
    req = dict(run_id='c52', provider_tool_id=TOOL, provider_params=params or {}, symbol=symbol,
               market=market, timeframe=timeframe, start_at=T0,
               end_at=T0 + len(data) * DAY if end_at is None else end_at,
               initial_capital='10000', fee_bps='0', slippage_bps='0')
    response = TestClient(p.app).post('/cutie/backtest', json={'backtest': req})
    assert response.status_code == 200
    return response.json(), calls


def test_entry_exit_catalog_and_frozen_result(monkeypatch, tmp_path):
    data = frame(overrides={3: [120, 121, 119, 120], 4: [125, 126, 124, 125], 5: [130, 131, 129, 130]})
    for symbol in ('BTCUSDT', 'ETH/USDC', 'BTCBUSD'):
        body, calls = run(monkeypatch, tmp_path, data=data, symbol=symbol)
        assert body['result_status'] == 'success', body
        trade, = body['trades']
        assert (trade['opened_at'], trade['closed_at']) == (T0 + 3 * DAY, T0 + 5 * DAY)
        assert (trade['entry_price'], trade['exit_price']) == ('120', '130')
        assert set(trade) == TRADE_KEYS
        assert set(body['metrics']) == {'total_return', 'max_drawdown', 'trade_count'}
        assert set(body['data_manifest']) == {'source', 'symbol', 'market', 'timeframe', 'start_at', 'end_at', 'kline_count', 'checksum', 'checksum_algo'}
        evidence, = body['raw_report']['exchange_netflow']['executions']
        assert evidence['exit_reason'] == 'netflow_inflow' and evidence['signal_date'] == T0 + 2 * DAY
        assert Decimal(evidence['stop_price']) == Decimal('116.4')
        assert body['raw_report']['trade_exit_kinds'] == [{'seq': 1, 'exit_kind': 'signal_exit'}]
        assert calls[0]['start_at'] == T0 - DAY  # day1 first decision needs days -1,0,1
        assert calls[0]['end_at'] == T0 + 7 * DAY - 1  # last decision day6, no day7
        assert body['assumptions']['exchange_netflow']['balance_snapshots'] == '余额快照为上游日终值、按日期 +1 天可读；缺所当日不计入合计'
    spec = p.TOOL_SPECS[TOOL]
    assert spec['markets'] == ['spot'] and spec['timeframes'] == ['1d'] and not spec['is_default']
    assert spec['description'].endswith('做空镜像未开放')
    assert spec['build']({})['executed_name'] == 'Exchange Netflow'
    assert nf.ExchangeNetflowConfig.parse({}).values == dict(outflow_days=3, inflow_exit_days=2, stop_loss_pct=3)


def test_zero_is_neither_outflow_nor_inflow(monkeypatch, tmp_path):
    params = dict(outflow_days=1, inflow_exit_days=1)
    body, _ = run(monkeypatch, tmp_path, {1: -1, 2: 0, 3: 0, 4: 1}, params)
    trade, = body['trades']
    assert trade['opened_at'] == T0 + 2 * DAY and trade['closed_at'] == T0 + 5 * DAY
    assert body['raw_report']['exchange_netflow']['executions'][0]['exit_reason'] == 'netflow_inflow'
    body, _ = run(monkeypatch, tmp_path, {}, params)
    assert body['result_status'] == 'success' and body['trades'] == []
    body, _ = run(monkeypatch, tmp_path, {0: -1, 1: 0, 2: -1}, dict(outflow_days=3, inflow_exit_days=1))
    assert body['trades'] == []


def test_intrabar_stop_actual_fill_and_risk_percentage_sizing(monkeypatch, tmp_path):
    data = frame(overrides={3: [120, 121, 116.4, 120], 4: [90, 91, 89.5, 90]})
    body, _ = run(monkeypatch, tmp_path, data=data,
                  params=dict(position_size_risk_pct=1, position_size_qty_step=.01))
    trade, = body['trades']
    assert (trade['opened_at'], trade['closed_at']) == (T0 + 3 * DAY, T0 + 4 * DAY)
    assert (trade['entry_price'], trade['exit_price']) == ('120', '90')
    assert Decimal(trade['qty']) == Decimal('27.77')  # $100 / (120 - 116.4), floored to .01
    evidence, = body['raw_report']['exchange_netflow']['executions']
    assert evidence['exit_reason'] == 'stop_loss' and Decimal(evidence['stop_price']) == Decimal('116.4')
    assert body['raw_report']['trade_exit_kinds'] == [{'seq': 1, 'exit_kind': 'stop_loss'}]


def test_day_value_available_only_at_its_close_and_no_lookahead(monkeypatch, tmp_path):
    body, _ = run(monkeypatch, tmp_path, {2: -1, 3: 1}, dict(outflow_days=1, inflow_exit_days=1))
    trade, = body['trades']
    assert (trade['opened_at'], trade['closed_at']) == (T0 + 3 * DAY, T0 + 4 * DAY)
    config = nf.ExchangeNetflowConfig.parse(dict(outflow_days=1, inflow_exit_days=1))
    required = tuple(T0 + k * DAY for k in (1, 2, 3))
    config.bind([dict(ts=t, value=v, available_at=0, fetched_at=0) for t, v in zip(required, (0, -1, 1))],
                symbol='BTC', required=required)
    assert config.window(T0 + 3 * DAY - 1, 1)[0]['ts'] == T0 + DAY
    assert config.window(T0 + 3 * DAY, 1)[0]['ts'] == T0 + 2 * DAY
    config.bind([dict(ts=t, value=v) for t, v in zip(required, (0, -99, -99))], symbol='BTC', required=required)
    assert config.window(T0 + 3 * DAY - 1, 1)[0]['value'] == 0


def test_missing_calendar_day_fails_whole_run_with_date_details(monkeypatch, tmp_path):
    body, _ = run(monkeypatch, tmp_path, missing=(5,))  # Saturday must exist too
    assert body['result_status'] == 'failed' and body['error_type'] == 'TIME_DATA_GAP'
    assert body['limitations']['reason'] == 'exchange_netflow_data_gap'
    assert body['limitations']['required_first_date'] == '2026-03-01'
    assert body['limitations']['required_last_date'] == '2026-03-08'
    assert 'required_first_ts' not in body['limitations'] and 'required_last_ts' not in body['limitations']
    assert not body.get('trades')
    for extra, missing in (((), (-1,)), ((dict(ts=T0 + DAY, value='NaN'),), ())):
        body, _ = run(monkeypatch, tmp_path, extra=extra, missing=missing)
        assert body['limitations']['reason'] == 'exchange_netflow_data_gap'


def test_central_unavailable_is_history_failure(monkeypatch, tmp_path):
    body, _ = run(monkeypatch, tmp_path, fail=p.CentralMetricTransportError('unreachable'))
    assert body['result_status'] == 'failed' and body['error_type'] == 'TIME_DATA_GAP'
    assert body['limitations']['reason'] == 'exchange_netflow_history_unavailable'
    assert body['limitations']['required_first_date'] == '2026-03-01'
    assert body['limitations']['required_last_date'] == '2026-03-08'


def test_invalid_scope_and_parameters_fail_before_fetch(monkeypatch, tmp_path):
    for kw, params, error in ((dict(market='futures'), {}, 'INVALID_PARAMS'),
        (dict(timeframe='4h'), {}, 'TIMEFRAME_UNSUPPORTED'), (dict(symbol='SOLUSDT'), {}, 'INVALID_PARAMS'),
        (dict(symbol='BTCUSDT:USDT'), {}, 'INVALID_PARAMS'), (dict(symbol='BTCUSD'), {}, 'INVALID_PARAMS'),
        ({}, dict(outflow_days=True), 'INVALID_PARAMS'), ({}, dict(inflow_exit_days=False), 'INVALID_PARAMS'),
        ({}, dict(outflow_days=20.5), 'INVALID_PARAMS'), ({}, dict(inflow_exit_days=20.5), 'INVALID_PARAMS'),
        ({}, dict(outflow_days=0), 'INVALID_PARAMS'), ({}, dict(inflow_exit_days=31), 'INVALID_PARAMS'),
        ({}, dict(stop_loss_pct=.09), 'INVALID_PARAMS'), ({}, dict(stop_loss_pct=51), 'INVALID_PARAMS'),
        ({}, dict(take_profit_pct=3), 'INVALID_PARAMS'), ({}, dict(direction='short'), 'INVALID_PARAMS')):
        body, calls = run(monkeypatch, tmp_path, params=params, forbidden=True, **kw)
        assert body['result_status'] == 'failed' and body['error_type'] == error, body
        assert calls == []
    config = nf.ExchangeNetflowConfig.parse(dict(outflow_days=20.0, inflow_exit_days=20.0))
    assert config.values['outflow_days'] == config.values['inflow_exit_days'] == 20
    assert type(config.values['outflow_days']) is type(config.values['inflow_exit_days']) is int
    body, _ = run(monkeypatch, tmp_path, {}, dict(outflow_days=20.0, inflow_exit_days=20.0))
    assert body['result_status'] == 'success'


def test_latest_final_bar_needs_no_row_and_extra_dates_discarded(monkeypatch, tmp_path):
    body, calls = run(monkeypatch, tmp_path, missing=(7,),
                      extra=(dict(ts=T0 + 7 * DAY, value='unreadable'), dict(ts=T0 - 20 * DAY, value='bad')))
    assert body['result_status'] == 'success' and body['trades']
    assert calls[0]['end_at'] < T0 + 7 * DAY
    assert body['assumptions']['exchange_netflow']['ignored_rows_count'] == 2
    body, _ = run(monkeypatch, tmp_path, {2: -1}, dict(outflow_days=1, inflow_exit_days=1), missing=(7,))
    trade, = body['trades']
    assert trade['closed_at'] == T0 + 7 * DAY
    assert body['raw_report']['exchange_netflow']['executions'][0]['exit_reason'] == 'final_close'
