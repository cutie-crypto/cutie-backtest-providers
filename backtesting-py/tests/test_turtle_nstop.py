"""TURTLE-NSTOP: turtle_groups 每组的 n（开组 N）与 stop（最后一腿成交后的组止损）。

期望值全部来自 test_turtle_template 里独立的标量手算账户（Wilder N、逐腿成交价），
不读 provider 策略实例状态。
"""
from decimal import Decimal

import pytest
from backtesting import Backtest
from fastapi.testclient import TestClient

import cutie_backtesting_provider as p
from test_turtle_template import (directional_fixture, directional_hand, expected_groups,
                                  fixture, frame, hand, request)


def http_groups(monkeypatch, tmp_path, fx, extra):
    data = frame(fx)
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a: data.copy())
    monkeypatch.setattr(p, '_fetch_template_warmup', lambda *a: data.iloc[:0].copy())
    monkeypatch.setattr(p, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **k: None)
    body = request({**fx['params'], **extra})
    body['backtest'].update(market='futures', start_at=int(data.index[0].timestamp()),
                            end_at=int(data.index[-1].timestamp()) + 3600)
    result = TestClient(p.app).post('/cutie/backtest', json=body).json()
    assert result['result_status'] == 'success', result
    return result['raw_report']['turtle_groups']


def scenario(direction):
    fx = fixture() if direction == 'long' else directional_fixture(direction)
    expected = hand(fx) if direction == 'long' else directional_hand(fx)
    return fx, expected


def hand_n_stop(fx, expected):
    """每组 (n, stop, 腿数)：n 取手算开组 N；stop = 该组最后一腿成交价 - side * 倍数 * n。"""
    mult = Decimal(str(fx['params']['stop_atr_multiplier']))
    out = {}
    for group in expected['groups']:
        legs = [t for t in expected['trades'] if t['group_id'] == group['group_id']]
        side = 1 if legs[-1]['size'] > 0 else -1
        n = Decimal(str(group['n']))
        out[group['group_id']] = (n, Decimal(str(legs[-1]['entry_price'])) - side * mult * n, len(legs))
    return out


def close(actual, wanted):
    return abs(Decimal(actual) - wanted) <= Decimal('1e-9')


@pytest.mark.parametrize('direction', ['long', 'short', 'both'])
@pytest.mark.parametrize('extra', [{'risk_layer_enabled': True},
                                   {'time_layer_enabled': True, 'time_session_start': '00:00',
                                    'time_session_end': '23:59'}])
def test_group_n_and_stop_follow_last_fill(monkeypatch, tmp_path, direction, extra):
    fx, expected = scenario(direction)
    groups = http_groups(monkeypatch, tmp_path, fx, extra)
    wanted = hand_n_stop(fx, expected)
    assert [g['group_id'] for g in groups] == list(wanted)
    assert [g['units'] for g in groups] == [wanted[g['group_id']][2] for g in groups]
    assert max(g['units'] for g in groups) == 4 and min(g['units'] for g in groups) == 1
    for group in groups:
        n, stop, _ = wanted[group['group_id']]
        assert list(group)[-2:] == ['n', 'stop'] and set(group) >= {'exit_reason', 'n', 'stop'}
        assert close(group['n'], n), (group, n)
        assert close(group['stop'], stop), (group, stop)
    # 多组互不串：每组 (n, stop) 两两不同。
    assert len({(g['n'], g['stop']) for g in groups}) == len(groups)
    # 加仓组的止损跟最后一腿走，不等于首单价 ∓ 倍数 × n。
    mult = Decimal(str(fx['params']['stop_atr_multiplier']))
    first_leg = next(t for t in expected['trades'] if t['group_id'] == 'turtle-1')
    side = 1 if first_leg['size'] > 0 else -1
    first_stop = Decimal(str(first_leg['entry_price'])) - side * mult * wanted['turtle-1'][0]
    assert not close(groups[0]['stop'], first_stop)


@pytest.mark.parametrize('direction', ['long', 'short'])
def test_single_unit_group_stop_is_first_fill(monkeypatch, tmp_path, direction):
    fx, expected = scenario(direction)
    groups = http_groups(monkeypatch, tmp_path, fx, {'risk_layer_enabled': True})
    mult = Decimal(str(fx['params']['stop_atr_multiplier']))
    singles = [g for g in groups if g['units'] == 1]
    assert [g['group_id'] for g in singles] == ['turtle-2', 'turtle-3']
    for group in singles:
        leg = next(t for t in expected['trades'] if t['group_id'] == group['group_id'])
        side = 1 if leg['size'] > 0 else -1
        n = Decimal(str(next(g['n'] for g in expected['groups'] if g['group_id'] == group['group_id'])))
        assert close(group['n'], n)
        assert close(group['stop'], Decimal(str(leg['entry_price'])) - side * mult * n)


@pytest.mark.parametrize('direction', ['long', 'short', 'both'])
def test_without_risk_or_time_layer_groups_carry_neither_key(monkeypatch, tmp_path, direction):
    fx, expected = scenario(direction)
    groups = http_groups(monkeypatch, tmp_path, fx, {})
    assert groups == expected_groups(expected)
    assert all(set(g) == {'group_id', 'trade_seqs', 'units'} for g in groups)


def test_builder_maps_n_stop_by_group_id_and_omits_when_absent():
    fx = fixture()
    strategy = p.TOOL_SPECS['local.backtesting_py.turtle']['build'](fx['params'])['strategy']
    stats = Backtest(frame(fx), strategy, cash=fx['cash'], exclusive_orders=False, finalize_trades=False).run()
    trades = stats['_trades'].sort_values(['ExitBar', 'EntryBar'])
    v2 = p._build_result_v2_trades(trades, Decimal(1), Decimal(0), Decimal(0))
    n_stop = {'turtle-1': (1.5, 90.25), 'turtle-2': (2.5, 80.0), 'turtle-3': (0.125, 70.0)}
    groups = p._build_turtle_groups(trades, v2, {g: 'channel' for g in n_stop}, n_stop)
    assert [(g['group_id'], g['n'], g['stop']) for g in groups] == [
        ('turtle-1', '1.5', '90.25'), ('turtle-2', '2.5', '80'), ('turtle-3', '0.125', '70')]
    plain = p._build_turtle_groups(trades, v2, {g: 'channel' for g in n_stop}, None)
    assert all(set(g) == {'group_id', 'trade_seqs', 'units', 'exit_reason'} for g in plain)
    with pytest.raises(KeyError):
        p._build_turtle_groups(trades, v2, None, {'turtle-1': (1.0, 2.0)})
