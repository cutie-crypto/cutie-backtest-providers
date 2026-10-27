"""PIN7-E: period-end settlement fills at the last bar's Close, and the last bar makes no exit decision.

backtesting.py's finalize_trades closes leftover trades and re-runs broker.next on the last bar's own
OHLC, so a market close used to fill at Open[-1]; a stop / take profit judged on the last bar was
therefore settled at the open and the close-based verdict was lost. The exit kind stays end_of_data.
Every case runs on synthetic bars.
"""
import sys
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from backtesting import Backtest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p

import _pliq1_cases as pliq
import test_p6_trade_exit_kinds as p6
import test_f3_us_open as f3
import test_short_pat4_candles as s4

PREFIX = p6.PREFIX
N = 60
SIGNAL = 40  # bar whose close breaks the channel; the entry fills at the open of bar SIGNAL + 1
LAST = N - 1


def frame(direction, last_close):
    """Flat base, a channel break at SIGNAL, a flat plateau, and a final bar closing at ``last_close``.

    The final bar opens at the plateau, so Open[-1] and Close[-1] differ and the settlement price tells
    which of the two was used. Its High / Low always cover the close."""
    up = direction == 'long'
    base, plateau = 100.0, 110.0 if up else 90.0
    close = np.full(N, base)
    close[SIGNAL:] = plateau
    close[LAST] = last_close
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + 0.1
    low = np.minimum(open_, close) - 0.1
    index = pd.date_range('2024-01-01', periods=N, freq='1h', tz='UTC')
    return pd.DataFrame(dict(Open=open_, High=high, Low=low, Close=close, Volume=np.full(N, 500.0)), index=index)


def run(monkeypatch, tmp_path, direction, layer, data, **risk):
    params = dict(lookback=5, exit_lookback=5, direction=direction, risk_layer_enabled=layer, **risk)
    return p6.post(monkeypatch, tmp_path, 'breakout', params, data, market='futures')


def only_trade(body):
    assert body['result_status'] == 'success', body.get('error_message')
    assert len(body['trades']) == 1, body['trades']
    assert p6.kinds_of(body) == ['end_of_data']
    return body['trades'][0]


# (direction, risk-layer?, risk params, last close that satisfies them)
TAKE_PROFIT = [
    ('long', True, dict(stop_loss_pct=5, take_profit_r=1), 125.0),
    ('short', True, dict(stop_loss_pct=5, take_profit_r=1), 70.0),
    ('long', False, dict(stop_loss_pct=5, take_profit_pct=5), 125.0),
    ('short', False, dict(stop_loss_pct=5, take_profit_pct=5), 70.0),
]
STOP = [
    ('long', True, dict(stop_loss_pct=5, take_profit_r=1), 90.0),
    ('short', True, dict(stop_loss_pct=5, take_profit_r=1), 112.0),
    ('long', False, dict(stop_loss_pct=5, take_profit_pct=5), 90.0),
    ('short', False, dict(stop_loss_pct=5, take_profit_pct=5), 112.0),
]


def settled(monkeypatch, tmp_path, direction, layer, risk, last_close):
    data = frame(direction, last_close)
    trade = only_trade(run(monkeypatch, tmp_path, direction, layer, data, **risk))
    assert Decimal(trade['entry_price']) == Decimal(str(data.Open.iloc[SIGNAL + 1])), trade
    assert Decimal(str(data.Open.iloc[LAST])) != Decimal(str(last_close))
    return Decimal(trade['exit_price']), Decimal(str(data.Close.iloc[LAST]))


@pytest.mark.parametrize('direction,layer,risk,last_close', TAKE_PROFIT)
def test_last_bar_take_profit_settles_at_close_as_end_of_data(monkeypatch, tmp_path, direction, layer, risk,
                                                              last_close):
    exit_price, close = settled(monkeypatch, tmp_path, direction, layer, risk, last_close)
    assert exit_price == close


@pytest.mark.parametrize('direction,layer,risk,last_close', STOP)
def test_last_bar_stop_settles_at_close_as_end_of_data(monkeypatch, tmp_path, direction, layer, risk, last_close):
    exit_price, close = settled(monkeypatch, tmp_path, direction, layer, risk, last_close)
    assert exit_price == close


