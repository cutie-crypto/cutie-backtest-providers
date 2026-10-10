"""P-EVENT0: local.backtesting_py.event_window (inline event windows).

Frame: 1h candles from 2026-01-01 00:00 UTC, bar i has Open 100+i, High 101+i, Low 99+i, Close 100.5+i,
so every expected price below is read straight off the bar index (hand arithmetic, never generated).
Event bar = open <= ts < next open; entry = open of the bar bars_before before it (queued at the
previous close); window exit = queued at the close of the bars_after-th held bar (entry bar = 1),
filled at the next open; a risk exit that comes first wins.
"""
import json
from pathlib import Path
import sys

import pandas as pd
import pytest
from backtesting import Backtest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p  # noqa: E402
from strategy_entry_filters import PATTERN_CONFIRM_PARAM_SCHEMA_PROPERTIES  # noqa: E402

TOOL = 'local.backtesting_py.event_window'
GOLDEN = Path(__file__).parent / 'fixtures' / 'event0_window_golden.json'
H = 3600
T0 = 1767225600  # 2026-01-01T00:00:00Z
# Listed out of order on purpose: processing is by instant.
EVENTS = [dict(ts_utc='2026-01-01T12:20:00+02:00', label='E3 off-grid'),   # 10:20Z -> event bar 10
          dict(ts_utc='2026-01-01T05:00:00Z', label='E1 on-grid'),         # event bar 5
          dict(ts_utc='2026-01-01T06:30:00+00:00', label='E2 in window')]  # event bar 6
GOLDEN_PARAMS = dict(events=EVENTS, bars_before=1, bars_after=3)


def frame(n=24):
    return pd.DataFrame(dict(Open=[100. + i for i in range(n)], High=[101. + i for i in range(n)],
                             Low=[99. + i for i in range(n)], Close=[100.5 + i for i in range(n)],
                             Volume=[1.] * n), index=pd.date_range('2026-01-01', periods=n, freq='h'))


def prefix(data, bars, drop=None):
    index = pd.date_range(end=data.index[0] - pd.Timedelta(hours=1), periods=bars, freq='h')
    out = pd.DataFrame(dict(Open=[100.] * bars, High=[101.] * bars, Low=[99.] * bars, Close=[100.] * bars,
                            Volume=[1.] * bars), index=index)
    return out.drop(out.index[drop]) if drop is not None else out


def post(monkeypatch, tmp_path, params, data=None, market='futures', warmup=None):
    data = frame() if data is None else data
    calls = []

    def fetch_warmup(*args, **kwargs):
        calls.append(args)
        if warmup is None:
            raise AssertionError('event_window fetches no warmup while the filter is off')
        return warmup.copy()
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', Path(tmp_path))
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a, **k: data.copy())
    monkeypatch.setattr(p, '_fetch_template_warmup', fetch_warmup)
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **k: None)
    request = dict(run_id='event0', provider_tool_id=TOOL, provider_params=params, symbol='BTCUSDT',
                   market=market, timeframe='1h', start_at=int(data.index[0].timestamp()),
                   end_at=int(data.index[-1].timestamp()) + H, initial_capital='10000', fee_bps='0', slippage_bps='0')
    return TestClient(p.app).post('/cutie/backtest', json={'backtest': request}).json()


def events_of(body):
    return body['assumptions']['event_window']['events']


def check_event_records(body):
    """Every event carries a status; an entered event's prices are the result.v2 row strings, not floats."""
    from datetime import datetime
    for record in events_of(body):
        assert 'status' in record, record
        if record['status'] != 'entered':
            continue
        opened = int(datetime.fromisoformat(record['entry_utc']).timestamp())
        rows = [t for t in body['trades'] if t['opened_at'] == opened]
        assert rows and type(record['entry_price']) is str and record['entry_price'] == rows[0]['entry_price']
        assert [type(e['price']) for e in record['exits']] == [str] * len(rows)
        assert [e['price'] for e in record['exits']] == [t['exit_price'] for t in rows]
        assert type(record['exit_price']) is str and record['exit_price'] == rows[-1]['exit_price']


