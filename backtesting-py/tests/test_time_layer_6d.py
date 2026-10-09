"""6d: independent UTC boundaries, scalar prefixes and strict fetch contract."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import pandas as pd
import pytest
from test_risk_layer import p
from strategy_time_layer import TimeConfig, TimeContext
from strategy_time_series import (PeriodDefinition, PeriodBounds, SeriesBar, TimeHistoryError,
    period_bounds, required_history_start, validate_time_history, range_window, freeze_range, prefix_vwap)


def dt(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def context(timeframe='1h'):
    return TimeContext.build(TimeConfig(enabled=True), timeframe, [dt('2026-01-01T00:00')])


def bars(start='2026-01-01T00:00', count=4):
    return [SeriesBar(dt(start)+timedelta(hours=i), 12+i, 6+i, 9+i, i+1) for i in range(count)]


@pytest.mark.parametrize('zone,opened,reset,start,end', [
    ('UTC', '2026-01-01T23:00', '00:00', '2026-01-01T00:00', '2026-01-02T00:00'),
    ('UTC', '2026-01-02T05:00', '06:00', '2026-01-01T06:00', '2026-01-02T06:00'),
    ('UTC', '2026-01-02T06:00', '06:00', '2026-01-02T06:00', '2026-01-03T06:00'),
    ('America/New_York', '2026-01-02T02:00', '00:00', '2026-01-01T05:00', '2026-01-02T05:00'),
    ('America/New_York', '2026-07-02T10:00', '06:00', '2026-07-02T10:00', '2026-07-03T10:00'),
])
def test_day_anchors(zone, opened, reset, start, end):
    assert period_bounds(dt(opened), PeriodDefinition(zone, reset_at=reset)) == PeriodBounds(dt(start), dt(end))


@pytest.mark.parametrize('opened,start,end,hours', [
    ('2026-03-08T12:00', '2026-03-08T05:00', '2026-03-09T04:00', 23),
    ('2026-11-01T12:00', '2026-11-01T04:00', '2026-11-02T05:00', 25),
])
def test_dst_day_length(opened, start, end, hours):
    cycle = period_bounds(dt(opened), PeriodDefinition('America/New_York'))
    assert cycle == PeriodBounds(dt(start), dt(end))
    assert cycle.end_utc-cycle.start_utc == timedelta(hours=hours)


@pytest.mark.parametrize('zone,opened,reset,start,end', [
    ('UTC', '2027-01-03T23:45', '00:00', '2026-12-28T00:00', '2027-01-04T00:00'),
    ('America/New_York', '2027-01-04T04:45', '00:00', '2026-12-28T05:00', '2027-01-04T05:00'),
    ('UTC', '2026-12-28T05:45', '06:00', '2026-12-21T06:00', '2026-12-28T06:00'),
    ('America/New_York', '2026-03-08T12:00', '00:00', '2026-03-02T05:00', '2026-03-09T04:00'),
    ('America/New_York', '2026-11-01T12:00', '00:00', '2026-10-26T04:00', '2026-11-02T05:00'),
])
def test_week_anchors(zone, opened, reset, start, end):
    assert period_bounds(dt(opened), PeriodDefinition(zone, 'week', reset)) == PeriodBounds(dt(start), dt(end))


def test_nonexistent_reset_skips_boundary():
    cycle = period_bounds(dt('2026-03-08T08:00'), PeriodDefinition('America/New_York', reset_at='02:30'))
    assert cycle == PeriodBounds(dt('2026-03-07T07:30'), dt('2026-03-09T06:30'))
    assert cycle.end_utc-cycle.start_utc == timedelta(hours=47)


def test_repeated_reset_first_only():
    spec = PeriodDefinition('America/New_York', reset_at='01:30')
    first, repeated = dt('2026-11-01T05:30'), dt('2026-11-01T06:30')
    assert period_bounds(first, spec) == period_bounds(repeated, spec) == PeriodBounds(first, dt('2026-11-02T06:30'))
    assert period_bounds(first-timedelta(minutes=15), spec).end_utc == first


@pytest.mark.parametrize('start,reset', [('2026-03-07T00:00', '00:00'), ('2026-11-01T00:00', '00:00'),
                                       ('2026-03-07T00:00', '02:30'), ('2026-11-01T00:00', '01:30')])
def test_continuous_quarters_partition_real_utc(start, reset):
    spec = PeriodDefinition('America/New_York', reset_at=reset)
    opens = [dt(start)+timedelta(minutes=15*i) for i in range(3*24*4)]
    cycles = set(period_bounds(value, spec) for value in opens)
    ordered = sorted(cycles, key=lambda item: item.start_utc)
    assert all(a.end_utc == b.start_utc for a, b in zip(ordered, ordered[1:]))
    assert all(sum(c.start_utc <= value < c.end_utc for c in cycles) == 1 for value in opens)


@pytest.mark.parametrize('kwargs', [{'timezone_name': 'Mars/Olympus'}, {'timezone_name': None},
    {'reset_period': 'month'}, {'reset_at': '2:30'}, {'reset_at': '24:00'}, {'reset_at': True}, {'reset_at': '01:60'}])
def test_invalid_period_definition(kwargs):
    with pytest.raises(ValueError, match='INVALID_PARAMS:'): PeriodDefinition(**kwargs)


@pytest.mark.parametrize('spec,start,expected', [
    (PeriodDefinition('America/New_York'), '2026-03-08T14:00', '2026-03-08T05:00'),
    (PeriodDefinition('America/New_York', 'week'), '2027-01-03T15:00', '2026-12-28T05:00'),
    (PeriodDefinition(reset_at='06:00'), '2026-01-02T04:00', '2026-01-01T06:00')])
def test_required_history_start(spec, start, expected):
    assert required_history_start(dt(start), spec) == dt(expected)


def test_earlier_observation_history():
    assert required_history_start(dt('2026-01-02T03:00'), PeriodDefinition(), dt('2026-01-01T22:00')) == dt('2026-01-01T22:00')


def frame(start='2026-01-01T00:00', count=8):
    return pd.DataFrame({'Open': 10., 'High': 12., 'Low': 6., 'Close': 9., 'Volume': 2.},
                        index=pd.date_range(dt(start).replace(tzinfo=None), periods=count, freq='h'))


def strict_fetch(monkeypatch, source, *, spec=PeriodDefinition(), start='2026-01-01T03:00', end='2026-01-01T06:00', **kwargs):
    calls = []
    def fetch(*args):
        calls.append(args)
        return source
    monkeypatch.setattr(p, '_fetch_ohlcv', fetch)
    result = p._fetch_strict_time_history('binance', 'spot', 'BTC/USDT', '1h', int(dt(start).timestamp()),
                                          int(dt(end).timestamp()), spec, **kwargs)
    return result, calls


def test_strict_fetch_since_crop_and_warmup(monkeypatch):
    result, calls = strict_fetch(monkeypatch, frame('2025-12-31T23:00', 10))
    assert calls == [('binance', 'spot', 'BTC/USDT', '1h', int(dt('2026-01-01T00:00').timestamp()), int(dt('2026-01-01T06:00').timestamp()))]
    assert list(result.index) == list(frame(count=6).index)
    assert result.attrs['time_warmup_only'] == [True]*3+[False]*3


def test_strict_week_since(monkeypatch):
    result, calls = strict_fetch(monkeypatch, frame('2026-12-28T05:00', 31), spec=PeriodDefinition('America/New_York', 'week'),
                                start='2026-12-29T06:00', end='2026-12-29T12:00')
    assert calls[0][-2] == int(dt('2026-12-28T05:00').timestamp())
    assert len(result) == 31


@pytest.mark.parametrize('missing', [0, 2, 5])
def test_strict_fetch_missing_fails(monkeypatch, missing):
    with pytest.raises(TimeHistoryError, match='INSUFFICIENT_DATA:time_history_incomplete:'):
        strict_fetch(monkeypatch, frame(count=6).drop(frame(count=6).index[missing]))


@pytest.mark.parametrize('fault', ['empty', 'nan', 'negative', 'duplicate', 'disorder'])
def test_strict_fetch_invalid_history(monkeypatch, fault):
    source = frame(count=6)
    if fault == 'empty': source = source.iloc[:0]
    if fault == 'nan': source.iloc[2, 4] = float('nan')
    if fault == 'negative': source.iloc[2, 4] = -1
    if fault == 'duplicate': source = pd.concat([source, source.iloc[:1]])
    if fault == 'disorder': source = source.iloc[::-1]
    with pytest.raises(TimeHistoryError): strict_fetch(monkeypatch, source)


def test_strict_source_failure(monkeypatch):
    def fail(*args): raise RuntimeError('offline')
    monkeypatch.setattr(p, '_fetch_ohlcv', fail)
    with pytest.raises(TimeHistoryError) as caught:
        p._fetch_strict_time_history('binance', 'spot', 'BTC/USDT', '1h', int(dt('2026-01-01T03:00').timestamp()),
                                      int(dt('2026-01-01T06:00').timestamp()), PeriodDefinition())
    assert (caught.value.error_type, caught.value.reason) == ('INSUFFICIENT_DATA', 'time_history_incomplete')


def test_strict_bounds_rejected_before_fetch(monkeypatch):
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a: pytest.fail('invalid bounds fetched'))
    with pytest.raises(ValueError, match='INVALID_PARAMS:'):
        p._fetch_strict_time_history('binance', 'spot', 'BTC/USDT', '1h', int(dt('2026-01-01T03:00').timestamp()),
                                      int(dt('2026-01-01T06:00').timestamp()), PeriodDefinition(reset_at='00:30'))


def frozen(data, start='00:00', end='03:00', as_of='2026-01-01T03:00', spec=PeriodDefinition(), cycle=None):
    return freeze_range(data, context(), cycle or period_bounds(data[0].open_utc, spec), spec, start, end,
                        as_of=dt(as_of), backtest_start=dt('2026-01-01T01:00'))


def test_range_freeze_excludes_breakout():
    data = bars()
    data[3] = replace(data[3], high=10000, low=-10000)
    result = frozen(data, as_of='2026-01-01T04:00')
    assert (result.high, result.low, result.available) == (14, 6, True)
    assert result.freeze_utc == dt('2026-01-01T03:00')
    assert not result.warmup_only


def test_range_before_end_unavailable():
    result = frozen(bars(), as_of='2026-01-01T02:59')
    assert not result.available and result.high is result.low is None


def test_range_end_uses_close_clock():
    result = frozen(bars(count=3))
    assert (result.high, result.low, result.available) == (14, 6, True)


def test_range_midnight_window_start_day():
    cycle = period_bounds(dt('2026-01-01T22:00'), PeriodDefinition())
    result = frozen(bars('2026-01-01T22:00', 5), '22:00', '02:00', '2026-01-02T03:00', cycle=cycle)
    assert result.cycle == cycle and result.freeze_utc == dt('2026-01-02T02:00')
    assert (result.high, result.low) == (15, 6)


@pytest.mark.parametrize('start,end', [('00:30', '03:00'), ('00:00', '03:30')])
def test_range_cut_candle_rejected(start, end):
    with pytest.raises(ValueError, match='INVALID_PARAMS:'): frozen(bars(), start, end)


def test_range_missing_candle_fails():
    data = bars()
    with pytest.raises(TimeHistoryError): frozen([data[0], data[2], data[3]])


def test_range_only_reads_closed_window_prices():
    data = bars()
    data[3] = replace(data[3], high=float('nan'))
    assert frozen(data).high == 14
    data[2] = replace(data[2], high=float('nan'))
    assert not frozen(data, as_of='2026-01-01T02:00').available
    with pytest.raises(TimeHistoryError): frozen(data)


def test_range_24_wall_hours_and_reset_offset():
    spec = PeriodDefinition(reset_at='06:00')
    cycle = period_bounds(dt('2026-01-01T12:00'), spec)
    assert range_window(cycle, spec, '02:00', '02:00') == PeriodBounds(dt('2026-01-02T02:00'), dt('2026-01-03T02:00'))


@pytest.mark.parametrize('start,end', [('02:30', '04:00'), ('01:00', '02:30')])
def test_range_nonexistent_endpoint_rejected(start, end):
    spec = PeriodDefinition('America/New_York')
    with pytest.raises(ValueError, match='INVALID_PARAMS:'):
        range_window(period_bounds(dt('2026-03-08T12:00'), spec), spec, start, end)


def test_range_repeated_endpoint_first_fold():
    spec = PeriodDefinition('America/New_York')
    assert range_window(period_bounds(dt('2026-11-01T12:00'), spec), spec, '01:30', '02:30') == PeriodBounds(dt('2026-11-01T05:30'), dt('2026-11-01T07:30'))


@pytest.mark.parametrize('source,expected', [('hlc3', [9, (9+20)/3, (9+20+33)/6, (9+20+33+48)/10]),
                                          ('close', [8, (8+18)/3, (8+18+30)/6, (8+18+30+44)/10])])
def test_vwap_scalar_prefixes(source, expected):
    data = [SeriesBar(dt('2026-01-01T00:00')+timedelta(hours=i), 13+i, 6+i, 8+i, i+1) for i in range(4)]
    # Independent HLC3 prices 9,10,11,12; closes 8,9,10,11; volumes 1,2,3,4.
    output = prefix_vwap(data, context(), PeriodDefinition(), backtest_start=dt('2026-01-01T02:00'), price_source=source)
    assert [v.value for v in output] == pytest.approx(expected)
    assert [v.warmup_only for v in output] == [True, True, False, False]
    assert output[0].decision_utc == dt('2026-01-01T01:00')


def test_vwap_zero_volume_is_absent():
    data = [replace(value, volume=0) for value in bars(count=3)]
    data[2] = replace(data[2], volume=2)
    output = prefix_vwap(data, context(), PeriodDefinition(), backtest_start=data[0].open_utc, price_source='close')
    assert [v.value for v in output] == [None, None, 11]


def test_vwap_reset_and_midnight_close_belongs_to_open_day():
    data = bars(count=26)
    output = prefix_vwap(data, context(), PeriodDefinition(), backtest_start=data[0].open_utc, price_source='close')
    assert output[23].cycle.start_utc == dt('2026-01-01T00:00')
    assert output[23].decision_utc == dt('2026-01-02T00:00')
    assert output[24].value == data[24].close
    assert output[25].value == pytest.approx((33*25+34*26)/51)


def test_vwap_future_price_does_not_change_prefix():
    data = bars()
    original = prefix_vwap(data, context(), PeriodDefinition(), backtest_start=data[0].open_utc)
    changed = [*data[:2], replace(data[2], high=9000, low=3000, close=6000, volume=999), data[3]]
    output = prefix_vwap(changed, context(), PeriodDefinition(), backtest_start=data[0].open_utc)
    assert output[:2] == original[:2] and output[2].value != original[2].value


def test_vwap_visibility_uses_close_not_open():
    data = bars()
    output = prefix_vwap(data, context(), PeriodDefinition(), backtest_start=data[0].open_utc, as_of=dt('2026-01-01T02:00'))
    assert len(output) == 2 and output[-1].open_utc == dt('2026-01-01T01:00')


@pytest.mark.parametrize('source', ['ohlc4', None])
def test_vwap_invalid_price_source(source):
    with pytest.raises(ValueError, match='INVALID_PARAMS:'):
        prefix_vwap(bars(), context(), PeriodDefinition(), backtest_start=dt('2026-01-01T00:00'), price_source=source)


@pytest.mark.parametrize('fault', ['prefix', 'gap', 'order', 'duplicate', 'negative', 'nonfinite', 'overflow'])
def test_vwap_bad_history_fails(fault):
    data = bars()
    if fault == 'prefix': data = data[1:]
    if fault == 'gap': data.pop(1)
    if fault == 'order': data = data[::-1]
    if fault == 'duplicate': data.insert(1, data[0])
    if fault == 'negative': data[1] = replace(data[1], volume=-1)
    if fault == 'nonfinite': data[1] = replace(data[1], close=float('inf'))
    if fault == 'overflow': data[1] = replace(data[1], close=1e308, volume=1e308)
    with pytest.raises(TimeHistoryError):
        prefix_vwap(data, context(), PeriodDefinition(), backtest_start=dt('2026-01-01T00:00'))


def test_strict_observation_since(monkeypatch):
    result, calls = strict_fetch(monkeypatch, frame('2026-01-01T22:00', 10), start='2026-01-02T03:00',
        end='2026-01-02T06:00', observation_start=dt('2026-01-01T22:00'))
    assert calls[0][-2] == int(dt('2026-01-01T22:00').timestamp())
    assert result.attrs['time_warmup_only'] == [True]*5+[False]*3


@pytest.mark.parametrize('timeframe,start,end', [('1h', '2026-01-01T00:00', '2026-01-01T03:30'),
                                               ('1h', '2026-01-01T00:00', '2026-01-01T00:00')])
def test_history_invalid_endpoints(timeframe, start, end):
    with pytest.raises(ValueError, match='INVALID_PARAMS:'):
        validate_time_history([], dt(start), dt(end), context(timeframe))


def test_naive_opens_are_utc():
    assert period_bounds(datetime(2026, 1, 1, 23), PeriodDefinition()) == PeriodBounds(dt('2026-01-01T00:00'), dt('2026-01-02T00:00'))


def test_range_warmup_snapshot():
    data = bars()
    snapshot = freeze_range(data, context(), period_bounds(data[0].open_utc, PeriodDefinition()), PeriodDefinition(),
        '00:00', '03:00', as_of=dt('2026-01-01T04:00'), backtest_start=dt('2026-01-01T03:00'))
    assert snapshot.available and snapshot.warmup_only


def test_vwap_week_reset_across_year():
    data = bars('2026-12-28T00:00', 169)
    output = prefix_vwap(data, context(), PeriodDefinition(reset_period='week'), backtest_start=data[0].open_utc, price_source='close')
    assert output[167].cycle.start_utc == dt('2026-12-28T00:00')
    assert output[168].cycle.start_utc == dt('2027-01-04T00:00')
    assert output[168].value == data[168].close


@pytest.mark.parametrize('start,length', [('2026-03-08T05:00', 23), ('2026-11-01T04:00', 25)])
def test_vwap_dst_resets_on_local_midnight(start, length):
    data = bars(start, length+1)
    spec = PeriodDefinition('America/New_York')
    output = prefix_vwap(data, context(), spec, backtest_start=data[0].open_utc, price_source='close')
    assert output[length-1].cycle.start_utc == dt(start)
    assert output[length].value == data[length].close
    assert output[length].cycle.start_utc == dt(start)+timedelta(hours=length)


def test_vwap_as_of_does_not_read_unclosed_prices():
    data = bars()
    data[2] = replace(data[2], close=float('nan'))
    output = prefix_vwap(data, context(), PeriodDefinition(), backtest_start=data[0].open_utc, as_of=dt('2026-01-01T02:00'))
    assert len(output) == 2


@pytest.mark.parametrize('start,hours', [('2026-03-08T05:00', 23), ('2026-11-01T04:00', 25)])
def test_range_full_local_day_dst(start, hours):
    data = bars(start, hours+1)
    spec = PeriodDefinition('America/New_York')
    snapshot = freeze_range(data, context(), period_bounds(data[0].open_utc, spec), spec, '00:00', '00:00',
        as_of=data[hours].open_utc, backtest_start=data[0].open_utc)
    assert snapshot.freeze_utc == dt(start)+timedelta(hours=hours)
    assert snapshot.high == 12+hours-1
