"""Q18 hand-computed 5-minute candles: inline macro events, no prefix/network."""
from pathlib import Path
import sys

import pandas as pd
import pytest
from backtesting import Backtest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p
from strategy_macro_events import KINDS

H1, H2, H3 = KINDS
T0 = 1767225600
STEP = 300


def frame(n=96):
    return pd.DataFrame(dict(Open=[100.] * n, High=[101.] * n, Low=[99.] * n,
                             Close=[100.] * n, Volume=[1.] * n),
                        index=pd.date_range('2026-01-01', periods=n, freq='5min'))


def event(bar=12, **extra):
    return dict(ts_utc=pd.Timestamp(T0 + bar * STEP, unit='s', tz='UTC').isoformat(), label=f'E{bar}', **extra)


def params(kind, **extra):
    base = dict(events=[event(**(dict(expected=2.9, actual=2.8) if kind == H2 else {}))])
    base.update(dict(pre_window_minutes=30, max_hold_minutes=15) if kind == H1 else
                dict(hold_minutes=15) if kind == H2 else
                dict(lookback_hours=1, hold_minutes=15, entry_delay_minutes=10))
    return {**base, **extra}


def data_for(kind, side='long'):
    data = frame()
    if kind == H1:
        data.iloc[12] = [100, 103, 99, 102, 1]
        data.iloc[13:] = [103, 104, 102, 103, 1]
        if side == 'short':
            data[['Open', 'High', 'Low', 'Close']] = pd.DataFrame(
                dict(Open=200-data.Open, High=200-data.Low, Low=200-data.High, Close=200-data.Close))
    elif kind == H3:
        data.iloc[11] = [100, 106, 99, 105, 1] if side == 'short' else [100, 101, 94, 95, 1]
    return data


def post(monkeypatch, tmp_path, kind, values=None, data=None, market='futures', invalid=False):
    data = data_for(kind) if data is None else data
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', tmp_path)
    calls = []
    def fetch(*a, **k):
        calls.append(a)
        assert not invalid, 'invalid parameters must reject before fetch'
        return data.copy()
    def no_warmup(*a, **k):
        pytest.fail('macro templates must fetch zero warmup')
    monkeypatch.setattr(p, '_fetch_ohlcv', fetch)
    monkeypatch.setattr(p, '_fetch_template_warmup', no_warmup)
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **k: None)
    request = dict(run_id='q18', provider_tool_id=f'local.backtesting_py.{kind}',
                   provider_params=values if values is not None else params(kind), symbol='BTCUSDT', market=market,
                   timeframe='5m', start_at=T0, end_at=int(data.index[-1].timestamp())+STEP, initial_capital='10000',
                   fee_bps='0', slippage_bps='0')
    response = TestClient(p.app).post('/cutie/backtest', json={'backtest': request})
    body = response.json()
    if not invalid:
        assert body['result_status'] == 'success', body
        assert len(calls) == 1
        assert body['assumptions'][kind]['warmup_bars'] == 0
        assert body['assumptions'][kind]['data_source'] == 'inline_params_only'
        assert all('status' in r for r in body['raw_report'][kind]['events'])
    return body


def report(body, kind):
    return body['raw_report'][kind]


def legs(body):
    return [((t['opened_at']-T0)//STEP, t['entry_price'], (t['closed_at']-T0)//STEP,
             t['exit_price'], t['side']) for t in body['trades']]


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('side', ['long', 'short'])
def test_long_short_exact_fills_and_zero_warmup(monkeypatch, tmp_path, kind, side):
    values = params(kind)
    if kind == H2:
        values['events'] = [event(expected=2.9, actual=2.8 if side == 'long' else 3.0)]
    body = post(monkeypatch, tmp_path, kind, values, data_for(kind, side))
    entry = 13 if kind == H1 else 12 if kind == H2 else 14
    price = ('103' if side == 'long' else '97') if kind == H1 else '100'
    assert legs(body) == [(entry, price, entry+3, price, side)]
    record = report(body, kind)['events'][0]
    assert record['status'] == 'entered'
    assert record['exit_reason'] == 'time_expiry'
    assert record['entry_price'] == body['trades'][0]['entry_price']
    assert record['exit_price'] == body['trades'][0]['exit_price']