def legs(body):
    return [((t['opened_at'] - T0) // H, t['entry_price'], (t['closed_at'] - T0) // H, t['exit_price'], t['side'])
            for t in body['trades']]


def canonical(body):
    body = dict(body)
    body.pop('report_path', None)
    return json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False) + '\n'


# --- golden: three events ---------------------------------------------------------------------------

def test_golden_three_events_hand_computed(monkeypatch, tmp_path):
    body = post(monkeypatch, tmp_path, GOLDEN_PARAMS)
    assert body['result_status'] == 'success', body.get('error_message')
    # E1 event bar 5 -> entry bar 4 (queued at bar 3 close) open 104; held bars 4,5,6 -> exit queued
    #    at bar 6 close, filled at bar 7 open 107.
    # E2 event bar 6 -> entry bar 5, decided at bar 4 close while E1 is held -> skipped.
    # E3 10:20Z inside bar 10 -> entry bar 9 open 109; held 9,10,11 -> filled at bar 12 open 112.
    assert legs(body) == [(4, '104', 7, '107', 'long'), (9, '109', 12, '112', 'long')]
    records = events_of(body)
    assert [(r['label'], r['status']) for r in records] == [
        ('E1 on-grid', 'entered'), ('E2 in window', 'skipped_in_position'), ('E3 off-grid', 'entered')]
    assert [(r['event_bar_utc'], r['entry_bar_utc'], r['planned_exit_decision_bar_utc']) for r in records] == [
        ('2026-01-01T05:00:00+00:00', '2026-01-01T04:00:00+00:00', '2026-01-01T06:00:00+00:00'),
        ('2026-01-01T06:00:00+00:00', '2026-01-01T05:00:00+00:00', '2026-01-01T07:00:00+00:00'),
        ('2026-01-01T10:00:00+00:00', '2026-01-01T09:00:00+00:00', '2026-01-01T11:00:00+00:00')]
    entered = [r for r in records if r['status'] == 'entered']
    assert [(r['entry_price'], r['exit_price'], r['exit_reason'], r['exit_utc']) for r in entered] == [
        ('104', '107', 'window_end', '2026-01-01T07:00:00+00:00'),
        ('109', '112', 'window_end', '2026-01-01T12:00:00+00:00')]
    assert [r['exits'] for r in entered] == [
        [dict(time='2026-01-01T07:00:00+00:00', price='107', reason='window_end')],
        [dict(time='2026-01-01T12:00:00+00:00', price='112', reason='window_end')]]
    assert 'exits' not in records[1]
    assert body['assumptions']['event_window']['entry'] == 'open_of_bar_bars_before_earlier_than_event_bar'
    assert records[2]['ts_utc'] == '2026-01-01T10:20:00+00:00'
    # result.v2 review rule: every fill lies inside its own bucket's [low, high].
    data = frame()
    for trade in body['trades']:
        for at, price in ((trade['opened_at'], trade['entry_price']), (trade['closed_at'], trade['exit_price'])):
            bar = data.iloc[(at - T0) // H]
            assert bar.Low <= float(price) <= bar.High
    assert set(body['trades'][0]) == {'seq', 'opened_at', 'closed_at', 'side', 'qty', 'entry_price', 'exit_price',
                                      'fee', 'slippage', 'pnl'}


def test_golden_bytes(monkeypatch, tmp_path):
    assert canonical(post(monkeypatch, tmp_path, GOLDEN_PARAMS)) == GOLDEN.read_text()


def test_short_mirror(monkeypatch, tmp_path):
    body = post(monkeypatch, tmp_path, {**GOLDEN_PARAMS, 'direction': 'short'})
    assert legs(body) == [(4, '104', 7, '107', 'short'), (9, '109', 12, '112', 'short')]
    assert all(float(t['pnl']) < 0 for t in body['trades'])  # shorting a rising frame loses
    assert [r['status'] for r in events_of(body)] == ['entered', 'skipped_in_position', 'entered']
    assert body['assumptions']['event_window']['direction'] == 'short'


# --- boundaries -------------------------------------------------------------------------------------

def one(ts, **params):
    return dict(events=[dict(ts_utc=ts, label='x')], **params)


@pytest.mark.parametrize('params,expected', [
    # bars_before 0: event bar 5 is the entry bar; bars_after 1 exits at the next open.
    (one('2026-01-01T05:00:00Z', bars_before=0, bars_after=1), (5, '105', 6, '106')),
    # bars_before 48: event bar 60 -> entry bar 12.
    (one('2026-01-03T12:00:00Z', bars_before=48, bars_after=1), (12, '112', 13, '113')),
    # bars_after 96: entry bar 10, held 10..105, filled at bar 106.
    (one('2026-01-01T10:00:00Z', bars_before=0, bars_after=96), (10, '110', 106, '206')),
    # both maxima: event bar 60 -> entry 12, held 12..107, filled at bar 108.
    (one('2026-01-03T12:59:59Z', bars_before=48, bars_after=96), (12, '112', 108, '208')),
])
def test_bar_count_boundaries(monkeypatch, tmp_path, params, expected):
    body = post(monkeypatch, tmp_path, params, data=frame(120))
    assert body['result_status'] == 'success', body.get('error_message')
    assert legs(body) == [(*expected, 'long')]
    assert events_of(body)[0]['status'] == 'entered'


def test_out_of_range_events_are_skipped(monkeypatch, tmp_path):
    events = [dict(ts_utc='2025-12-31T23:59:59Z', label='before data'),
              dict(ts_utc='2026-01-01T01:00:00Z', label='entry bar 0'),       # bars_before 1 -> bar 0
              dict(ts_utc='2026-01-01T02:00:00Z', label='entry bar 1'),       # no close before bar 1 is seen
              dict(ts_utc='2026-01-01T00:30:00Z', label='entry before start'),  # bar 0 - 1
              dict(ts_utc='2026-01-01T20:00:00Z', label='window past end'),   # entry 19, held 19..21, needs bar 22
              dict(ts_utc='2026-01-02T00:00:00Z', label='after data'),        # last bar 23 ends 2026-01-02T00:00
              dict(ts_utc='2026-01-01T19:00:00Z', label='last fitting')]      # entry 18, exit filled at bar 21
    body = post(monkeypatch, tmp_path, dict(events=events, bars_before=1, bars_after=3), data=frame(22))
    assert body['result_status'] == 'success', body.get('error_message')
    got = {r['label']: (r['status'], r.get('reason')) for r in events_of(body)}
    assert got == {
        'before data': ('skipped_out_of_range', 'event_before_data_start'),
        'entry before start': ('skipped_out_of_range', 'entry_before_data_start'),
        'entry bar 0': ('skipped_out_of_range', 'entry_before_first_decision'),
        'entry bar 1': ('skipped_out_of_range', 'entry_before_first_decision'),
        'last fitting': ('entered', None),
        'window past end': ('skipped_out_of_range', 'window_past_data_end'),
        'after data': ('skipped_out_of_range', 'event_after_data_end'),
    }
    assert legs(body) == [(18, '118', 21, '121', 'long')]


def test_event_inside_a_data_gap_is_skipped(monkeypatch, tmp_path):
    data = frame(30).drop(pd.Timestamp('2026-01-01 10:00'))
    body = post(monkeypatch, tmp_path, one('2026-01-01T10:30:00Z', bars_after=2), data=data)
    assert [(r['status'], r['reason']) for r in events_of(body)] == [('skipped_out_of_range', 'event_in_data_gap')]
    assert body['trades'] == []


# --- risk / time / filter / sizing ------------------------------------------------------------------

def stop_frame():
    data = frame()
    data.iloc[5] = [105, 106, 94, 95, 1]   # close 95 and low 94 both breach a 2% stop on entry 104 (101.92)
    data.iloc[6] = [95, 96, 94, 95.5, 1]
    return data


@pytest.mark.parametrize('risk_layer,reason', [(False, 'risk_exit'), (True, 'stop_loss')])
def test_stop_exit_precedes_window_end(monkeypatch, tmp_path, risk_layer, reason):
    params = {**one('2026-01-01T05:00:00Z', bars_before=1, bars_after=3), 'stop_loss_pct': 2,
              'risk_layer_enabled': risk_layer}
    body = post(monkeypatch, tmp_path, params, data=stop_frame())
    # Entry bar 4 open 104; stop decided at bar 5's close, filled at bar 6 open 95 -- before the
    # window-end fill at bar 7.
    assert legs(body) == [(4, '104', 6, '95', 'long')]
    record = events_of(body)[0]
    assert (record['status'], record['exit_reason'], record['exit_price']) == ('entered', reason, '95')


def test_time_layer_closed_gate_is_recorded(monkeypatch, tmp_path):
    params = {**GOLDEN_PARAMS, 'time_layer_enabled': True, 'time_session_start': '00:00', 'time_session_end': '06:00'}
    body = post(monkeypatch, tmp_path, params)
    # E1 decides at 04:00 (inside); E3 decides at 09:00 (outside) -> blocked, never filled.
    assert legs(body) == [(4, '104', 7, '107', 'long')]
    assert [r['status'] for r in events_of(body)] == ['entered', 'skipped_in_position', 'blocked_by_time_or_filter']
    assert 'time_layer' in body['assumptions']


def falling(n=24):
    data = frame(n)
    data[['Open', 'High', 'Low', 'Close']] = data[['Open', 'High', 'Low', 'Close']].values[::-1] - 50
    return data


FILTER = dict(filter_layer_enabled=True, filter_ema_enabled=True, filter_ema_period=2)


def test_entry_filter_blocks_long_and_passes_short(monkeypatch, tmp_path):
    data = falling()
    long_body = post(monkeypatch, tmp_path, {**GOLDEN_PARAMS, **FILTER}, data=data, warmup=prefix(data, 2))
    assert long_body['result_status'] == 'success', long_body.get('error_message')
    assert long_body['trades'] == []
    assert [r['status'] for r in events_of(long_body)] == ['blocked_by_time_or_filter'] * 3
    short_body = post(monkeypatch, tmp_path, {**GOLDEN_PARAMS, **FILTER, 'direction': 'short'}, data=data,
                      warmup=prefix(data, 2))
    assert [r['status'] for r in events_of(short_body)] == ['entered', 'skipped_in_position', 'entered']


def test_filter_on_gapped_warmup_fails_before_backtest(monkeypatch, tmp_path):
    monkeypatch.setattr(Backtest, 'run', lambda *a, **k: pytest.fail('backtest ran on a gapped filter warmup'))
    data = frame()
    body = post(monkeypatch, tmp_path, {**GOLDEN_PARAMS, **FILTER}, data=data, warmup=prefix(data, 3, drop=1))
    assert (body['result_status'], body['error_type']) == ('failed', 'INSUFFICIENT_DATA'), body


def test_risk_sizing_without_stop_is_rejected_before_fetch(monkeypatch, tmp_path):
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a, **k: pytest.fail('fetched'))
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    request = dict(run_id='event0', provider_tool_id=TOOL, provider_params={**GOLDEN_PARAMS, 'position_size_risk_pct': 1},
                   symbol='BTCUSDT', market='futures', timeframe='1h', start_at=T0, end_at=T0 + 24 * H,
                   initial_capital='10000', fee_bps='0', slippage_bps='0')
    body = TestClient(p.app).post('/cutie/backtest', json={'backtest': request}).json()
    assert (body['result_status'], body['error_type']) == ('failed', 'INVALID_PARAMS'), body
    assert body['raw_report']['position_sizing'] == {'rejections': [{'reason': 'missing_initial_stop'}]}


