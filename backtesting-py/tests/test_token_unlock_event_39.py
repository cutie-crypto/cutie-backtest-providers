"""C39 hand-computed UTC opens; HTTP path, no network or market mirror."""
from pathlib import Path
import sys

import pandas as pd
from backtesting import Backtest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p
from strategy_token_unlock import TOOL_ID


def frame(tf='1h'):
    step = {'1h': 3600, '4h': 14400, '1d': 86400}.get(tf, 3600)
    return pd.DataFrame(dict(Open=100., High=101., Low=99., Close=100., Volume=1.),
                        index=pd.date_range('2026-01-01', '2026-01-18', freq=pd.Timedelta(seconds=step)))


def event(day=6, known='2026-01-03T00:00:00Z', pct='1'):
    return dict(ts_utc=f'2026-01-{day:02d}T00:00:00Z', label='token_unlock',
                pct_of_supply=pct, known_at_utc=known)


def post(monkeypatch, tmp_path, params=None, data=None, tf='1h', market='futures', symbol='BTCUSDT', invalid=False):
    data = frame(tf) if data is None else data
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', tmp_path)
    calls = []
    def fetch(*a, **k):
        calls.append(a)
        assert not invalid, 'must reject before fetch'
        return data.copy()
    def no_prefix(*a, **k):
        raise AssertionError('inline unlock template fetches no prefix')
    monkeypatch.setattr(p, '_fetch_ohlcv', fetch)
    monkeypatch.setattr(p, '_fetch_template_warmup', no_prefix)
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **k: None)
    req = dict(run_id='c39', provider_tool_id=TOOL_ID, provider_params={'events': [event()], **(params or {})},
               symbol=symbol, market=market, timeframe=tf, start_at=int(data.index[0].timestamp()),
               end_at=int(data.index[-1].timestamp()) + {'1h': 3600, '4h': 14400, '1d': 86400}.get(tf, 3600),
               initial_capital='10000', fee_bps='0', slippage_bps='0')
    body = TestClient(p.app).post('/cutie/backtest', json={'backtest': req}).json()
    if not invalid:
        assert body['result_status'] == 'success', body
        assert len(calls) == 1
    return body


def report(body):
    return body['raw_report']['token_unlock_event']


def test_short_long_exact_opens_and_settled_evidence(monkeypatch, tmp_path):
    for tf in ('1h', '4h', '1d'):
        body = post(monkeypatch, tmp_path, tf=tf)
        expected = [(1767398400, 1767657600, 'short'), (1767657600, 1767916800, 'long')]
        assert [(r['opened_at'], r['closed_at'], r['side']) for r in body['trades']] == expected
        assert all(r['entry_price'] == r['exit_price'] == '100' for r in body['trades'])
        legs = report(body)['events'][0]['legs']
        assert [r['exit_reason'] for r in legs] == ['unlock_day', 'post_days']
        assert [r['entry_price'] for r in legs] == [r['entry_price'] for r in body['trades']]
        assumptions = body['assumptions']['token_unlock_event']
        assert assumptions['schedule_notice'] == '解锁排期按上市日起视为已公开，改期事件按我方首次抓到时间'
        assert assumptions['history_notice'] == '回补前上游已改过的历史排期无法还原，按最新排期计'
        assert body['assumptions']['indicator_warmup_bars'] == 0
        assert not any('unlock' in k for r in body['trades'] for k in r)


def test_known_at_equal_entry_allowed_and_one_second_late_skips_entire_event(monkeypatch, tmp_path):
    equal = post(monkeypatch, tmp_path)
    assert len(equal['trades']) == 2
    late = post(monkeypatch, tmp_path, {'events': [event(known='2026-01-03T00:00:01Z')]})
    assert late['trades'] == []
    assert report(late)['events'][0]['status'] == 'skipped_not_known'


def test_threshold_below_skips_equal_trades(monkeypatch, tmp_path):
    for pct, count in [('0.99', 0), ('1', 2), ('1.01', 2)]:
        body = post(monkeypatch, tmp_path, {'events': [event(pct=pct)]})
        assert len(body['trades']) == count


def test_overlapping_later_event_skipped_boundary_allowed(monkeypatch, tmp_path):
    body = post(monkeypatch, tmp_path, {'events': [event(12), event(9), event(6)]})
    assert len(body['trades']) == 4
    assert [e['status'] for e in report(body)['events']] == ['entered', 'skipped_overlap', 'entered']
    assert report(body)['skipped_overlap'] == 1


def test_invalid_market_leverage_symbol_and_timeframe_reject_before_fetch(monkeypatch, tmp_path):
    for kwargs in [dict(market='spot'), dict(params={'leverage': 2}), dict(symbol='BTCUSDC'),
                   dict(symbol='BTC/USDT:BTC'), dict(params={'leverage': True}), dict(tf='4h', params={'take_profit_pct': 2})]:
        body = post(monkeypatch, tmp_path, invalid=True, **kwargs)
        assert body['error_type'] == 'INVALID_PARAMS', body
    assert post(monkeypatch, tmp_path, tf='15m', invalid=True)['error_type'] == 'TIMEFRAME_UNSUPPORTED'
    assert len(post(monkeypatch, tmp_path, symbol='BTC/USDT:USDT')['trades']) == 2


def test_invalid_event_shapes_and_parameter_bounds_before_fetch(monkeypatch, tmp_path):
    missing = event(); missing.pop('known_at_utc')
    for values in [{'events': []}, {'events': [missing]}, {'events': [event(pct='101')]},
                   {'events': [event(pct='-1')]}, {'events': [event(pct='NaN')]},
                   {'events': [event()] * 51}, {'events': [event(pct=1)]},
                   {'events': [event(), event()]}, {'pre_days': True}, {'post_days': 15},
                   {'pre_days': 0}, {'min_pct_of_supply': 0}, {'stop_loss_pct': 20.1},
                   {'events': [{**event(), 'ts_utc': '2026-01-06T01:00:00Z'}]}]:
        body = post(monkeypatch, tmp_path, values, invalid=True)
        assert body['error_type'] == 'INVALID_PARAMS', body


def test_fixed_stop_intrabar_then_next_open_and_long_frozen_separately(monkeypatch, tmp_path):
    data = frame()
    data.loc['2026-01-04T12:00:00', 'High'] = 104
    data.loc['2026-01-07T12:00:00', 'Low'] = 96
    body = post(monkeypatch, tmp_path, {'stop_loss_pct': 3}, data)
    assert [r['closed_at'] for r in body['trades']] == [1767531600, 1767790800]
    assert [l['exit_reason'] for l in report(body)['events'][0]['legs']] == ['stop_loss', 'stop_loss']


def test_position_sizing_both_legs_and_catalog(monkeypatch, tmp_path):
    body = post(monkeypatch, tmp_path, {'position_size_risk_pct': 1, 'stop_loss_pct': 5})
    assert len(body['trades']) == 2
    assert [r['qty'] for r in body['trades']] == ['20', '20']
    spec = p._catalog_tool(TOOL_ID, p.TOOL_SPECS[TOOL_ID], ['BTCUSDT'])
    assert spec['markets'] == ['futures'] and spec['timeframes'] == ['1h', '4h', '1d']
    assert spec['is_default'] is False
    props = spec['param_schema']['properties']
    assert props['leverage']['enum'] == [1] and 'take_profit_pct' not in props