@pytest.mark.parametrize('direction,layer,risk,last_close', TAKE_PROFIT + STOP)
def test_the_bar_before_the_last_still_exits_normally(monkeypatch, tmp_path, direction, layer, risk, last_close):
    # The same close one bar earlier is a regular stop / take profit: queued at bar LAST - 1, filled at the
    # open of the next bar, kind taken from the trigger, not end_of_data.
    data = frame(direction, last_close)
    before = LAST - 1
    open_ = data.Open.iloc[before]
    data.iloc[before, data.columns.get_loc('Close')] = last_close
    data.iloc[before, data.columns.get_loc('High')] = max(last_close, open_) + 0.1
    data.iloc[before, data.columns.get_loc('Low')] = min(last_close, open_) - 0.1
    data.iloc[LAST, data.columns.get_loc('Open')] = last_close
    body = run(monkeypatch, tmp_path, direction, layer, data, **risk)
    assert body['result_status'] == 'success', body.get('error_message')
    assert p6.kinds_of(body)[0] in {'stop_loss', 'take_profit'}, p6.kinds_of(body)
    if layer:
        kind = p6.kinds_of(body)[0]
        offset = Decimal('0.05') * (1 if direction == 'long' else -1)
        multiplier = 1 + offset if kind == 'take_profit' else 1 - offset
        expected = Decimal(body['trades'][0]['entry_price']) * multiplier
        assert body['trades'][0]['closed_at'] == int(data.index[LAST-1].timestamp())
    else:
        expected = Decimal(str(data.Open.iloc[LAST]))
    assert Decimal(body['trades'][0]['exit_price']) == expected


def test_last_bar_isolated_liquidation_not_vetoed_by_skipped_stop(monkeypatch, tmp_path):
    # Codex P1: entry 110, 10x isolated, 20% position (margin 2000 of 10000), 5% stop at 104.5; the last
    # bar drops 110 -> 90 through the 99 liquidation price without a gap. The last bar skips the stop, so
    # the stop cannot claim it is crossed first and veto liquidation: liquidation arbitrates as usual.
    data = frame('long', 90.0)
    body = run(monkeypatch, tmp_path, 'long', True, data, stop_loss_pct=5, take_profit_r=1, leverage=10,
               position_size_pct=20)
    assert body['result_status'] == 'success', body.get('error_message')
    (trade,) = body['trades']
    assert Decimal(trade['entry_price']) == Decimal('110')
    report = body['raw_report']['isolated_risk']
    assert report['liquidation_count'] == 1, report
    margin = Decimal('2000')
    assert Decimal(report['liquidated_margin_total']) <= margin, report
    assert -Decimal(trade['pnl']) <= margin, trade
    assert Decimal(body['metrics']['total_return']) >= -margin / Decimal(body['initial_capital']), body['metrics']
    # On the last bar the stop counts as absent: the run equals the same run with no stop configured.
    bare = run(monkeypatch, tmp_path, 'long', True, data, leverage=10, position_size_pct=20)
    assert body['trades'] == bare['trades']
    assert report == bare['raw_report']['isolated_risk']
    assert p6.kinds_of(body) == p6.kinds_of(bare)


def count_position_closes(monkeypatch):
    """Template-queued exits go through Position.close; finalize_trades uses Trade.close, so it is not counted."""
    from backtesting.backtesting import Position
    calls, close = [], Position.close

    def recording(self, portion=1.0):
        calls.append(portion)
        return close(self, portion)

    monkeypatch.setattr(Position, 'close', recording)
    return calls


def trigger_one_bar_earlier(direction, last_close):
    data = frame(direction, last_close)
    before = LAST - 1
    data.iloc[before, data.columns.get_loc('Close')] = last_close
    data.iloc[before, data.columns.get_loc('High')] = max(last_close, data.Open.iloc[before]) + 0.1
    data.iloc[before, data.columns.get_loc('Low')] = min(last_close, data.Open.iloc[before]) - 0.1
    return data


@pytest.mark.parametrize('direction,layer,risk,last_close', TAKE_PROFIT)
def test_last_bar_queues_no_take_profit_order(monkeypatch, tmp_path, direction, layer, risk, last_close):
    # No template signal fires on these bars, so any Position.close here is a risk-layer verdict.
    calls = count_position_closes(monkeypatch)
    only_trade(run(monkeypatch, tmp_path, direction, layer, frame(direction, last_close), **risk))
    assert calls == []
    body = run(monkeypatch, tmp_path, direction, layer, trigger_one_bar_earlier(direction, last_close), **risk)
    if layer:
        assert calls == []
        assert p6.kinds_of(body) == ['take_profit']
    else:
        assert calls, 'legacy control still queues Position.close'