def test_risk_sizing_with_user_stop(monkeypatch, tmp_path):
    params = {**one('2026-01-01T05:00:00Z', bars_before=1, bars_after=3), 'stop_loss_pct': 2,
              'position_size_risk_pct': 1, 'position_size_qty_step': 0.001}
    body = post(monkeypatch, tmp_path, params)
    # 1% of 10000 = 100 at risk; fill 104, stop 104 * 0.98 = 101.92 -> 100 / 2.08 = 48.0769.. -> 48.076
    fill = body['raw_report']['position_sizing']['fills'][0]
    assert (fill['fill_price'], fill['qty'], fill['initial_stop']) == ('104.0', '48.076', '101.920')
    assert legs(body) == [(4, '104', 7, '107', 'long')]


# --- validation -------------------------------------------------------------------------------------

def ev(**item):
    return dict(events=[{'ts_utc': '2026-01-01T05:00:00Z', 'label': 'x', **item}])


INVALID = {
    'naive_ts': ev(ts_utc='2026-01-01T05:00:00'),
    'date_only': ev(ts_utc='2026-01-01'),
    'space_separator': ev(ts_utc='2026-01-01 05:00:00Z'),
    'unparsable_ts': ev(ts_utc='2026-13-01T05:00:00Z'),
    'ts_not_string': ev(ts_utc=1767243600),
    'duplicate_ts': dict(events=[dict(ts_utc='2026-01-01T05:00:00Z', label='a'),
                                 dict(ts_utc='2026-01-01T07:00:00+02:00', label='b')]),
    'empty_label': ev(label=''),
    'blank_label': ev(label='   '),
    'long_label': ev(label='x' * 65),
    'label_not_string': ev(label=5),
    'extra_key': ev(note='x'),
    'missing_label': dict(events=[dict(ts_utc='2026-01-01T05:00:00Z')]),
    'item_not_object': dict(events=['2026-01-01T05:00:00Z']),
    'missing_events': {},
    'empty_events': dict(events=[]),
    'too_many_events': dict(events=[dict(ts_utc=f'2026-01-01T00:{m:02d}:00Z', label='x') for m in range(51)]),
    'events_not_list': dict(events={'ts_utc': '2026-01-01T05:00:00Z', 'label': 'x'}),
    'bars_before_negative': {**ev(), 'bars_before': -1},
    'bars_before_49': {**ev(), 'bars_before': 49},
    'bars_after_0': {**ev(), 'bars_after': 0},
    'bars_after_97': {**ev(), 'bars_after': 97},
    'bars_after_float': {**ev(), 'bars_after': 1.5},
    'direction_both': {**ev(), 'direction': 'both'},
    'pattern_confirm_switch': {**ev(), 'filter_layer_enabled': True, 'filter_pattern_confirm_enabled': True},
}


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a, **k: pytest.fail('invalid params reached market data'))
    return TestClient(p.app)