@pytest.mark.parametrize('kind', KINDS)
def test_multiple_events_and_overlap_skip(monkeypatch, tmp_path, kind):
    data = data_for(kind)
    items = [event(i, **(dict(expected=2.9, actual=2.8) if kind == H2 else {})) for i in (12, 15, 30)]
    if kind == H1:
        data.iloc[30] = [103, 106, 102, 105, 1]
        data.iloc[31:] = [106, 107, 105, 106, 1]
    if kind == H3:
        data.iloc[29] = [100, 101, 94, 95, 1]
    body = post(monkeypatch, tmp_path, kind, params(kind, events=items[::-1]), data)
    evidence = report(body, kind)
    assert [r['status'] for r in evidence['events']] == ['entered', 'skipped_in_position', 'entered']
    assert evidence['skipped_in_position_count'] == 1
    assert len(body['trades']) == 2


@pytest.mark.parametrize('kind', [H2, H3])
@pytest.mark.parametrize('sign', [-1, 1])
@pytest.mark.parametrize('boundary', ['below', 'equal', 'above'])
def test_threshold_boundary(monkeypatch, tmp_path, kind, sign, boundary):
    values = params(kind)
    data = frame()
    size = {'below': 0.09, 'equal': 0.1, 'above': 0.11}[boundary] if kind == H2 else {'below': 4.9, 'equal': 5, 'above': 5.1}[boundary]
    if kind == H2:
        values['events'] = [event(expected=0, actual=sign*size)]
    else:
        price = 100+sign*size
        data.iloc[11] = [100, max(101, price), min(99, price), price, 1]
    body = post(monkeypatch, tmp_path, kind, values, data)
    assert len(body['trades']) == (0 if boundary == 'below' else 1)
    if boundary != 'below':
        assert body['trades'][0]['side'] == ('short' if sign > 0 else 'long')


@pytest.mark.parametrize('side', ['long', 'short'])
def test_h1_equal_range_boundary_does_not_trigger(monkeypatch, tmp_path, side):
    data = frame()
    data.iloc[12:, data.columns.get_loc('Close')] = 101 if side == 'long' else 99
    # window 96 outlasts the data (event bar 12 + 96 > 95), so the data end, not expiry, is the last word
    body = post(monkeypatch, tmp_path, H1, params(H1, direction=side, breakout_window_bars=96), data)
    assert not body['trades']
    assert report(body, H1)['events'][0]['reason'] == 'no_breakout_before_data_end'


@pytest.mark.parametrize('expected,actual', [(None, 1), (1, None), (None, None)])
def test_h2_null_skips_and_reports_details(monkeypatch, tmp_path, expected, actual):
    values = params(H2, events=[event(expected=expected, actual=actual), event(30, expected=2.9, actual=2.8)])
    body = post(monkeypatch, tmp_path, H2, values)
    evidence = report(body, H2)
    assert evidence['skipped_null_count'] == evidence['skipped_count'] == 1
    assert evidence['skipped_events'] == [dict(ts_utc=event()['ts_utc'], reason='missing_expected_or_actual')]
    assert len(body['trades']) == 1


@pytest.mark.parametrize('kind', KINDS)
def test_out_of_range_and_late_hold_are_skipped(monkeypatch, tmp_path, kind):
    extra = dict(expected=2.9, actual=2.8) if kind == H2 else {}
    items = [event(-1, **extra), event(96, **extra), event(94, **extra)]
    data = data_for(kind)
    if kind == H1:
        data.iloc[94] = [103, 106, 102, 105, 1]
    if kind == H3:
        data.iloc[93] = [100, 101, 94, 95, 1]
    body = post(monkeypatch, tmp_path, kind, params(kind, events=items), data)
    reasons = {r['reason'] for r in report(body, kind)['events']}
    assert 'event_before_data_start' in reasons and 'event_after_data_end' in reasons
    assert reasons & {'window_past_data_end', 'entry_past_data_end'}
    assert not body['trades']