@pytest.mark.parametrize('direction,layer,risk,last_close', STOP)
def test_last_bar_names_no_stop_exit(monkeypatch, tmp_path, direction, layer, risk, last_close):
    # The breakout template's own channel exit may still fire on a falling last bar; the stop verdict may not.
    names = []
    mark_exit_kind = p._mark_exit_kind
    monkeypatch.setattr(p, '_mark_exit_kind', lambda strategy, kind: (names.append(kind), mark_exit_kind(strategy, kind)))
    only_trade(run(monkeypatch, tmp_path, direction, layer, frame(direction, last_close), **risk))
    assert names == []
    body = run(monkeypatch, tmp_path, direction, layer, trigger_one_bar_earlier(direction, last_close), **risk)
    if layer:
        assert names == []
        assert p6.kinds_of(body) == ['stop_loss']
    else:
        assert names == ['stop_loss']


# ---------------------------------------------------------------- every engine-path v2 tool

# condition_kernel 走 Decimal v2 内核（strategy_kernel_v2），不经过 backtesting.py 的 Backtest.run，
# 所以 record_runs 抓不到 frame，无法从这里取到 Close[-1] 作对照；其末端收盘结算不变量已在 P6 与
# test_condition_kernel_tool 覆盖。
NON_ENGINE_RUNNERS = {p.SCALE_IN_OUT_RUNNER, p.TURTLE_RUNNER, 'kernel_v3', p.ROTATION_RUNNER,
                      p.CONDITION_KERNEL_RUNNER}
ENGINE_TOOLS = sorted(tool.removeprefix(PREFIX) for tool, spec in p.TOOL_SPECS.items()
                      if spec.get('runner') not in NON_ENGINE_RUNNERS)


def test_engine_tool_enumeration_is_runtime_derived():
    assert set(ENGINE_TOOLS) <= set(p6.V2_TOOLS)
    assert 'breakout' in ENGINE_TOOLS and 'turtle' not in ENGINE_TOOLS and 'grid' not in ENGINE_TOOLS
    assert not set(ENGINE_TOOLS) & set(p6.GENERIC_LEDGER)


def record_runs(monkeypatch):
    """Record every Backtest.run input frame so the expected Close[-1] comes from the run itself."""
    frames, run_backtest = [], Backtest.run

    def recording(self, **kwargs):
        frames.append(self._data.copy())
        return run_backtest(self, **kwargs)

    monkeypatch.setattr(Backtest, 'run', recording)
    return frames


def final_settlements(monkeypatch, tmp_path, frames, name):
    """Run ``name``'s synthetic p6 case; return its end_of_data trades after checking they fill at Close[-1]."""
    del frames[:]
    p6.CURRENT[0] = name
    body = p6.SPECIAL.get(name, p6.generic())(monkeypatch, tmp_path)
    assert body['result_status'] == 'success', body.get('error_message')
    assert frames, f'{name}: no engine run'
    close = Decimal(str(frames[-1].Close.iloc[-1]))
    finals = [trade for trade, kind in zip(body['trades'], p6.kinds_of(body)) if kind == 'end_of_data']
    # Every period-end settlement is priced at the last Close; tools whose synthetic run ends flat have none.
    assert all(Decimal(trade['exit_price']) == close for trade in finals), (name, close, finals)
    return finals


@pytest.mark.parametrize('name', ENGINE_TOOLS)
def test_finalize_exit_price_is_last_close_for_all_v2_engine_tools(monkeypatch, tmp_path, name):
    final_settlements(monkeypatch, tmp_path, record_runs(monkeypatch), name)


def test_synthetic_runs_cover_end_of_data_for_several_engine_tools(monkeypatch, tmp_path):
    # Guards the loop above against passing vacuously once every tool ended its synthetic run flat.
    # Self-contained: it runs the cases it needs instead of relying on the parametrized ones running first.
    frames, seen = record_runs(monkeypatch), []
    for name in ENGINE_TOOLS:
        reports = tmp_path / name
        reports.mkdir()
        if final_settlements(monkeypatch, reports, frames, name):
            seen.append(name)
            if len(seen) >= 3:
                break
    assert len(seen) >= 3, seen


# ---------------------------------------------------------------- liquidation is not a period-end close

def test_liquidation_close_price_unchanged(monkeypatch, tmp_path):
    # P-LIQ1 geometry (10x isolated red_streak_rsi, crash bar k): the broker-phase liquidation keeps its
    # kind and price whether or not the period-end re-pricing is installed.
    data, _ = pliq.scenario_frame('red_streak_rsi', 'B2')
    params = dict(pliq.scenario_params('red_streak_rsi', 'B2', 'full'), risk_layer_enabled=False)

    def go():
        return pliq.post(monkeypatch, tmp_path, 'red_streak_rsi', params, data, 'futures')

    with_pin7e = go()
    monkeypatch.setattr(p, '_final_close_strategy', lambda strategy: strategy)
    before = go()
    kinds = p6.kinds_of(with_pin7e)
    assert 'liquidation' in kinds
    assert kinds == p6.kinds_of(before)
    liquidated = [trade for trade, kind in zip(with_pin7e['trades'], kinds) if kind == 'liquidation']
    assert liquidated == [trade for trade, kind in zip(before['trades'], p6.kinds_of(before))
                          if kind == 'liquidation']