def submit(client, params, market='futures'):
    request = dict(run_id='event0', provider_tool_id=TOOL, provider_params=params, symbol='BTCUSDT', market=market,
                   timeframe='1h', start_at=T0, end_at=T0 + 24 * H, initial_capital='10000', fee_bps='0',
                   slippage_bps='0')
    return client.post('/cutie/backtest', json={'backtest': request}).json()


@pytest.mark.parametrize('case', INVALID)
def test_invalid_params_rejected_before_fetch(client, case):
    body = submit(client, INVALID[case])
    assert (body['result_status'], body['error_type']) == ('failed', 'INVALID_PARAMS'), body
    with pytest.raises(ValueError, match='INVALID_PARAMS'):
        p.TOOL_SPECS[TOOL]['build'](INVALID[case])


def test_spot_short_rejected_and_long_accepted(client, monkeypatch, tmp_path):
    body = submit(client, {**ev(), 'direction': 'short'}, market='spot')
    assert (body['error_type'], body['error_message']) == ('INVALID_PARAMS', 'event window short requires futures market')
    assert post(monkeypatch, tmp_path, GOLDEN_PARAMS, market='spot')['result_status'] == 'success'


def test_catalog_entry():
    spec = p.TOOL_SPECS[TOOL]
    props = spec['param_schema_properties']
    assert props['events']['minItems'] == 1 and props['events']['maxItems'] == 50
    assert (props['bars_before']['default'], props['bars_before']['maximum']) == (0, 48)
    assert (props['bars_after']['default'], props['bars_after']['minimum'], props['bars_after']['maximum']) == (12, 1, 96)
    assert props['direction'] == {'type': 'string', 'default': 'long', 'enum': ['long', 'short']}
    for key in ('stop_loss_pct', 'position_size_risk_pct', 'time_layer_enabled', 'filter_layer_enabled', 'leverage'):
        assert key in props, key
    assert not set(PATTERN_CONFIRM_PARAM_SCHEMA_PROPERTIES) & set(props)
    assert TOOL not in p.POSITION_SIZING_UNWIRED_TOOLS | p.POSITION_SIZING_TEMPLATE_STOP_TOOLS
    assert spec.get('markets', ['spot', 'futures']) == ['spot', 'futures']
    assert 'ahead' not in spec['description']
    built = spec['build'](GOLDEN_PARAMS)
    assert issubclass(built['strategy'], p._FixedRiskMixin)