@pytest.mark.parametrize('kind', KINDS)
def test_unknown_top_level_and_event_keys_reject_before_fetch(monkeypatch, tmp_path, kind):
    for extra in ({'unknown': 1}, {'atr_stop_multiplier': 2}, {'time_layer_enabled': True}):
        body = post(monkeypatch, tmp_path, kind, params(kind, **extra), invalid=True)
        assert body['error_type'] == 'INVALID_PARAMS' and 'unknown parameter' in body['error_message']
    values = params(kind)
    values['events'][0]['unknown'] = 1
    body = post(monkeypatch, tmp_path, kind, values, invalid=True)
    assert body['error_type'] == 'INVALID_PARAMS' and 'exactly' in body['error_message']


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('bad', [[], None, 'events', [event()]*51])
def test_event_count_type_reject(monkeypatch, tmp_path, kind, bad):
    body = post(monkeypatch, tmp_path, kind, params(kind, events=bad), invalid=True)
    assert body['error_type'] == 'INVALID_PARAMS'


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('field,value,reason', [
    ('ts_utc', [], 'ISO8601'), ('ts_utc', {}, 'ISO8601'), ('ts_utc', None, 'ISO8601'),
    ('ts_utc', '2026-01-01T01:00:00', 'ISO8601'), ('ts_utc', '2026-99-01T01:00:00Z', 'valid ISO8601'),
    ('label', '', 'non-empty'), ('label', 'a'*65, 'at most 64'), ('label', True, 'non-empty')])
def test_event_fields_reject(monkeypatch, tmp_path, kind, field, value, reason):
    values = params(kind)
    values['events'][0][field] = value
    body = post(monkeypatch, tmp_path, kind, values, invalid=True)
    assert body['error_type'] == 'INVALID_PARAMS' and reason in body['error_message']


@pytest.mark.parametrize('kind', KINDS)
def test_duplicate_normalized_timestamp_reject(monkeypatch, tmp_path, kind):
    values = params(kind)
    values['events'].append({**values['events'][0], 'ts_utc': '2026-01-01T03:00:00+02:00'})
    body = post(monkeypatch, tmp_path, kind, values, invalid=True)
    assert body['error_type'] == 'INVALID_PARAMS' and 'must not repeat' in body['error_message']


@pytest.mark.parametrize('kind,key,value', [
    (H1, 'pre_window_minutes', 0), (H1, 'max_hold_minutes', True), (H1, 'take_profit_r', 0),
    (H1, 'direction', 'up'), (H1, 'pre_window_minutes', 30.0), (H2, 'surprise_threshold', -1),
    (H2, 'hold_minutes', 0), (H2, 'stop_loss_pct', 100), (H2, 'direction_map', {'below': 'long'}),
    (H2, 'direction_map', {'below': 'long', 'above': 'short', 'extra': 'none'}),
    (H2, 'direction_map', {'below': 'up', 'above': 'short'}), (H2, 'direction_map', None),
    (H3, 'lookback_hours', 0), (H3, 'move_threshold_pct', 0), (H3, 'entry_delay_minutes', -1),
    (H3, 'entry_delay_minutes', 0.5), (H3, 'hold_minutes', False), (H3, 'stop_loss_pct', -1),
])
def test_parameter_rejection(monkeypatch, tmp_path, kind, key, value):
    body = post(monkeypatch, tmp_path, kind, params(kind, **{key: value}), invalid=True)
    assert body['error_type'] == 'INVALID_PARAMS' and key in body['error_message']


@pytest.mark.parametrize('key', ['expected', 'actual'])
@pytest.mark.parametrize('value', ['2.9', True, {}, []])
def test_h2_numeric_rejection(monkeypatch, tmp_path, key, value):
    values = params(H2)
    values['events'][0][key] = value
    body = post(monkeypatch, tmp_path, H2, values, invalid=True)
    assert body['error_type'] == 'INVALID_PARAMS' and f'events[].{key}' in body['error_message']


@pytest.mark.parametrize('key', ['expected', 'actual'])
@pytest.mark.parametrize('value', [float('nan'), float('inf'), -float('inf'), 10**1000])
def test_h2_nonfinite_rejection(key, value):
    values = params(H2)
    values['events'][0][key] = value
    with pytest.raises(ValueError, match=f'INVALID_PARAMS:events.*{key} must be a finite number or null'):
        p.TOOL_SPECS[f'local.backtesting_py.{H2}']['build'](values)


@pytest.mark.parametrize('kind', KINDS)
def test_catalog_contract(kind):
    schema = p._catalog_tool(f'local.backtesting_py.{kind}', p.TOOL_SPECS[f'local.backtesting_py.{kind}'], ['BTCUSDT'])['param_schema']
    assert schema['additionalProperties'] is False
    assert schema['required'] == ['events']
    events = schema['properties']['events']
    assert (events['minItems'], events['maxItems']) == (1, 50)
    expected = {'ts_utc', 'label', 'expected', 'actual'} if kind == H2 else {'ts_utc', 'label'}
    assert set(events['items']['required']) == set(events['items']['properties']) == expected
    assert events['items']['additionalProperties'] is False
    if kind == H2:
        assert events['items']['properties']['expected']['type'] == ['number', 'null']
    assert 'atr_stop_multiplier' not in schema['properties']