# ---------------------------------------------------------------- PIN7-E b2a

def test_last_bar_liquidation_exit_kind_is_liquidation(monkeypatch, tmp_path):
    # Statement 146: a last-bar liquidation keeps the liquidation kind; finalize_trades' own close of the
    # same trade must not relabel it end_of_data (server S-1 reads end_of_data as a period-end close).
    # Same numbers as the b1 case: entry 110, 10x, 20% position, 5% stop, last bar 110 -> 90.
    data = frame('long', 90.0)
    body = run(monkeypatch, tmp_path, 'long', True, data, stop_loss_pct=5, take_profit_r=1, leverage=10,
               position_size_pct=20)
    assert body['result_status'] == 'success', body.get('error_message')
    (trade,) = body['trades']
    report = body['raw_report']['isolated_risk']
    assert report['liquidation_count'] == 1, report
    assert p6.kinds_of(body) == ['liquidation']
    assert Decimal(trade['exit_price']) == Decimal(report['liquidations'][0]['fill_price']) == Decimal('99')
    # A plain period-end close on the same last bar (no liquidation reached) is still end_of_data at Close.
    plain = frame('long', 105.0)
    body = run(monkeypatch, tmp_path, 'long', True, plain, leverage=10, position_size_pct=20)
    assert body['result_status'] == 'success', body.get('error_message')
    (trade,) = body['trades']
    assert body['raw_report']['isolated_risk']['liquidation_count'] == 0
    assert p6.kinds_of(body) == ['end_of_data']
    assert Decimal(trade['exit_price']) == Decimal('105')


def test_us_open_last_bar_window_stop_cannot_breach_margin(monkeypatch, tmp_path):
    # US Open Momentum long: window 13:30-14:00 Low 98 is the window stop, entry at the 14:00 open 100,
    # 10x isolated -> liquidation price 90, margin 2000 of 10000 (20% position). The last bar (14:30)
    # opens at 100 (no gap) and trades down to 80, closing at 85: it crosses the window stop and the
    # liquidation price. The last bar settles at its Close, so a window stop there cannot claim it is
    # crossed first: without the guard the trade would settle at 85 and lose 3000 > margin.
    data = f3.frame().loc[:'2026-03-09 14:30'].copy()
    data.loc['2026-03-09 14:30', ['Open', 'High', 'Low', 'Close']] = [100, 100.5, 80, 85]
    data.index = data.index.tz_localize('UTC')
    body = p6.post(monkeypatch, tmp_path, 'us_open_momentum', dict(leverage=10, position_size_pct=20), data,
                   market='futures', timeframe='15m')
    assert body['result_status'] == 'success', body.get('error_message')
    (trade,) = body['trades']
    assert Decimal(trade['entry_price']) == Decimal('100'), trade
    report = body['raw_report']['isolated_risk']
    margin = Decimal('2000')
    assert -Decimal(trade['pnl']) <= margin, trade
    assert report['liquidation_count'] == 1, report
    assert Decimal(report['loss_beyond_margin_total']) == 0, report
    assert p6.kinds_of(body) == ['liquidation']


def test_turtle_final_close_at_last_close(monkeypatch, tmp_path):
    # Statement 141: the turtle group's period-end close also fills at the last Close; closed_at stays the
    # last bar's open time. The channel break at SIGNAL opens a group that is still open at the end.
    data = frame('long', 115.0)
    assert data.Open.iloc[LAST] != data.Close.iloc[LAST]
    body = p6.post(monkeypatch, tmp_path, 'turtle', {}, data, market='futures')
    assert body['result_status'] == 'success', body.get('error_message')
    assert body['trades'], body
    last_open = int(data.index[LAST].timestamp())
    assert all(trade['closed_at'] == last_open for trade in body['trades']), body['trades']
    assert all(Decimal(trade['exit_price']) == Decimal('115') for trade in body['trades']), body['trades']
    assert set(p6.kinds_of(body)) == {'end_of_data'}
    (group,) = body['raw_report']['turtle_groups']
    assert group['trade_seqs'] == [trade['seq'] for trade in body['trades']]