@pytest.mark.parametrize('key', ['atr_stop_multiplier', 'risk_atr_period'])
def test_atr_keys_not_declared_and_rejected_before_fetch(client, key):
    # No risk_warmup prefix is fetched for event_window, so an ATR stop could not be served.
    assert key not in p.TOOL_SPECS[TOOL]['param_schema_properties']
    assert key not in p._catalog_tool(TOOL, p.TOOL_SPECS[TOOL], ['BTCUSDT'])['param_schema']['properties']
    body = submit(client, {**ev(), 'risk_layer_enabled': True, 'atr_stop_multiplier': 2, 'risk_atr_period': 14})
    assert (body['error_type'], body['error_message']) == (
        'INVALID_PARAMS', "unknown parameter 'atr_stop_multiplier' (not in tool param_schema)")
    body = submit(client, {**ev(), key: 2})
    assert (body['error_type'], body['error_message']) == (
        'INVALID_PARAMS', f"unknown parameter '{key}' (not in tool param_schema)")


# --- exits: one entered event = one holding lifecycle, possibly several result.v2 rows ---------------

PARTIAL = {**one('2026-01-01T05:00:00Z', bars_before=1, bars_after=3), 'risk_layer_enabled': True,
           'stop_loss_pct': 2, 'tp1_r': 0.5, 'tp1_close_pct': 50, 'tp2_r': 10, 'tp2_close_pct': 50}


