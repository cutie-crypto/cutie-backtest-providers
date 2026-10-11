"""Calendar wiring: independently specified UTC fills, HTTP contract, frozen bytes."""
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path
import sys

import pandas as pd
import pytest
from backtesting import Backtest, Strategy
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p
import test_time_layer_compatibility as compat
from canonical_json import canonical_json
from scale_in_out_ledger import LedgerBar, run_scale_in_out
from strategy_time_layer import TimeConfig, TimeContext
from strategy_time_calendar import REGULAR_CALENDAR_ASSUMPTION

CALENDARS = [('us_equity_regular', 'America/New_York'), ('cme_btc_regular', 'America/Chicago')]
LEDGERS = compat.capture.LEDGER_PARAMS
# Each listed signal is a closed 15m candle; fill is exactly 15m later.
# UTC schedules are hand derived from EST/CST before DST and EDT/CDT after it.
SEQUENCES = [
    ('us_weekend', 'us_equity_regular', 'America/New_York', '2026-01-02T20:00', 300,
     [('2026-01-02T20:15', 'buy'), ('2026-01-02T20:30', 'sell_all'),
      ('2026-01-02T20:45', 'buy'), ('2026-01-03T14:15', 'buy'),
      ('2026-01-05T14:15', 'buy'), ('2026-01-05T14:30', 'sell_all')],
     ['2026-01-02T20:30', '2026-01-05T14:30'], ['2026-01-02T20:45', '2026-01-05T14:45']),
    ('us_dst', 'us_equity_regular', 'America/New_York', '2026-03-08T06:00', 150,
     [('2026-03-08T06:45', 'buy'), ('2026-03-08T07:00', 'buy'),
      ('2026-03-09T13:15', 'buy'), ('2026-03-09T13:30', 'sell_all')],
     ['2026-03-09T13:30'], ['2026-03-09T13:45']),
    ('cme_pause', 'cme_btc_regular', 'America/Chicago', '2026-01-08T21:00', 40,
     [('2026-01-08T21:15', 'buy'), ('2026-01-08T21:30', 'sell_all'),
      ('2026-01-08T21:45', 'buy'), ('2026-01-08T22:00', 'buy'),
      ('2026-01-08T22:45', 'buy'), ('2026-01-08T23:00', 'sell_all')],
     ['2026-01-08T21:30', '2026-01-08T23:00'], ['2026-01-08T21:45', '2026-01-08T23:15']),
    ('cme_dst_weekend', 'cme_btc_regular', 'America/Chicago', '2026-03-06T21:00', 230,
     [('2026-03-06T21:15', 'buy'), ('2026-03-06T21:30', 'sell_all'),
      ('2026-03-06T21:45', 'buy'), ('2026-03-06T22:45', 'buy'),
      ('2026-03-07T22:45', 'buy'), ('2026-03-08T07:45', 'buy'),
      ('2026-03-08T21:45', 'buy'), ('2026-03-08T22:00', 'sell_all')],
     ['2026-03-06T21:30', '2026-03-08T22:00'], ['2026-03-06T21:45', '2026-03-08T22:15']),
]


def frame(start, count, freq='15min'):
    return pd.DataFrame(dict(Open=[100.]*count, High=[101.]*count, Low=[99.]*count,
        Close=[100.]*count, Volume=[1.]*count), index=pd.date_range(start, periods=count, freq=freq))


def clock(data, calendar, zone, timeframe='15m', **extra):
    return TimeContext.build(TimeConfig.parse(dict(time_layer_enabled=True,
        time_calendar=calendar, time_timezone=zone, **extra)), timeframe, data.index)


def manual_class(context, actions):
    class Manual(p._FixedRiskMixin, Strategy):
        _time_config = context.config
        _time_context = context
        _risk = {}
        def init(self):
            self._risk_init()
        def next(self):
            action = actions.get(len(self.data)-1, 'hold')
            if action == 'buy':
                self._risk_buy()
            elif action == 'sell_all' and self.position:
                self.position.close()
    return Manual


