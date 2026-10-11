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
    assert Decimal(body['trades'][0]['exit_price']) == Decimal(str(data.Open.iloc[LAST]))


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
    run(monkeypatch, tmp_path, direction, layer, trigger_one_bar_earlier(direction, last_close), **risk)
    assert calls, 'control: the same verdict one bar before the last must queue a close'


@pytest.mark.parametrize('direction,layer,risk,last_close', STOP)
def test_last_bar_names_no_stop_exit(monkeypatch, tmp_path, direction, layer, risk, last_close):
    # The breakout template's own channel exit may still fire on a falling last bar; the stop verdict may not.
    names = []
    mark_exit_kind = p._mark_exit_kind
    monkeypatch.setattr(p, '_mark_exit_kind', lambda strategy, kind: (names.append(kind), mark_exit_kind(strategy, kind)))
    only_trade(run(monkeypatch, tmp_path, direction, layer, frame(direction, last_close), **risk))
    assert names == []
    run(monkeypatch, tmp_path, direction, layer, trigger_one_bar_earlier(direction, last_close), **risk)
    assert names == ['stop_loss'], 'control: the same verdict one bar before the last names the stop'


# ---------------------------------------------------------------- every engine-path v2 tool

NON_ENGINE_RUNNERS = {p.SCALE_IN_OUT_RUNNER, p.TURTLE_RUNNER, 'kernel_v3', p.ROTATION_RUNNER}
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