def test_partial_take_profit_then_window_end_lists_both_exits(monkeypatch, tmp_path):
    body = post(monkeypatch, tmp_path, PARTIAL)
    assert body['result_status'] == 'success', body.get('error_message')
    # Entry bar 4 open 104, stop 101.92, R 2.08: tp1 0.5R = 105.04 is touched by bar 5's high 106 ->
    # half closed at bar 6 open 106; tp2 10R = 124.8 is never reached, so the window (held 4,5,6)
    # closes the rest at bar 7 open 107.
    assert legs(body) == [(4, '104', 6, '106', 'long'), (4, '104', 7, '107', 'long')]
    record = events_of(body)[0]
    assert record['exits'] == [dict(time='2026-01-01T06:00:00+00:00', price='106', reason='take_profit_levels'),
                               dict(time='2026-01-01T07:00:00+00:00', price='107', reason='window_end')]
    assert (record['exit_reason'], record['exit_price'], record['exit_utc']) == (
        'window_end', '107', '2026-01-01T07:00:00+00:00')


def liquidation_frame():
    data = frame()
    data.iloc[5] = [98, 99, 97, 97.5, 1]   # gaps below the 20x long liquidation price 104 * (1 - 1/20) = 98.8
    return data


def blowout_frame():
    data = frame()
    data.iloc[5] = [250, 260, 240, 255, 1]  # an unlevered short from 104 loses more than the whole account
    return data


LIQUIDATION = {**one('2026-01-01T05:00:00Z', bars_before=1, bars_after=3), 'leverage': 20, 'stop_loss_pct': 50}
BLOWOUT = {**one('2026-01-01T05:00:00Z', bars_before=1, bars_after=3), 'direction': 'short'}