def _module_case(name):
    """(base frame, template params) for one template of each module that hands its frozen stop to T2-2b."""
    if name in s4.SHORT:
        return s4.frame(name), {}
    if name in pliq.b.CASES:
        build, params, _ = pliq.b.CASES[name]
        return build(), dict(params)
    return pliq.base_frame(name), {}


# strategy_pattern_template (long / short), strategy_bottom_patterns, strategy_top_patterns, strategy_chan,
# strategy_divergence: each passes its frozen stop into _risk_isolated_exit before judging the stop.
MODULE_TEMPLATES = ('bullish_engulfing', s4.SHORT[0], 'double_bottom', 'double_top', 'chan_3sell',
                    'macd_bearish_divergence')
# Entry opens moved near the frozen stop so it is nearer than the 10x liquidation price (stop / L):
# double_bottom 99.9 / 94.5, double_top 100.1 / 104.5, macd_bearish_divergence 122.122 / 127.6.
# The others already fill with the stop nearer than liquidation.
NEAR_STOP_ENTRY = {'double_bottom': 105.0, 'double_top': 95.0, 'macd_bearish_divergence': 116.0}


@pytest.mark.parametrize('name', MODULE_TEMPLATES)
def test_template_stop_cannot_veto_last_bar_liquidation(monkeypatch, tmp_path, name):
    # The first trade's fill bar becomes the second-to-last bar; the last bar opens at the entry (no gap)
    # and runs 20% against the position, closing 15% against it, through the frozen pattern stop and the
    # 10x liquidation price. The last bar settles at its Close, so the stop cannot veto liquidation there.
    base, params = _module_case(name)
    params = dict(params, leverage=10, position_size_pct=20)
    probe = pliq.post(monkeypatch, tmp_path, name, params, base, 'futures')
    assert probe['result_status'] == 'success' and probe['trades'], probe.get('error_message')
    opened = probe['trades'][0]['opened_at']
    fill = [int(ts.timestamp()) for ts in base.index].index(opened)
    long = probe['trades'][0]['side'] == 'long'
    data = base.iloc[:fill + 2].copy()
    E = NEAR_STOP_ENTRY.get(name, float(data.Open.iloc[fill]))
    ohlc = [data.columns.get_loc(column) for column in ('Open', 'High', 'Low', 'Close')]
    if name == 'macd_bearish_divergence':
        # A flat fill bar would flip MACD into the template's cross-up exit: keep its close (106) and keep
        # its low above the 2R target the 116 entry re-freezes (116 - 2 * 6.122 = 103.756).
        data.iloc[fill, ohlc] = [E, E, 104.5, data.Close.iloc[fill]]
    else:
        data.iloc[fill, ohlc] = [E, E * 1.001, E * 0.999, E]
    data.iloc[fill + 1, ohlc] = [E, E * 1.001, E * 0.8, E * 0.85] if long else [E, E * 1.2, E * 0.999, E * 1.15]
    body = pliq.post(monkeypatch, tmp_path, name, params, data, 'futures')
    assert body['result_status'] == 'success', body.get('error_message')
    (trade,) = body['trades']
    assert trade['opened_at'] == opened and Decimal(trade['entry_price']) == Decimal(str(E)), trade
    margin = Decimal(trade['qty']) * Decimal(trade['entry_price']) / 10
    report = body['raw_report']['isolated_risk']
    assert report['liquidation_count'] == 1, (report, trade)
    assert Decimal(report['loss_beyond_margin_total']) == 0, report
    assert -Decimal(trade['pnl']) <= margin, trade
    assert p6.kinds_of(body) == ['liquidation']


# ---------------------------------------------------------------- the last bar opens nothing in the re-run

def record_leftovers(monkeypatch):
    """Record, per Backtest.run, the trades still open and the orders still queued after finalize_trades."""
    leftovers, run_backtest = [], Backtest.run

    def recording(self, **kwargs):
        stats = run_backtest(self, **kwargs)
        broker = stats._strategy._broker
        leftovers.append(([(t.size, t.entry_price, t.entry_bar) for t in broker.trades], list(broker.orders)))
        return stats

    monkeypatch.setattr(Backtest, 'run', recording)
    return leftovers


def summary_matches_trades(body):
    """Legacy summary return (engine equity, rounded to 0.01 pct) vs the sum of the itemised pnl.

    fee_bps / slippage_bps are 0 on these runs, so the two only differ by the 2-decimal rounding of
    total_return_pct (<= 0.005 pct) when every filled trade is settled."""
    capital = Decimal(body['initial_capital'])
    itemised = sum((Decimal(t['pnl']) for t in body['trades']), Decimal(0)) / capital * 100
    summary = Decimal(str(body['raw_report']['legacy_metrics']['total_return_pct']))
    return abs(summary - itemised) <= Decimal('0.0051'), (summary, itemised)