def ledger_rows(data, step=900):
    return [LedgerBar(int(t.timestamp()), int(t.timestamp())+step, D(100), D(100)) for t in data.index]


@pytest.mark.parametrize('case', SEQUENCES, ids=[s[0] for s in SEQUENCES])
@pytest.mark.parametrize('runner', ['single', 'ledger'])
def test_hand_calculated_fills(case, runner):
    _, calendar, zone, start, count, signals, opens, closes = case
    data = frame(start, count)
    actions = {data.index.get_loc(pd.Timestamp(t)): action for t, action in signals}
    ctx = clock(data, calendar, zone)
    if runner == 'single':
        result = Backtest(data, manual_class(ctx, actions), cash=10000,
            exclusive_orders=True, finalize_trades=True).run()['_trades']
        actual_open, actual_close = list(result.EntryTime), list(result.ExitTime)
    else:
        rows = ledger_rows(data)
        result = run_scale_in_out(rows, lambda i: actions.get(i, 'hold'),
            initial_capital=D(10000), buy_notional=D(100), sell_notional=D(100),
            fee_bps=D(0), slippage_bps=D(0), start_at=rows[0].open_time,
            end_at=rows[-1].close_time, time_context=ctx)
        actual_open = [pd.Timestamp(t['opened_at'], unit='s') for t in result.trades]
        actual_close = [pd.Timestamp(t['closed_at'], unit='s') for t in result.trades]
    assert actual_open == list(map(pd.Timestamp, opens))
    assert actual_close == list(map(pd.Timestamp, closes))
    assert ctx.assumptions()['time_layer']['calendar'] == dict(name=calendar,
        gate='entire_next_fill_bar', note=REGULAR_CALENDAR_ASSUMPTION)


@pytest.mark.parametrize('calendar,zone', CALENDARS)
@pytest.mark.parametrize('runner', ['single', 'ledger'])
def test_whole_fill_bar_and_unrestricted_expiry(calendar, zone, runner):
    # 15:00 local 1h fill is inside; next 16:00 open is closed, but exit must fill.
    start = '2026-01-08T18:00' if calendar == 'us_equity_regular' else '2026-01-08T19:00'
    data = frame(start, 7, 'h')
    ctx = clock(data, calendar, zone, '1h', time_max_holding_minutes=60)
    expected_open = '2026-01-08T20:00' if calendar == 'us_equity_regular' else '2026-01-08T21:00'
    expected_close = '2026-01-08T21:00' if calendar == 'us_equity_regular' else '2026-01-08T22:00'
    if runner == 'single':
        base = manual_class(ctx, {1:'buy'})
        class Expiry(base):
            def next(self):
                if self.position:
                    self._risk_check_exit()
                else:
                    super().next()
        trades = Backtest(data, Expiry, cash=10000, finalize_trades=True).run()['_trades']
        assert list(trades.EntryTime) == [pd.Timestamp(expected_open)]
        assert list(trades.ExitTime) == [pd.Timestamp(expected_close)]
    else:
        rows = ledger_rows(data, 3600)
        result = run_scale_in_out(rows, lambda i: 'buy' if i == 1 else 'hold',
            initial_capital=D(10000), buy_notional=D(100), sell_notional=D(100),
            fee_bps=D(0), slippage_bps=D(0), start_at=rows[0].open_time,
            end_at=rows[-1].close_time, time_context=ctx)
        assert [pd.Timestamp(t['opened_at'], unit='s') for t in result.trades] == [pd.Timestamp(expected_open)]
        assert [pd.Timestamp(t['closed_at'], unit='s') for t in result.trades] == [pd.Timestamp(expected_close)]
        assert result.time_expiry_fills == 1
    # These opens are before close, but their full 1h candle crosses it.
    assert ctx.allow_entry(pd.Timestamp(expected_open)+pd.Timedelta(minutes=30)) is False