def test_isolated_liquidation_exit_uses_settled_fill(monkeypatch, tmp_path):
    body = post(monkeypatch, tmp_path, LIQUIDATION, data=liquidation_frame())
    assert body['result_status'] == 'success', body.get('error_message')
    # Entry bar 4 open 104; bar 5 opens at 98 below the liquidation price 98.8, so result.v2 settles the
    # gap at that open (closed_at bar 5). The broker itself closed at bar 5's close 97.5 when the
    # account went insolvent -- that raw price must not leak into exits.
    assert legs(body) == [(4, '104', 5, '98', 'long')]
    (detail,) = body['raw_report']['isolated_risk']['liquidations']
    assert (detail['seq'], detail['fill_price']) == (1, '98')
    record = events_of(body)[0]
    assert record['exits'] == [dict(time='2026-01-01T05:00:00+00:00', price='98', reason='liquidation')]
    assert (record['exit_reason'], record['exit_price'], record['exit_utc']) == (
        'liquidation', '98', '2026-01-01T05:00:00+00:00')


def test_unlevered_short_blowout_is_insolvency(monkeypatch, tmp_path):
    body = post(monkeypatch, tmp_path, BLOWOUT, data=blowout_frame())
    assert body['result_status'] == 'success', body.get('error_message')
    # Short at bar 4 open 104; bar 5 opens at 250, equity <= 0, the broker closes at bar 5's close 255
    # and ends the run before next() sees bar 5.
    assert legs(body) == [(4, '104', 5, '255', 'short')]
    assert 'isolated_risk' not in body['raw_report']
    record = events_of(body)[0]
    assert record['exits'] == [dict(time='2026-01-01T05:00:00+00:00', price='255', reason='insolvency')]
    assert record['exit_reason'] == 'insolvency'


def entry_blowout_frame():
    data = frame()
    data.iloc[4] = [104, 260, 100, 250, 1]  # the entry bar itself: a short from 104 is wiped out by its close 250
    return data


ENTRY_BLOWOUT = {**one('2026-01-01T05:00:00Z', bars_before=1, bars_after=3), 'direction': 'short'}


def test_insolvent_on_the_entry_bar_is_entered_with_one_insolvency_exit(monkeypatch, tmp_path):
    body = post(monkeypatch, tmp_path, ENTRY_BLOWOUT, data=entry_blowout_frame())
    assert body['result_status'] == 'success', body.get('error_message')
    # The short fills at bar 4 open 104 and the broker's equity check on that same bar closes it at
    # bar 4's close 250 and breaks the run: next() never runs on bar 4.
    assert legs(body) == [(4, '104', 4, '250', 'short')]
    record = events_of(body)[0]
    assert (record['status'], record['entry_utc'], record['entry_price']) == (
        'entered', '2026-01-01T04:00:00+00:00', '104')
    assert record['exits'] == [dict(time='2026-01-01T04:00:00+00:00', price='250', reason='insolvency')]
    assert (record['exit_reason'], record['exit_price'], record['exit_utc']) == (
        'insolvency', '250', '2026-01-01T04:00:00+00:00')


LAST_BAR = {**PARTIAL, 'bars_after': 19, 'tp2_r': 9, 'tp2_close_pct': 25, 'trailing_stop_pct': 50}


def test_close_decided_on_the_last_bar_keeps_its_reason(monkeypatch, tmp_path):
    body = post(monkeypatch, tmp_path, LAST_BAR)
    assert body['result_status'] == 'success', body.get('error_message')
    # tp1 as in PARTIAL (bar 6 open 106); tp2 9R = 122.72 is first touched by bar 22's high 123, so a quarter
    # closes at bar 23 open 123 -- which defers the window close (held 4..22) to bar 23's close. Bar 23 is
    # the last: finalize fills that queued close at the same open 123, ahead of its own close.
    assert legs(body) == [(4, '104', 6, '106', 'long'), (4, '104', 23, '123', 'long'), (4, '104', 23, '123', 'long')]
    assert [e['reason'] for e in events_of(body)[0]['exits']] == [
        'take_profit_levels', 'take_profit_levels', 'window_end']