@pytest.mark.parametrize('kind', KINDS)
def test_spot_short_rejected_before_fetch(monkeypatch, tmp_path, kind):
    body = post(monkeypatch, tmp_path, kind, market='spot', invalid=True)
    assert body['error_type'] in ('INVALID_PARAMS', 'MARKET_UNSUPPORTED')


@pytest.mark.parametrize('kind', [H1, H2])
def test_spot_long_supported(monkeypatch, tmp_path, kind):
    values = params(kind, **({'direction': 'long'} if kind == H1 else {'direction_map': {'below': 'long', 'above': 'none'}}))
    assert len(post(monkeypatch, tmp_path, kind, values, market='spot')['trades']) == 1


@pytest.mark.parametrize('kind', KINDS)
def test_stop_triggers_intrabar_next_open(monkeypatch, tmp_path, kind):
    data = data_for(kind)
    entry = 13 if kind == H1 else 12 if kind == H2 else 14
    data.iloc[entry+1] = [100, 104, 95, 100, 1]
    data.iloc[entry+2] = [98, 99, 97, 98, 1]
    body = post(monkeypatch, tmp_path, kind, data=data)
    assert body['trades'][0]['closed_at'] == T0+(entry+2)*STEP
    assert report(body, kind)['events'][0]['exit_reason'] == 'stop_loss'


def test_h1_take_profit_uses_actual_fill_r(monkeypatch, tmp_path):
    data = data_for(H1)
    data.iloc[13] = [104, 105, 103, 104, 1]  # stop99, actual R5, take114 (not range height2)
    data.iloc[14] = [104, 114, 103, 104, 1]
    data.iloc[15] = [110, 111, 109, 110, 1]
    body = post(monkeypatch, tmp_path, H1, data=data)
    assert legs(body) == [(13, '104', 15, '110', 'long')]
    record = report(body, H1)['events'][0]
    assert float(record['take_price']) == 114
    assert record['exit_reason'] == 'take_profit'


def test_h1_gap_through_stop_cancelled(monkeypatch, tmp_path):
    data = data_for(H1)
    data.iloc[13] = [98, 99, 97, 98, 1]
    body = post(monkeypatch, tmp_path, H1, data=data)
    assert not body['trades']
    record = report(body, H1)['events'][0]
    assert record['status'] == 'rejected_at_fill' and record['reason'] == 'stop_wrong_side_of_fill'


@pytest.mark.parametrize('kind', KINDS)
def test_offset_and_off_grid_rounding(monkeypatch, tmp_path, kind):
    values = params(kind)
    values['events'][0]['ts_utc'] = '2026-01-01T03:00:01+02:00'
    body = post(monkeypatch, tmp_path, kind, values)
    expected = 13 if kind in (H1, H2) else 15
    assert body['trades'][0]['opened_at'] == T0+expected*STEP
    assert report(body, kind)['events'][0]['ts_utc'] == '2026-01-01T01:00:01+00:00'


@pytest.mark.parametrize('kind', [H1, H3])
def test_insufficient_history_skips_without_prefix_fetch(monkeypatch, tmp_path, kind):
    body = post(monkeypatch, tmp_path, kind, params(kind, events=[event(3)]))
    assert not body['trades']
    assert report(body, kind)['events'][0]['reason'] == 'insufficient_pre_event_history'


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('mode', ['risk', 'pct', 'notional'])
def test_shared_sizing_uses_intrinsic_stop(monkeypatch, tmp_path, kind, mode):
    sizing = {'risk': dict(position_size_risk_pct=1), 'pct': dict(position_size_pct=20),
              'notional': dict(position_size_notional=2000)}[mode]
    values = params(kind, compound=False, position_size_qty_step=0.1, **sizing)
    if kind == H1:
        values['take_profit_r'] = 2
    body = post(monkeypatch, tmp_path, kind, values)
    assert len(body['trades']) == 1
    fills = body['raw_report']['position_sizing']['fills']
    assert len(fills) == 1
    if mode == 'risk':
        assert float(fills[0]['qty']) == (25 if kind == H1 else 33.3)  # $100 / R4 or R3, floor0.1
        assert float(fills[0]['initial_stop']) == (99 if kind == H1 else 97)