def tp_then_cross_frame():
    """Codex's PIN7-E P2 case on ema_cross 5/20 with two take-profit levels (stop 5%, tp1 1R, tp2 2R).

    Golden cross at bar 30 (entry fills at Close[30] = 103, trade_on_close). Bar 58 (second to last)
    touches tp1 (High 109 >= 108.15) and closes 99: the partial take profit returns before the death
    cross is looked at. The last bar closes 120: golden cross again, High above tp2 (113.3). Fixed
    fe3f762 settled tp2 there; PIN7-E skips the last bar's exit, so the template went on to _risk_buy and
    the period-end re-run filled that buy at Close[-2] (99) and never settled it."""
    close = np.r_[np.linspace(100, 99, 30), [103.0], np.full(27, 104.0), [99.0, 120.0]]
    open_ = np.r_[close[0], close[:-1]]
    high, low = np.maximum(open_, close) + 0.1, np.minimum(open_, close) - 0.1
    high[58] = 109.0
    index = pd.date_range('2024-01-01', periods=len(close), freq='1h', tz='UTC')
    return pd.DataFrame(dict(Open=open_, High=high, Low=low, Close=close, Volume=np.full(len(close), 500.0)),
                        index=index)


TP_LEVELS = dict(ema_fast=5, ema_slow=20, risk_layer_enabled=True, stop_loss_pct=5,
                 tp1_r=1, tp1_close_pct=50, tp2_r=2, tp2_close_pct=50)


def test_last_bar_new_entry_not_filled_in_final_rerun(monkeypatch, tmp_path):
    leftovers = record_leftovers(monkeypatch)
    data = tp_then_cross_frame()
    body = p6.post(monkeypatch, tmp_path, 'ema_cross', TP_LEVELS, data)
    assert body['result_status'] == 'success', body.get('error_message')
    assert leftovers == [([], [])], leftovers
    ok, detail = summary_matches_trades(body)
    assert ok, detail
    # fe3f762 also closed two rows here: the tp1 half, and the rest (at tp2 there, at period end now).
    assert len(body['trades']) == 2 and p6.kinds_of(body) == ['take_profit', 'end_of_data'], body['trades']
    assert Decimal(body['trades'][1]['exit_price']) == Decimal(str(data.Close.iloc[-1]))


# Generic-run templates whose entry, cut right after its signal bar, the period-end re-run used to fill and
# leave open (fe3f762 already did; it is not specific to the PIN7-E last-bar exit skip). turtle enters
# through its own group orders, not _FixedRiskMixin._risk_open. vwap_reversion / red_streak_rsi and the
# candle / pattern templates refuse a last-bar entry themselves and are not listed.
CUT_AT_SIGNAL = [
    'adx_di_cross', 'bias_reversion', 'bollinger_breakout', 'bollinger_reversal', 'bollinger_squeeze_breakout',
    'breakout', 'cci_rsi', 'ema_cross', 'ema_pullback', 'ema_rsi_pullback', 'ema_trend_rsi',
    'ema_triple_alignment', 'ichimoku_cloud_breakout', 'keltner_breakout', 'macd', 'macd_above_zero',
    'parabolic_sar', 'roc', 'rsi_reversal', 'stoch_oversold_cross', 'supertrend', 'turtle', 'volume_breakout',
]


@pytest.mark.parametrize('name', CUT_AT_SIGNAL)
def test_last_bar_signal_entry_not_filled(monkeypatch, tmp_path, name):
    # Cut the generic run right after the signal bar of its last entry: that entry is then queued by the
    # last next() and has no next bar to fill on.
    p6.CURRENT[0] = name
    full = p6.generic()(monkeypatch, tmp_path / 'full')
    assert full['result_status'] == 'success' and full['trades'], full.get('error_message')
    data = p6.generic_frame()
    opened = full['trades'][-1]['opened_at']
    fill = p6.bar_index(data, opened)
    leftovers = record_leftovers(monkeypatch)
    markets = p.TOOL_SPECS[PREFIX + name].get('markets') or ['spot']
    body = p6.post(monkeypatch, tmp_path, name, None, data.iloc[:fill],
                   market='spot' if 'spot' in markets else 'futures')
    assert body['result_status'] == 'success', body.get('error_message')
    assert leftovers == [([], [])], leftovers
    ok, detail = summary_matches_trades(body)
    assert ok, detail
    assert len(body['trades']) == sum(t['opened_at'] < opened for t in full['trades']), body['trades']