def test_ledger_adds_blocked_in_cme_pause():
    data = frame('2026-01-08T21:00', 14)
    ctx = clock(data, 'cme_btc_regular', 'America/Chicago')
    rows = ledger_rows(data)
    result = run_scale_in_out(rows, lambda i: {1:'buy', 3:'buy', 4:'buy', 7:'buy', 8:'sell_all'}.get(i, 'hold'),
        initial_capital=D(10000), buy_notional=D(100), sell_notional=D(100),
        fee_bps=D(0), slippage_bps=D(0), start_at=rows[0].open_time,
        end_at=rows[-1].close_time, time_context=ctx)
    assert result.buy_fills == 2
    assert [t['opened_at'] for t in result.trades] == [1767907800, 1767913200]
    assert [t['closed_at'] for t in result.trades] == [1767914100, 1767914100]


@pytest.mark.parametrize('calendar,zone', CALENDARS)
def test_calendar_intersects_session_and_weekdays(calendar, zone):
    data = frame('2026-01-08T00:00', 8)
    ctx = clock(data, calendar, zone, time_session_start='10:00', time_session_end='11:00', time_weekdays=8)
    prefix = '2026-01-08T'  # Thursday, bit3
    hour = 15 if calendar == 'us_equity_regular' else 16
    assert ctx.allow_entry(pd.Timestamp(prefix+f'{hour}:00')) is True
    assert ctx.allow_entry(pd.Timestamp(prefix+f'{hour-1}:45')) is False
    assert ctx.allow_entry(pd.Timestamp('2026-01-09T'+f'{hour}:00')) is False


@pytest.mark.parametrize('name', ['ema_cross', *LEDGERS])
@pytest.mark.parametrize('calendar,zone', CALENDARS)
@pytest.mark.parametrize('bad', ['timezone', 'disabled', 'daily'])
def test_invalid_calendar_before_fetch(monkeypatch, name, calendar, zone, bad):
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a: pytest.fail('invalid calendar fetched'))
    values = dict(LEDGERS.get(name, {}), time_calendar=calendar, time_timezone=zone, time_layer_enabled=True)
    if bad == 'timezone':
        values['time_timezone'] = 'UTC'
    elif bad == 'disabled':
        values['time_layer_enabled'] = False
    req = dict(provider_tool_id='local.backtesting_py.'+name, provider_params=values,
        symbol='BTCUSDT', market='spot', timeframe='1d' if bad == 'daily' else '15m',
        start_at=1767870000, end_at=1767913200)
    body = TestClient(p.app).post('/cutie/backtest', json={'backtest': req}).json()
    assert body['error_type'] == 'INVALID_PARAMS', body


@pytest.mark.parametrize('value', ['unknown', '', True, None, 0])
def test_calendar_enum_and_type(value):
    with pytest.raises(ValueError, match='INVALID_PARAMS:'):
        TimeConfig.parse(dict(time_layer_enabled=True, time_calendar=value))