@pytest.mark.parametrize('kind', KINDS)
def test_explicit_leverage_one_is_byte_identical(monkeypatch, tmp_path, kind):
    import json
    absent = post(monkeypatch, tmp_path, kind)
    explicit = post(monkeypatch, tmp_path, kind, params(kind, leverage=1))
    for body in (absent, explicit):
        body.pop('report_path', None)
    assert json.dumps(absent, sort_keys=True) == json.dumps(explicit, sort_keys=True)


@pytest.mark.parametrize('side', ['long', 'short'])
def test_h2_custom_map_and_none(monkeypatch, tmp_path, side):
    values = params(H2, direction_map=dict(below=side, above='none'))
    body = post(monkeypatch, tmp_path, H2, values)
    assert body['trades'][0]['side'] == side
    values['events'][0]['actual'] = 3.0
    body = post(monkeypatch, tmp_path, H2, values)
    assert not body['trades']
    assert report(body, H2)['events'][0]['reason'] == 'direction_map_none'


def test_h2_zero_surprise_even_at_zero_threshold_has_no_direction(monkeypatch, tmp_path):
    body = post(monkeypatch, tmp_path, H2, params(H2, surprise_threshold=0, events=[event(expected=1, actual=1)]))
    assert not body['trades']


def test_h2_null_count_has_precedence_over_overlap(monkeypatch, tmp_path):
    body = post(monkeypatch, tmp_path, H2, params(H2, events=[event(expected=1, actual=0), event(13, expected=None, actual=0)]))
    assert report(body, H2)['skipped_null_count'] == 1
    assert report(body, H2)['skipped_in_position_count'] == 0


@pytest.mark.parametrize('kind', KINDS)
def test_fifty_events_are_accepted_and_accounted(monkeypatch, tmp_path, kind):
    extra = dict(expected=None, actual=None) if kind == H2 else {}
    body = post(monkeypatch, tmp_path, kind, params(kind, events=[event(i, **extra) for i in range(50)]))
    assert len(report(body, kind)['events']) == 50
    assert sum(report(body, kind)['counts'].values()) == 50


def test_h3_zero_pre_event_open_skips_only_invalid_event(monkeypatch, tmp_path):
    data = frame(960)
    data.iloc[863] = data.iloc[899] = [100, 101, 94, 95, 1]
    values = params(H3, lookback_hours=72, events=[event(864), event(900)])
    baseline = post(monkeypatch, tmp_path, H3, values, data)
    assert legs(baseline) == [(866, '100', 869, '100', 'long'),
                              (902, '100', 905, '100', 'long')]

    data.iloc[0] = [0, 101, 0, 100, 1]
    body = post(monkeypatch, tmp_path, H3, values, data)
    evidence = report(body, H3)
    assert evidence['skipped_count'] == report(baseline, H3)['skipped_count'] + 1 == 1
    assert evidence['counts'] == {'skipped': 1, 'entered': 1}
    assert evidence['skipped_events'] == [dict(ts_utc=event(864)['ts_utc'],
                                              reason='invalid_pre_event_price')]
    assert evidence['events'][0]['reason'] == 'invalid_pre_event_price'
    assert evidence['events'][1] == report(baseline, H3)['events'][1]
    assert legs(body) == [(902, '100', 905, '100', 'long')]


def test_h3_zero_delay_and_post_release_prices_do_not_change_direction(monkeypatch, tmp_path):
    data = data_for(H3, 'short')
    data.iloc[12:] = [50, 51, 49, 50, 1]
    body = post(monkeypatch, tmp_path, H3, params(H3, entry_delay_minutes=0), data)
    assert legs(body) == [(12, '50', 15, '50', 'short')]
    assert report(body, H3)['events'][0]['move_pct'] == '5.00'


def test_h1_pre_window_is_frozen_and_ignores_older_bars(monkeypatch, tmp_path):
    data = data_for(H1)
    data.iloc[0, data.columns.get_loc('High')] = 1000
    data.iloc[12, data.columns.get_loc('High')] = 2000
    body = post(monkeypatch, tmp_path, H1, data=data)
    record = report(body, H1)['events'][0]
    assert (record['range_high'], record['range_low']) == (101, 99)
    assert len(body['trades']) == 1


@pytest.mark.parametrize('kind', KINDS)
def test_non_multiple_hold_rounds_up(monkeypatch, tmp_path, kind):
    values = params(kind, **({'max_hold_minutes': 11} if kind == H1 else {'hold_minutes': 11}))
    body = post(monkeypatch, tmp_path, kind, values)
    trade = body['trades'][0]
    assert trade['closed_at'] - trade['opened_at'] == 15*60


