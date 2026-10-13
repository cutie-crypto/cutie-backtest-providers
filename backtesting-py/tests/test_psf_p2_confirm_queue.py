"""A protective fill blocks entry while consuming this bar's pending confirmations."""
import pytest
from backtesting import Backtest
import cutie_backtesting_provider as p
from strategy_entry_filters import PatternConfirmQueue
import test_9t1_engulf_pin as candle


@pytest.mark.parametrize('pending', ['confirmed', 'expired'])
def test_protective_exit_advances_confirmation_queue_once(monkeypatch, pending):
    data = candle.frame('bullish_engulfing', size=34)
    data.iloc[26, :4] = [106, 110, 105, 109]
    data.iloc[27, :4] = [106, 107, 95, 106]
    data.iloc[28:, :4] = [106, 107, 105, 106]
    cls = p.TOOL_SPECS['local.backtesting_py.bullish_engulfing']['build'](
        dict(position_filter=False, filter_layer_enabled=True, filter_pattern_confirm_enabled=True,
             filter_pattern_confirm_bars=1))['strategy']
    calls = []
    advance = PatternConfirmQueue.advance
    injected = []

    def observed(self, bar, close):
        calls.append(bar)
        if bar == 26:
            hit = advance(self, bar, close)
            self.register(26, 'long', 105 if pending == 'confirmed' else 1000)
            injected.append(self)
            return hit
        return advance(self, bar, close)

    monkeypatch.setattr(PatternConfirmQueue, 'advance', observed)
    stats = Backtest(data, p._final_close_strategy(p._exit_kind_strategy(cls)), cash=10000,
                     exclusive_orders=True, finalize_trades=True).run()
    assert stats['_trades'].EntryBar.tolist() == [27]
    assert stats['_trades'].ExitBar.tolist() == [27]
    assert calls.count(27) == 1
    queue = injected[0]
    assert queue.submitted_count == 1
    assert queue.discarded['position_or_order_open'] == (pending == 'confirmed')
    assert queue.discarded['unconfirmed'] == (pending == 'expired')
    assert queue._pending == []
