"""P6: raw_report.trade_exit_kinds -- one {"seq", "exit_kind"} per result.v2 trade, same seq order.

result.v2 trades keep their frozen 10 keys (server / connector compare the exact key set); the exit
reason lives only in raw_report. Every V2 runner is exercised on synthetic data, every kind is pinned
on the branch that produces it, and templates that already record their own exit reasons keep those
raw_report keys byte for byte.
"""
import json
import re
import sys
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from backtesting import Backtest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p
import scale_in_out_ledger as ledger_mod

import _10b2a_cases as c2a
import _10b2b_cases as c2b
import _10b2c_cases as c2c
import _10b2d_cases as c2d
import _pliq1_cases as pliq
import test_event0_event_window as ew
import test_p1_fear_greed_scale_in as fg
import test_p2_funding_settlement_reversal as funding
import test_q18_macro_events as macro
import test_s3_top_long_short_reversal as lsr
import test_s4_liquidation_reversal as liq
import test_short_pat4_candles as short_candles

PREFIX = 'local.backtesting_py.'
V2_KEYS = {'seq', 'opened_at', 'closed_at', 'side', 'qty', 'entry_price', 'exit_price', 'fee', 'slippage', 'pnl'}
# Runners whose trades are not result.v2: basket kernel_v3 emits result.v3 (trades carry their own
# exit_kind), portfolio rotation emits v4 fills. Out of P6 scope; pinned so a new runner is noticed.
NON_V2_RUNNERS = {'kernel_v3', p.ROTATION_RUNNER}


def generic_frame(n=900, freq='1h'):
    """Deterministic two-wave walk: enough crosses, breakouts, dips and spikes for every indicator."""
    index = pd.date_range('2024-01-01', periods=n, freq=freq, tz='UTC')
    t = np.arange(n)
    rng = np.random.default_rng(7)
    close = 100 + 15 * np.sin(t / 25) + 6 * np.sin(t / 7) + rng.normal(0, 0.8, n).cumsum() * 0.3
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + rng.uniform(0.1, 1.5, n)
    low = np.minimum(open_, close) - rng.uniform(0.1, 1.5, n)
    volume = rng.uniform(100, 1000, n) * (1 + (t % 50 == 0) * 5)
    return pd.DataFrame(dict(Open=open_, High=high, Low=low, Close=close, Volume=volume), index=index)


def post(monkeypatch, tmp_path, name, params=None, data=None, market='spot', timeframe='1h', capital='10000',
         warmup=0):
    data = generic_frame() if data is None else data
    history, data = data.iloc[:warmup], data.iloc[warmup:]
    step = int((data.index[1] - data.index[0]).total_seconds())
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a, **k: data.copy())
    monkeypatch.setattr(p, '_fetch_template_warmup', lambda *a, **k: history.copy())
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **k: None)
    request = dict(run_id='p6', provider_tool_id=PREFIX + name, provider_params=dict(params or {}),
                   symbol='BTCUSDT', market=market, timeframe=timeframe,
                   start_at=int(data.index[0].timestamp()), end_at=int(data.index[-1].timestamp()) + step,
                   initial_capital=capital, fee_bps='0', slippage_bps='0')
    return TestClient(p.app).post('/cutie/backtest', json={'backtest': request}).json()


def generic(params=None, warmup=0):
    def run(mp, tmp):
        markets = p.TOOL_SPECS[PREFIX + CURRENT[0]].get('markets') or ['spot']
        return post(mp, tmp, CURRENT[0], params, market='spot' if 'spot' in markets else 'futures', warmup=warmup)
    return run


def via(module, name=None, **kw):
    return lambda mp, tmp: module.post(mp, tmp, name or CURRENT[0], **kw)


def fear_greed_run(mp, tmp):
    mp.setattr(p, 'REPORTS_DIR', tmp / 'reports')
    fg._install(mp, days=10, fg={1: 10, 2: 20, 3: 5, 5: 80, 7: 15})
    return fg._post(TestClient(p.app), days=10, params={**fg.BASE, 'max_lots': 2})


