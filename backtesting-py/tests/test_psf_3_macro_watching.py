"""PSF-3: native broker fills must block existing watchers, not future ones.

Public H1 currently retains intrinsic legacy protection. This unit harness opts
H1 into the shared native layer and seeds one trade after a normal next() so an
unconsumed watcher can coexist with its entry-bar protective fill. The broker,
macro watcher loop, and native blocked-next hook are all production code.
The seeded SL/TP are generic 5% protection, not H1 range-stop/take_profit_r.
This does not demonstrate native protection through the public H1 HTTP route.
"""
import pandas as pd
import pytest
from backtesting import Backtest

import cutie_backtesting_provider as p
import test_q18_macro_events as macro
from strategy_macro_events import MacroConfig, make_macro_strategy


@pytest.fixture
def native_watching_run():
    def run(exit_kind, *, existing_watcher=False):
        data = macro.frame(24)
        data[['Open', 'High', 'Low', 'Close']] = [100., 101., 99., 100.]
        # A seeded market order fills at bar 13; only one native protection is
        # touched. Bar 14 closes above the fresh event's frozen pre-event range.
        data.iloc[13] = ([100, 101, 94, 100, 1] if exit_kind == 'stop_loss'
                         else [100, 106, 99, 100, 1])
        data.iloc[14] = [100, 108, 99, 107, 1]
        data.iloc[15:] = [107, 108, 106, 107, 1]
        # Off-grid release becomes watching in next() at bar 14 (k+1), not
        # at the close of bar 13 as a boundary release at open 14 would.
        fresh = macro.event(14)
        fresh['ts_utc'] = pd.Timestamp(macro.T0 + 14 * macro.STEP + 60,
                                      unit='s', tz='UTC').isoformat()
        events = ([macro.event(12)] if existing_watcher else []) + [fresh]
        config = MacroConfig.parse(macro.params(macro.H1, events=events,
                                              direction='long'), macro.H1)
        risk = p._parse_fixed_risk_params(dict(risk_layer_enabled=True,
                                            stop_loss_pct=5, take_profit_pct=5))
        strategy = make_macro_strategy(p._FixedRiskMixin, config, risk, 10000)
        snapshots = []

        class SeededNative(strategy):
            def next(self):
                super().next()
                if len(self.data) - 1 == 12:
                    # No breakout yet: a pending watcher has no status. Seed a
                    # position independently to exercise blocked-next directly.
                    self._entry_record = dict(range_low=99., range_high=101.)
                    self._risk_buy()

            def _risk_native_blocked_next(self):
                snapshots.append((len(self.data) - 1,
                                  [(r['label'], r.get('status')) for r in self._watching]))
                super()._risk_native_blocked_next()

        stats = Backtest(data, p._exit_kind_strategy(SeededNative), cash=10000, exclusive_orders=True,
                         finalize_trades=True).run()
        return stats, snapshots
    return run


@pytest.mark.parametrize('exit_kind', ['stop_loss', 'take_profit'])
def test_new_watching_event_next_bar_enters_after_native_exit(native_watching_run, exit_kind):
    stats, snapshots = native_watching_run(exit_kind)
    trades = stats['_trades']
    assert stats['_strategy']._risk_native_enabled
    assert snapshots == [(13, [])]
    assert len(trades) == 2
    assert list(trades.EntryBar) == [13, 15]
    assert list(trades.ExitBar) == [13, 18]
    assert trades.ExitPrice.iloc[0] == (95 if exit_kind == 'stop_loss' else 105)
    assert p._engine_trade_exit_kinds(trades, stats['_strategy']._broker.closed_trades)[0] == exit_kind
    [fresh] = stats['_strategy'].macro_events
    assert fresh['status'] == 'submitted'
    assert fresh['entry_bar_utc'] == '2026-01-01T01:15:00+00:00'


@pytest.mark.parametrize('exit_kind', ['stop_loss', 'take_profit'])
def test_existing_watching_event_skipped_on_native_exit_bar(native_watching_run, exit_kind):
    stats, snapshots = native_watching_run(exit_kind, existing_watcher=True)
    trades = stats['_trades']
    assert snapshots == [(13, [('E12', None)])]
    old, fresh = stats['_strategy'].macro_events
    assert old['status'] == 'skipped_in_position'
    assert old['reason'] == 'native_exit_no_reentry'
    assert fresh['status'] == 'submitted'
    assert len(trades) == 2
    assert list(trades.EntryBar) == [13, 15]
    assert list(trades.ExitBar) == [13, 18]