def test_settle_derives_outcomes_and_fails_closed_on_unattributable_rows():
    from strategy_event_window import settle_event_window_exits

    def submitted():
        return dict(status='submitted', ts_utc='2026-01-01T05:00:00+00:00', entry_bar_utc='2026-01-01T04:00:00+00:00')
    opens = [T0 + i * H for i in range(12)]
    row = dict(seq=1, opened_at=T0 + 4 * H, closed_at=T0 + 7 * H, entry_price='104', exit_price='107')

    def settle(events, rows, isolated_risk=None, decisions=None, stop_bar=None):
        settle_event_window_exits(events, rows, isolated_risk, decisions or {}, opens, stop_bar)
        return events[0]
    record = settle([submitted()], [row], decisions={6: 'window_end'})
    assert (record['status'], record['entry_utc'], record['entry_price']) == (
        'entered', '2026-01-01T04:00:00+00:00', '104')
    assert record['exits'] == [dict(time='2026-01-01T07:00:00+00:00', price='107', reason='window_end')]
    # No row opened on the entry bar: the order was dropped at the fill.
    assert settle([submitted()], []) == dict(submitted(), status='rejected_at_fill')
    # Reason priority: liquidation > the decision one bar earlier > the break bar > finalize.
    assert settle([submitted()], [row], {'liquidations': [dict(seq=1)]}, {6: 'stop_loss'}, 7)['exit_reason'] == 'liquidation'
    assert settle([submitted()], [row], None, {6: 'stop_loss'}, 7)['exit_reason'] == 'stop_loss'
    assert settle([submitted()], [row], None, {7: 'stop_loss'}, 7)['exit_reason'] == 'insolvency'
    assert settle([submitted()], [row], None, {5: 'stop_loss'})['exit_reason'] == 'engine_finalize_trades_settlement'
    # One decision = one close order = at most one row: a second row at the same closed_at is the engine's.
    twin = {**row, 'seq': 2}
    assert [e['reason'] for e in settle([submitted()], [row, twin], None, {6: 'take_profit_levels'}, 7)['exits']] == [
        'take_profit_levels', 'insolvency']
    # A close queued on the last bar (11) is filled by finalize at that bar's open, after bar 10's decision.
    late = [{**row, 'closed_at': T0 + 11 * H}, {**twin, 'closed_at': T0 + 11 * H}]
    assert [e['reason'] for e in settle([submitted()], late, None, {10: 'take_profit_levels', 11: 'window_end'})[
        'exits']] == ['take_profit_levels', 'window_end']
    with pytest.raises(ValueError, match='matches 0 submitted events'):
        settle([submitted()], [row, {**row, 'seq': 2, 'opened_at': T0 + 9 * H}])
    with pytest.raises(ValueError, match='matches 2 submitted events'):
        settle([submitted(), submitted()], [row])


@pytest.mark.parametrize('params,data', [(GOLDEN_PARAMS, frame), (PARTIAL, frame),
                                         ({**GOLDEN_PARAMS, 'direction': 'short'}, frame),
                                         ({**PARTIAL, 'events': EVENTS, 'tp2_r': 2}, frame),
                                         (LIQUIDATION, liquidation_frame), (BLOWOUT, blowout_frame),
                                         (ENTRY_BLOWOUT, entry_blowout_frame)],
                         ids=['golden', 'partial', 'short', 'partial_three_events', 'liquidation', 'insolvency',
                              'insolvency_on_entry_bar'])
def test_exits_count_equals_result_rows(monkeypatch, tmp_path, params, data):
    body = post(monkeypatch, tmp_path, params, data=data())
    check_event_records(body)
    entered = [r for r in events_of(body) if r['status'] == 'entered']
    assert entered and sum(len(r['exits']) for r in entered) == len(body['trades'])
    assert [(e['time'], e['price']) for r in entered for e in r['exits']] == [
        (pd.Timestamp(t['closed_at'], unit='s', tz='UTC').isoformat(), t['exit_price']) for t in body['trades']]


if __name__ == '__main__':
    # Capture once: python3 tests/test_event0_event_window.py (writes the golden fixture).
    import tempfile
    mp = pytest.MonkeyPatch()
    with tempfile.TemporaryDirectory() as tmp:
        GOLDEN.write_text(canonical(post(mp, tmp, GOLDEN_PARAMS)))
    mp.undo()