CURRENT = ['']
GENERIC_LEDGER = {
    'grid': dict(lower_price=85, upper_price=115, grid_count=10, amount_per_grid=200),
    'dca': dict(amount=100, profit_target_pct=5),
    'rsi_scale_in_out': dict(buy_notional=200, sell_notional=200),
}
SPECIAL = {
    **{name: via(c2d) for name in c2d.NAMES},
    **{name: via(c2c) for name in ('morning_star', 'three_white_soldiers', 'bullish_doji_reversal')},
    'double_top': via(c2b),
    'fibonacci_retracement': via(c2a),
    **{name: (lambda mp, tmp, name=name: short_candles.post(mp, tmp, name)[0])
       for name in ('evening_star', 'three_black_crows', 'bearish_doji_reversal')},
    **{name: (lambda mp, tmp, name=name: macro.post(mp, tmp, name)) for name in macro.KINDS},
    'event_window': lambda mp, tmp: ew.post(mp, tmp, ew.GOLDEN_PARAMS),
    'funding_settlement_reversal': lambda mp, tmp: funding.run(mp, tmp, {0: '0.00060000'})[0],
    'top_long_short_reversal': lambda mp, tmp: lsr.run_daily(mp, tmp, {1: '0.65', 3: '1.10', 4: '0.50', 5: '1.80'}),
    'liquidation_reversal': lambda mp, tmp: liq.run(mp, tmp, dict(liq.SPIKE), rows=liq.ROWS)[0],
    'fear_greed_scale_in': fear_greed_run,
    # The scale-in/out templates refuse to run without at least one warmup candle.
    **{name: generic(params, warmup=100) for name, params in GENERIC_LEDGER.items()},
}
V2_TOOLS = sorted(tool.removeprefix(PREFIX) for tool, spec in p.TOOL_SPECS.items()
                  if spec.get('runner') not in NON_V2_RUNNERS)


def kinds_of(body):
    return [row['exit_kind'] for row in body['raw_report']['trade_exit_kinds']]


def assert_aligned(body):
    assert body['result_status'] == 'success', body.get('error_message')
    trades = body['trades']
    rows = body['raw_report']['trade_exit_kinds']
    assert len(rows) == len(trades)
    assert [row['seq'] for row in rows] == [trade['seq'] for trade in trades] == list(range(1, len(trades) + 1))
    assert all(set(row) == {'seq', 'exit_kind'} for row in rows)
    assert set(kinds_of(body)) <= p.TRADE_EXIT_KINDS
    assert all(set(trade) == V2_KEYS for trade in trades)


def test_catalog_partition_is_explicit():
    excluded = sorted(tool for tool, spec in p.TOOL_SPECS.items() if spec.get('runner') in NON_V2_RUNNERS)
    assert excluded == sorted(PREFIX + name for name in (
        'basket_ratio_sma_cross', 'basket_ratio_roc', 'basket_ratio_zscore', 'portfolio_rotation'))
    assert set(SPECIAL) <= set(V2_TOOLS)


@pytest.mark.parametrize('name', V2_TOOLS)
def test_every_v2_tool_reports_one_kind_per_trade(monkeypatch, tmp_path, name):
    CURRENT[0] = name
    body = SPECIAL.get(name, generic())(monkeypatch, tmp_path)
    assert body['schema_version'] == p.RESULT_V2_SCHEMA
    assert body['trades'], f'{name}: the synthetic run must close at least one trade'
    assert_aligned(body)
    # Recording the kinds changes nothing else: same body byte for byte without the wrapper.
    monkeypatch.setattr(p, '_exit_kind_strategy', lambda strategy: strategy)
    plain = SPECIAL.get(name, generic())(monkeypatch, tmp_path)
    assert canonical(without_kinds(body)) == canonical(without_kinds(plain))


def without_kinds(body):
    return dict(body, raw_report={k: v for k, v in body['raw_report'].items() if k != 'trade_exit_kinds'})


# ---------------------------------------------------------------- one directed case per kind