def win_rate_matches_trades(body):
    """Legacy (engine) win rate vs the share of itemised trades with pnl > 0."""
    trades = body['trades']
    itemised = round(sum(Decimal(t['pnl']) > 0 for t in trades) / len(trades) * 100, 2) if trades else 0.0
    return body['raw_report']['legacy_metrics']['win_rate_pct'] == itemised, (
        body['raw_report']['legacy_metrics']['win_rate_pct'], itemised)


def last_bar_liquidation_frame():
    """breakout long entered at 110; the last bar dips to 95 through the 10x liquidation price 99 (no gap:
    it opens at 110) and closes at 125, well above the entry."""
    data = frame('long', 125.0)
    data.iloc[-1, data.columns.get_loc('Low')] = 95.0
    return data


def last_bar_liquidation(monkeypatch, tmp_path):
    return run(monkeypatch, tmp_path, 'long', True, last_bar_liquidation_frame(), stop_loss_pct=5,
               take_profit_r=1, leverage=10, position_size_pct=20)


SUMMARY_CASES = {**{name: None for name in p6.V2_TOOLS}, 'breakout/last_bar_liquidation': last_bar_liquidation}


@pytest.mark.parametrize('name', SUMMARY_CASES)
def test_summary_return_equals_trade_pnl_across_templates(monkeypatch, tmp_path, name):
    """Filled-but-unsettled / re-priced-settlement detector: the engine summary (return, win rate) equals
    the itemised trades on every template's run, plus a leveraged last-bar liquidation."""
    p6.CURRENT[0] = name
    body = (SUMMARY_CASES[name] or p6.SPECIAL.get(name, p6.generic()))(monkeypatch, tmp_path)
    assert body['result_status'] == 'success', body.get('error_message')
    ok, detail = summary_matches_trades(body)
    assert ok, (name, detail)
    ok, detail = win_rate_matches_trades(body)
    assert ok, (name, detail)


def record_engine_trades(monkeypatch):
    """Record each Backtest.run's engine-side closed trades (stats._trades)."""
    runs, run_backtest = [], Backtest.run

    def recording(self, **kwargs):
        stats = run_backtest(self, **kwargs)
        runs.append(stats._trades.copy())
        return stats

    monkeypatch.setattr(Backtest, 'run', recording)
    return runs


def test_last_bar_liquidation_not_repriced_to_close(monkeypatch, tmp_path):
    # Codex PIN7-E round 2: the liquidation close queued on the last bar is filled in finalize_trades'
    # re-run, which re-priced it to Close[-1] (125): result.v2 kept the 99 liquidation loss, but the
    # engine booked a win (legacy win_rate_pct 100).
    runs = record_engine_trades(monkeypatch)
    body = last_bar_liquidation(monkeypatch, tmp_path)
    assert body['result_status'] == 'success', body.get('error_message')
    (trade,) = body['trades']
    assert p6.kinds_of(body) == ['liquidation']
    assert body['raw_report']['isolated_risk']['liquidation_count'] == 1
    assert Decimal(trade['exit_price']) == Decimal('99') and Decimal(trade['pnl']) < 0, trade
    (engine,) = runs[-1].itertuples()
    assert Decimal(str(engine.ExitPrice)) == Decimal(trade['exit_price']), engine
    assert engine.PnL < 0 and Decimal(str(engine.ReturnPct)).quantize(Decimal('1e-9')) == (
        Decimal(trade['pnl']) / (Decimal(trade['entry_price']) * Decimal(trade['qty']))).quantize(Decimal('1e-9'))
    assert body['raw_report']['legacy_metrics']['win_rate_pct'] == 0.0
    # The same last bar without reaching liquidation is still a period-end close at Close[-1].
    plain = frame('long', 125.0)
    body = run(monkeypatch, tmp_path, 'long', True, plain, stop_loss_pct=5, take_profit_r=1, leverage=10,
               position_size_pct=20)
    (trade,) = body['trades']
    assert p6.kinds_of(body) == ['end_of_data']
    assert Decimal(trade['exit_price']) == Decimal('125') == Decimal(str(runs[-1].ExitPrice.iloc[0]))
    assert body['raw_report']['legacy_metrics']['win_rate_pct'] == 100.0


# ---------------------------------------------------------------- what the period-end re-run may fill