@pytest.mark.parametrize('kind', KINDS)
def test_stop_never_reenters_same_event(monkeypatch, tmp_path, kind):
    data = data_for(kind)
    entry = 13 if kind == H1 else 12 if kind == H2 else 14
    data.iloc[entry] = [103 if kind == H1 else 100, 104, 95, 100, 1]
    body = post(monkeypatch, tmp_path, kind, data=data)
    assert len(body['trades']) == 1
    assert report(body, kind)['events'][0]['exit_reason'] == 'stop_loss'


@pytest.mark.parametrize('kind', [H1, H3])
def test_history_gap_is_skipped(monkeypatch, tmp_path, kind):
    data = data_for(kind).drop(data_for(kind).index[10])
    body = post(monkeypatch, tmp_path, kind, data=data)
    assert not body['trades']
    assert report(body, kind)['events'][0]['reason'] == 'pre_event_history_gap'


@pytest.mark.parametrize('kind', KINDS)
def test_event_in_missing_candle_skips(monkeypatch, tmp_path, kind):
    data = data_for(kind).drop(data_for(kind).index[12])
    body = post(monkeypatch, tmp_path, kind, data=data)
    assert not body['trades']
    assert report(body, kind)['events'][0]['reason'] == 'event_in_data_gap'


@pytest.mark.parametrize('kind', KINDS)
def test_default_parameters_have_requested_durations(monkeypatch, tmp_path, kind):
    data = data_for(kind) if kind == H1 else frame(2000 if kind == H3 else 400)
    at = 900 if kind == H3 else 12
    if kind == H3:
        data.iloc[at-1] = [100, 106, 99, 105, 1]
    values = dict(events=[event(at, **(dict(expected=2.9, actual=2.8) if kind == H2 else {}))])
    body = post(monkeypatch, tmp_path, kind, values, data)
    expected_entry = 13 if kind == H1 else 12 if kind == H2 else 906
    expected_hold = 48 if kind == H1 else 288 if kind == H2 else 576
    trade = body['trades'][0]
    assert trade['opened_at'] == T0+expected_entry*STEP
    assert trade['closed_at'] == T0+(expected_entry+expected_hold)*STEP


@pytest.mark.parametrize('kind', KINDS)
def test_leveraged_liquidation_is_settled_in_event_report(monkeypatch, tmp_path, kind):
    data = data_for(kind)
    entry = 13 if kind == H1 else 12 if kind == H2 else 14
    # Gap through both stop and liquidation; shared arbitration settles at the gap open.
    data.iloc[entry+1] = [50, 51, 49, 50, 1]
    body = post(monkeypatch, tmp_path, kind, params(kind, leverage=5), data)
    record = report(body, kind)['events'][0]
    assert record['status'] == 'entered'
    assert record['exit_price'] == body['trades'][0]['exit_price']
    assert record['exit_reason'] == 'liquidation'


@pytest.mark.parametrize('kind', KINDS)
def test_early_insolvency_accounts_for_unreached_events(monkeypatch, tmp_path, kind):
    data = data_for(kind, 'short')
    entry = 13 if kind == H1 else 12 if kind == H2 else 14
    data.iloc[entry+1:] = [1000, 1001, 999, 1000, 1]
    extra = dict(expected=1, actual=2) if kind == H2 else {}
    values = params(kind, events=[event(12, **extra), event(30, **extra)])
    body = post(monkeypatch, tmp_path, kind, values, data)
    first, second = report(body, kind)['events']
    assert first['status'] == 'entered' and first['exit_reason'] == 'insolvency'
    assert second['status'] == 'skipped_run_ended' and second['reason'] == 'insolvency_break'


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('tail', [False, True])
def test_holding_gap_requires_a_real_next_open(monkeypatch, tmp_path, kind, tail):
    data = data_for(kind)
    entry = 13 if kind == H1 else 12 if kind == H2 else 14
    if tail:
        data = data.iloc[:entry+4]
    data = data.drop(data.index[entry+2])
    body = post(monkeypatch, tmp_path, kind, data=data)
    record = report(body, kind)['events'][0]
    if tail:
        assert not body['trades']
        assert record['reason'] == 'window_past_data_end'
    else:
        assert body['trades'][0]['closed_at'] == T0+(entry+4)*STEP
        assert record['planned_exit_decision_bar_utc'] == event(entry+3)['ts_utc']