def bar_index(data, ts):
    return int(np.searchsorted(data.index.asi8 // 10**9, ts))


def decided(data, trade):
    """Decision bar of an exit: the close is queued at that bar and fills at the next open."""
    return bar_index(data, trade['closed_at']) - 1


def by_kind(body, kind):
    return [trade for trade, k in zip(body['trades'], kinds_of(body)) if k == kind]


def ema(params=None, market='spot'):
    return dict(ema_fast=5, ema_slow=20, **(params or {}))


def test_stop_loss_risk_layer_intrabar(monkeypatch, tmp_path):
    data = generic_frame()
    body = post(monkeypatch, tmp_path, 'ema_cross', ema(dict(risk_layer_enabled=True, stop_loss_pct=1)), data)
    assert_aligned(body)
    stops = by_kind(body, 'stop_loss')
    assert stops
    intrabar = 0
    for trade in body['trades']:
        bar = decided(data, trade)
        stop = Decimal(trade['entry_price']) * Decimal('0.99')
        touched = Decimal(str(data.Low.iloc[bar])) <= stop
        # Stop wins every bar whose Low touched it: those exits and only those are stop_loss.
        assert (trade in stops) == touched, trade
        intrabar += touched and Decimal(str(data.Close.iloc[bar])) > stop
    assert intrabar, 'at least one stop must fire on the Low while the bar closed above it'


def test_stop_loss_legacy_close_only(monkeypatch, tmp_path):
    data = generic_frame()
    body = post(monkeypatch, tmp_path, 'ema_cross', ema(dict(risk_layer_enabled=False, stop_loss_pct=1)), data)
    assert_aligned(body)
    stops = by_kind(body, 'stop_loss')
    assert stops
    for trade in body['trades']:
        bar = decided(data, trade)
        closed_below = Decimal(str(data.Close.iloc[bar])) <= Decimal(trade['entry_price']) * Decimal('0.99')
        assert (trade in stops) == closed_below, trade


def test_take_profit_legacy_and_risk_layer(monkeypatch, tmp_path):
    data = generic_frame()
    legacy = post(monkeypatch, tmp_path, 'ema_cross', ema(dict(risk_layer_enabled=False, take_profit_pct=2)), data)
    assert_aligned(legacy)
    takes = by_kind(legacy, 'take_profit')
    assert takes
    for trade in legacy['trades']:
        bar = decided(data, trade)
        hit = Decimal(str(data.Close.iloc[bar])) >= Decimal(trade['entry_price']) * Decimal('1.02')
        assert (trade in takes) == hit, trade
    layer = post(monkeypatch, tmp_path, 'ema_cross', ema(dict(risk_layer_enabled=True, stop_loss_pct=3,
                                                              take_profit_r=1)), data)
    assert_aligned(layer)
    assert 'take_profit' in kinds_of(layer)


def test_trailing_stop(monkeypatch, tmp_path):
    body = post(monkeypatch, tmp_path, 'ema_cross', ema(dict(risk_layer_enabled=True, trailing_stop_pct=1)))
    assert_aligned(body)
    kinds = kinds_of(body)
    assert 'trailing_stop' in kinds
    assert 'stop_loss' not in kinds  # Trailing is the only stop source here.


def test_trailing_with_fixed_stop_keeps_unmoved_stop_as_stop_loss(monkeypatch, tmp_path):
    body = post(monkeypatch, tmp_path, 'ema_cross', ema(dict(risk_layer_enabled=True, stop_loss_pct=0.5,
                                                             trailing_stop_pct=3)))
    assert_aligned(body)
    assert 'stop_loss' in kinds_of(body)


def test_time_exit_max_holding_bars(monkeypatch, tmp_path):
    data = generic_frame()
    body = post(monkeypatch, tmp_path, 'ema_cross', ema(dict(risk_layer_enabled=True, max_holding_bars=3)), data)
    assert_aligned(body)
    expired = by_kind(body, 'time_exit')
    assert expired
    for trade in expired:
        assert bar_index(data, trade['closed_at']) - bar_index(data, trade['opened_at']) == 3


def test_time_exit_hold_hours_template(monkeypatch, tmp_path):
    body, _ = funding.run(monkeypatch, tmp_path, {0: '0.00060000'})
    assert_aligned(body)
    assert kinds_of(body) == ['time_exit']


def test_signal_exit(monkeypatch, tmp_path):
    data = generic_frame()
    body = post(monkeypatch, tmp_path, 'ema_cross', ema(dict(risk_layer_enabled=False)), data)
    assert_aligned(body)
    kinds = kinds_of(body)
    assert kinds[:-1] and set(kinds[:-1]) == {'signal_exit'}


def test_end_of_data(monkeypatch, tmp_path):
    data = generic_frame()
    body = post(monkeypatch, tmp_path, 'rsi_reversal', dict(risk_layer_enabled=False), data)
    assert_aligned(body)
    kinds = kinds_of(body)
    assert kinds[-1] == 'end_of_data' and 'end_of_data' not in kinds[:-1]
    assert bar_index(data, body['trades'][-1]['closed_at']) == len(data) - 1


@pytest.mark.parametrize('scenario', ['B2', 'G'])
def test_liquidation_isolated_futures(monkeypatch, tmp_path, scenario):
    # P-LIQ1 geometry: 10x red_streak_rsi, user stop beyond the liquidation price, crash bar k.
    data, _ = pliq.scenario_frame('red_streak_rsi', scenario)
    params = dict(pliq.scenario_params('red_streak_rsi', scenario, 'full'), risk_layer_enabled=False)
    body = pliq.post(monkeypatch, tmp_path, 'red_streak_rsi', params, data, 'futures')
    assert_aligned(body)
    liquidated = {row['seq'] for row in body['raw_report']['isolated_risk']['liquidations']}
    assert liquidated
    for trade, kind in zip(body['trades'], kinds_of(body)):
        assert (kind == 'liquidation') == (trade['seq'] in liquidated), trade


def test_ledger_kinds_grid_stop_dca_target_and_end_of_data(monkeypatch, tmp_path):
    CURRENT[0] = 'grid'
    grid = post(monkeypatch, tmp_path, 'grid', dict(GENERIC_LEDGER['grid'], lower_price=95,
                                                    below_lower_action='stop_loss'), warmup=100)
    assert_aligned(grid)
    assert grid['assumptions']['stop_loss_triggered'] >= 1
    assert 'stop_loss' in kinds_of(grid) and 'signal_exit' in kinds_of(grid)
    dca = post(monkeypatch, tmp_path, 'dca', GENERIC_LEDGER['dca'], warmup=100)
    assert_aligned(dca)
    assert 'take_profit' in kinds_of(dca) and kinds_of(dca)[-1] == 'end_of_data'


def test_ledger_kind_order_follows_result_v2_sort():
    trades = [dict(opened_at=1, closed_at=9), dict(opened_at=2, closed_at=5), dict(opened_at=0, closed_at=5)]
    assert ledger_mod.result_v2_trade_exit_kinds(trades, ['a', 'b', 'c']) == ['c', 'b', 'a']
    assert ledger_mod.result_v2_trade_exit_kinds(trades, ['a'])[0].startswith('unresolved')


# ---------------------------------------------------------------- partial closes

def test_take_profit_levels_each_partial_fill_is_take_profit(monkeypatch, tmp_path):
    """tp1/tp2 partial closes: one queued close per decision bar -> one result.v2 row each.

    Levels reached on the same bar are summed by level_exit into a single close, so they share one row.
    The remainder closes on whatever ends it (stop, signal or the window end).
    """
    data = generic_frame()
    body = post(monkeypatch, tmp_path, 'ema_cross', ema(dict(
        risk_layer_enabled=True, stop_loss_pct=2, trailing_stop_pct=3, tp1_r=0.5, tp1_close_pct=30, tp2_r=1,
        tp2_close_pct=30)), data)
    assert_aligned(body)
    groups = {}
    for trade, kind in zip(body['trades'], kinds_of(body)):
        groups.setdefault(trade['opened_at'], []).append(kind)
    split = [kinds for kinds in groups.values() if len(kinds) > 1]
    assert split, 'some position must have been closed in parts'
    for kinds in split:
        assert set(kinds[:-1]) == {'take_profit'}, kinds
        assert kinds[-1] in p.TRADE_EXIT_KINDS


def test_unnamed_or_unmapped_exit_omits_the_field():
    trades = [{'seq': 1}, {'seq': 2}]
    assert p._trade_exit_kinds_report(trades, ['signal_exit', None]) == {}
    assert p._trade_exit_kinds_report(trades, ['signal_exit', 'unmapped:x']) == {}
    assert p._trade_exit_kinds_report(trades, ['signal_exit']) == {}
    assert p._trade_exit_kinds_report(trades, ['signal_exit', 'end_of_data']) == {'trade_exit_kinds': [
        {'seq': 1, 'exit_kind': 'signal_exit'}, {'seq': 2, 'exit_kind': 'end_of_data'}]}


# ---------------------------------------------------------------- self-recorded reasons stay intact

def canonical(value):
    # fetched_at_ms is the wall clock of the external-series fetch (P5 / S4), not run output.
    text = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return re.sub(r'"fetched_at_ms":\d+', '"fetched_at_ms":0', text)


SELF_RECORDED = {
    'turtle': (lambda mp, tmp: post(mp, tmp, 'turtle'), 'turtle_groups'),
    'calendar_schedule': (lambda mp, tmp: c2d.post(mp, tmp, 'calendar_schedule'), 'calendar_events'),
    'event_window': (lambda mp, tmp: ew.post(mp, tmp, ew.GOLDEN_PARAMS), None),
    'opening_range_breakout': (lambda mp, tmp: c2d.post(mp, tmp, 'opening_range_breakout'), 'range_breakout_days'),
}


@pytest.mark.parametrize('name', sorted(SELF_RECORDED))
def test_self_recorded_reports_unchanged_byte_for_byte(monkeypatch, tmp_path, name):
    run, key = SELF_RECORDED[name]
    body = run(monkeypatch, tmp_path)
    assert_aligned(body)
    monkeypatch.setattr(p, '_exit_kind_strategy', lambda strategy: strategy)
    before = run(monkeypatch, tmp_path)
    assert 'trade_exit_kinds' not in before['raw_report']  # Unwrapped closes carry no kind.
    assert canonical(without_kinds(body)) == canonical(before)
    if key is not None:
        assert canonical(body['raw_report'][key]) == canonical(before['raw_report'][key])