def test_final_rerun_class_a_finalize_close_fills_at_close(monkeypatch, tmp_path):
    # (a) finalize_trades' Trade.close() of a trade nothing else closed fills at Close[-1], end_of_data.
    runs = record_engine_trades(monkeypatch)
    data = frame('long', 105.0)
    trade = only_trade(run(monkeypatch, tmp_path, 'long', True, data, stop_loss_pct=5, take_profit_r=1,
                           leverage=10, position_size_pct=20))
    assert Decimal(trade['exit_price']) == Decimal('105') == Decimal(str(runs[-1].ExitPrice.iloc[0]))
    assert Decimal(str(data.Open.iloc[-1])) != Decimal('105')


def _toy_frame():
    index = pd.date_range('2024-01-01', periods=6, freq='1h', tz='UTC')
    close = np.array([100.0, 100.0, 101.0, 102.0, 103.0, 110.0])
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame(dict(Open=open_, High=np.maximum(open_, close) + 0.1, Low=np.minimum(open_, close) - 0.1,
                             Close=close, Volume=np.full(6, 1.0)), index=index)


def test_final_rerun_fourth_kind_of_order_raises(monkeypatch):
    # A close the last next() queued that would fill before finalize_trades' own close is none of (a) / (b) /
    # (c): simulate a library whose Trade.close appends instead of inserting at the head.
    from backtesting import Strategy
    import backtesting.backtesting as engine

    class LastBarClose(Strategy):
        def init(self):
            self.__dict__['_n'] = len(self.data)

        def next(self):
            if len(self.data) == 2:
                self.buy(size=1)
            elif len(self.data) == self.__dict__['_n'] and self.trades:
                self.trades[-1].close()  # a template signal close on the last bar

    def appending_close(trade, portion=1.):
        order = engine.Order(trade._Trade__broker, -trade.size, parent_trade=trade)
        trade._Trade__broker.orders.append(order)

    monkeypatch.setattr(engine.Trade, 'close', appending_close)
    bt = Backtest(_toy_frame(), p._final_close_strategy(LastBarClose), cash=10000, finalize_trades=True)
    with pytest.raises(RuntimeError, match='PIN7E_FINAL_RERUN: unexpected first order'):
        bt.run()


def test_final_rerun_unclosed_trade_degrades_to_engine_error(monkeypatch, tmp_path):
    # A trade left without finalize_trades' close is not admitted either; on the provider path the raise
    # degrades the run like any engine failure.
    import backtesting.backtesting as engine
    monkeypatch.setattr(engine.Trade, 'close', lambda trade, portion=1.: None)
    body = run(monkeypatch, tmp_path, 'long', True, frame('long', 105.0), stop_loss_pct=5, take_profit_r=1)
    assert body['result_status'] != 'success', body
    assert 'PIN7E_FINAL_RERUN: a trade has no period-end close' in body['error_message'], body


# Premise pin. The PIN7-E period-end rerun (b) class matches an isolated-liquidation record to a trade by
# opened_at only (admit_final_orders), so two positions opened on the same bar with only one liquidated would
# let the other be settled at the liquidation price. Only templates with exclusive_orders=False can hold
# several positions on one bar; today that is the turtle alone, and its strategy class has no isolated-margin
# risk (no leverage param, no _risk_isolated_exit, which lives on _FixedRiskMixin). Whoever adds a
# multi-position template that is also isolated-leveraged must first tighten the match key to per trade (pin 8).
def test_multi_position_templates_have_no_isolated_leverage():
    turtle_id = 'local.backtesting_py.turtle'
    multi = {tool_id for tool_id, spec in p.TOOL_SPECS.items() if spec.get('exclusive_orders', True) is False}
    assert multi == {turtle_id}, (
        f'multi-position templates changed: {sorted(multi)}. PIN7-E period-end rerun (b) matches liquidation '
        'records by opened_at (admit_final_orders), so several positions opened on one bar can be mis-settled; '
        'tighten the match key to per trade (pin 8) before adding a multi-position + isolated-leverage template')
    for tool_id in sorted(multi):
        spec = p.TOOL_SPECS[tool_id]
        assert 'leverage' not in spec['param_schema_properties'], (
            f'{tool_id} exposes leverage: multi-position + isolated margin breaks the opened_at match of the '
            'PIN7-E (b) class; tighten the match key to per trade (pin 8) first')
        strategy = spec['build']({}, initial_capital=10000.0)['strategy']
        assert not hasattr(strategy, '_risk_isolated_exit'), (
            f'{tool_id} strategy carries isolated risk (_risk_isolated_exit): multi-position + isolated margin '
            'breaks the opened_at match of the PIN7-E (b) class; tighten the match key to per trade (pin 8) first')
