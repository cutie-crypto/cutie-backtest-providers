"""P-LIQ1 follow-up: F5 vwap_reversion and F6 red_streak_rsi judge their stop on the close only when the
risk layer is off, so an intrabar liquidation precedes it (mixin legacy rule, provider _risk_check_exit).

Expected values are written by hand (lev 10, long L = E * 9 / 10):
  intrabar Low crosses L, close stays above the frozen stop -> liquidation at L on that bar
  close breaks the frozen stop, Low stays above L            -> stop_loss, next bar open
"""
import sys
from pathlib import Path

import backtesting.backtesting as bb

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _pliq1_cases as q
import test_f5_vwap_reversion as f5


def spied(monkeypatch, post):
    closes = []
    close = bb.Position.close

    def position_close(self, portion=1.0):
        strategy = sys._getframe(1).f_locals['self']
        closes.append((len(strategy.data) - 1, strategy._risk_exit_reason))
        return close(self, portion)
    monkeypatch.setattr(bb.Position, 'close', position_close)
    body = post()
    assert body['result_status'] == 'success', body
    return body, closes


def f6_case(monkeypatch, tmp_path, crash):
    # Fill bar k-1 at E 93, frozen stop 92 * (1 - 3%) = 89.24, L = 83.7.
    data, k = q.scenario_frame('red_streak_rsi', 'A1')
    ohlc = [data.columns.get_loc(column) for column in ('Open', 'High', 'Low', 'Close')]
    data.iloc[k, ohlc] = crash
    params = q.scenario_params('red_streak_rsi', 'A1', 'pct20')
    body, closes = spied(monkeypatch, lambda: q.post(monkeypatch, tmp_path, 'red_streak_rsi', params, data, 'futures'))
    [trade] = body['trades']
    return body, closes, trade, q.bar_of(data, trade['closed_at']), k


def test_f6_intrabar_liquidation_precedes_close_only_stop(monkeypatch, tmp_path):
    # opus55 review run: E 93, L 83.7, stop 89.24, Low 83.6, Close 90.
    body, closes, trade, closed_bar, k = f6_case(monkeypatch, tmp_path, [93.0, 93.2, 83.6, 90.0])
    assert trade['entry_price'] == '93'
    assert (trade['exit_price'], closed_bar) == ('83.7', k)
    assert closes == [(k, 'liquidation')]
    assert body['raw_report']['isolated_risk']['liquidation_count'] == 1


def test_f6_close_below_stop_without_liquidation_is_stop_loss(monkeypatch, tmp_path):
    body, closes, trade, closed_bar, k = f6_case(monkeypatch, tmp_path, [93.0, 93.2, 85.0, 88.0])
    assert (trade['exit_price'], closed_bar) == ('93', k + 1)
    assert closes == [(k, 'stop_loss')]
    assert body['raw_report']['isolated_risk']['liquidation_count'] == 0


def f5_case(monkeypatch, tmp_path, fill_bar):
    # Signal close 94 at bar 2, fill at bar 3 open 94: frozen stop 94 * .98 = 92.12, L = 84.6.
    data = f5.frame()
    data.loc[data.index[3], ['Open', 'High', 'Low', 'Close']] = fill_bar
    body, closes = spied(monkeypatch, lambda: f5.response(monkeypatch, tmp_path, {'leverage': 10}, data, market='futures'))
    first = body['trades'][0]
    return body, closes, first, int(data.index.get_loc(f5.pd.Timestamp(first['closed_at'], unit='s')))


def test_f5_intrabar_liquidation_precedes_close_only_stop(monkeypatch, tmp_path):
    body, closes, first, closed_bar = f5_case(monkeypatch, tmp_path, [94, 95, 84, 93])
    assert first['entry_price'] == '94'
    assert (first['exit_price'], closed_bar) == ('84.6', 3)
    assert closes[0] == (3, 'liquidation')
    assert body['raw_report']['isolated_risk']['liquidation_count'] == 1


def test_f5_close_below_stop_without_liquidation_is_stop_loss(monkeypatch, tmp_path):
    body, closes, first, closed_bar = f5_case(monkeypatch, tmp_path, [94, 95, 90, 91])
    assert first['entry_price'] == '94'
    assert (first['exit_price'], closed_bar) == ('200', 4)
    assert closes[0] == (3, 'stop_loss')
    assert body['raw_report']['isolated_risk']['liquidation_count'] == 0