@pytest.mark.parametrize('case', SEQUENCES, ids=[s[0] for s in SEQUENCES])
@pytest.mark.parametrize('name', ['ema_cross', *LEDGERS])
def test_registered_runner_consumes_calendar(monkeypatch, tmp_path, name, case):
    _, calendar, zone, start, count, _, _, _ = case
    data = frame(start, count)
    # Constant prices + oversold RSI produces repeated buys in RSI ledger;
    # oscillating closes exercise the registered single and grid builders.
    data['Close'] = [100. if i % 8 < 4 else 90. for i in range(count)]
    data['Open'] = data['Close']
    data['High'] = data['Close']+1
    data['Low'] = data['Close']-1
    values = dict(LEDGERS.get(name, {}), time_layer_enabled=True, time_calendar=calendar, time_timezone=zone)
    if name == 'ema_cross':
        values.update(ema_fast=2, ema_slow=3)
    if name == 'dca':
        values.update(dip_pct=1, max_dip_adds=10, profit_target_pct=1)
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', Path(tmp_path))
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a: data.copy())
    # Supply a genuine contiguous indicator prefix; calendar never trims OHLCV.
    warm = frame(str(data.index[0]-pd.Timedelta(hours=12)), 48)
    monkeypatch.setattr(p, '_fetch_template_warmup', lambda *a: warm.copy())
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **k: None)
    req = dict(run_id='calendar_6e2', provider_tool_id='local.backtesting_py.'+name, provider_params=values,
        symbol='BTCUSDT', market='spot', timeframe='15m', start_at=int(data.index[0].timestamp()),
        end_at=int(data.index[-1].timestamp())+900, initial_capital='10000', fee_bps='0', slippage_bps='0')
    body = TestClient(p.app).post('/cutie/backtest', json={'backtest': req}).json()
    assert body['result_status'] == 'success', body
    if name == 'dca' and calendar == 'us_equity_regular':
        # DCA daily signals at local midnight; the intersection has no open.
        assert body['trades'] == []
    else:
        assert body['trades'], (name, case[0])
    # Independent fixed UTC sessions, without calling calendar methods.
    sessions = {
        'us_weekend': [('2026-01-02T14:30', '2026-01-02T21:00'), ('2026-01-05T14:30', '2026-01-05T21:00')],
        'us_dst': [('2026-03-09T13:30', '2026-03-09T20:00')],
        'cme_pause': [('2026-01-07T23:00', '2026-01-08T22:00'), ('2026-01-08T23:00', '2026-01-09T22:00')],
        'cme_dst_weekend': [('2026-03-05T23:00', '2026-03-06T22:00'), ('2026-03-08T22:00', '2026-03-09T21:00')],
    }[case[0]]
    bounds = [(int(pd.Timestamp(a).timestamp()), int(pd.Timestamp(b).timestamp())) for a,b in sessions]
    assert all(any(a <= t['opened_at'] and t['opened_at']+900 <= b for a,b in bounds) for t in body['trades'])
    assert body['assumptions']['time_layer']['calendar']['note'] == REGULAR_CALENDAR_ASSUMPTION
    assert set(body['metrics']) == {'total_return', 'max_drawdown', 'trade_count'}
    assert set(body['data_manifest']) == {'source','symbol','market','timeframe','start_at','end_at','kline_count','checksum_algo','checksum'}
    assert body['data_manifest']['kline_count'] == count


@pytest.mark.parametrize('name', ['ema_cross', *LEDGERS])
def test_none_byte_unchanged(name):
    values = dict(LEDGERS.get(name, {}))
    absent = compat.capture.response(name, values)
    explicit = compat.capture.response(name, dict(values, time_calendar='none'))
    keys = compat.capture.V2_KEYS
    assert canonical_json({k:absent[k] for k in keys}) == canonical_json({k:explicit[k] for k in keys})
    for key in ('assumptions', 'raw_report'):
        assert compat.capture.digest(absent[key]) == compat.capture.digest(explicit[key])
    assert 'time_layer' not in explicit['assumptions']


@pytest.mark.parametrize('tool,spec', list(p.TOOL_SPECS.items()), ids=list(p.TOOL_SPECS))
def test_calendar_schema_matches_consumers(tool, spec):
    schema = spec['param_schema_properties']
    # TURTLE-TIME: 海龟 runner 已接时间层（含 time_calendar），不再排除。
    excluded = (spec.get('runner') in ('kernel_v3', p.ROTATION_RUNNER)
                or tool.removeprefix('local.backtesting_py.') in p.MACRO_SCHEMAS
                or tool in (p.FEAR_GREED_TOOL_ID, p.FUNDING_REVERSAL_TOOL_ID, p.TOP_LSR_TOOL_ID, p.LIQUIDATION_REVERSAL_TOOL_ID))  # P1/P2/S3/S4：不发布时间层
    assert ('time_calendar' in schema) is not excluded
    if not excluded:
        assert schema['time_calendar'] == {'type':'string', 'default':'none',
            'enum':['none','us_equity_regular','cme_btc_regular']}
