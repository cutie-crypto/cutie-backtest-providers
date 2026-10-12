"""PSF-2: native-stop blocked event decisions through real provider routes."""
import pandas as pd
import pytest
from fastapi.testclient import TestClient
import cutie_backtesting_provider as p
import test_event0_event_window as event0
import test_q18_macro_events as macro
import test_p2_funding_settlement_reversal as funding
import test_s4_liquidation_reversal as liquidation


def request(kind, params, data, timeframe, market='spot'):
    step = int((data.index[1] - data.index[0]).total_seconds())
    body = dict(run_id='psf2', provider_tool_id=f'local.backtesting_py.{kind}',
                provider_params=params, symbol='BTCUSDT', market=market, timeframe=timeframe,
                start_at=int(data.index[0].timestamp()), end_at=int(data.index[-1].timestamp()) + step,
                initial_capital='10000', fee_bps='0', slippage_bps='0')
    return TestClient(p.app).post('/cutie/backtest', json={'backtest': body}).json()


@pytest.mark.parametrize('kind', ['event_window', 'macro_surprise_direction',
                                 'funding_settlement_reversal', 'liquidation_reversal'])
def test_event_on_native_stop_bar_has_status(monkeypatch, tmp_path, kind):
    original = p.settle_event_window_exits
    errors, captured = [], []

    def settle(events, trades, isolated, **log):
        captured.append((events, trades, log))
        try:
            return original(events, trades, isolated, **log)
        except ValueError as exc:
            errors.append(exc)
            raise

    monkeypatch.setattr(p, 'settle_event_window_exits', settle)
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(macro.Backtest, 'plot', lambda *a, **k: None)
    if kind == 'event_window':
        data = event0.frame()
        data[['Open', 'High', 'Low', 'Close']] = [100., 100.5, 99.5, 100.]
        data.iloc[5] = [100., 100.5, 94., 100., 1.]
        params = dict(events=[dict(ts_utc=f'2026-01-01T{bar:02d}:00:00Z', label=str(bar))
                              for bar in (4, 6, 10)], bars_after=12,
                      risk_layer_enabled=True, stop_loss_pct=5)
        monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a, **k: data.copy())
        body = request(kind, params, data, '1h')
        blocked = lambda records: records[1]
    elif kind == 'macro_surprise_direction':
        data = macro.frame()
        data[['Open', 'High', 'Low', 'Close']] = [100., 100.5, 99.5, 100.]
        data.iloc[15] = [100., 100.5, 96., 100., 1.]
        params = macro.params(kind, events=[macro.event(i, expected=1, actual=0) for i in (12, 16, 60)],
                              hold_minutes=120, stop_loss_pct=3)
        monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a, **k: data.copy())
        body = request(kind, params, data, '5m', 'futures')
        blocked = lambda records: records[1]
    elif kind == 'funding_settlement_reversal':
        data = funding.frame()
        data.iloc[61] = [100., 100.5, 96., 100., 1.]
        # A data gap carries the first position to the next settlement decision.
        data = data.drop(data.index[31:61])
        funding.install(monkeypatch, tmp_path, {0: '-0.001', 1: '-0.001', 2: '-0.001'}, data)
        params = dict(hold_minutes=240, stop_loss_pct=3)
        body = request(kind, params, data, '15m', 'futures')
        blocked = lambda records: next(r for r in records if r['event_bar_utc'].endswith('15:30:00+00:00'))
    else:
        rows = {5: [100, 100.5, 95, 96], 6: [96, 96.5, 95.5, 96],
                7: [92, 93, 91, 92], 8: [92, 93, 88, 92], 9: [92, 93, 91, 92],
                10: [92, 93, 87, 88], 11: [88, 89, 87, 88], 12: [88, 89, 87, 88]}
        data = liquidation.candles('1d', rows, n_days=24)
        liquidation.install(monkeypatch, tmp_path, liquidation.store_for({5: 1000, 7: 1000, 10: 1000}, last_day=26), data)
        params = dict(hold_hours=240, stop_loss_pct=3, take_profit_pct=50)
        body = request(kind, params, data, '1d')
        blocked = lambda records: next(r for r in records if r.get('day_utc') == liquidation.lr._iso(liquidation.T0 + 7 * liquidation.DAY))
    records, trades, log = captured[-1]
    # Frozen at pin 13: queue signal candle, actual entry open, entry price and count.
    # Assert before settlement raises, so the same locks are exercised in red runs.
    base = int(data.index[0].timestamp())
    expected = {
        'event_window': [(3 * 3600, 4 * 3600, '100'), (9 * 3600, 10 * 3600, '100')],
        'macro_surprise_direction': [(11 * 300, 12 * 300, '100'), (59 * 300, 60 * 300, '100')],
        'funding_settlement_reversal': [(29 * 900, 30 * 900, '100'), (93 * 900, 94 * 900, '100')],
        'liquidation_reversal': [(6 * 86400, 7 * 86400, '92'), (11 * 86400, 12 * 86400, '88')],
    }[kind]
    actual = []
    for trade in trades:
        entry_index = data.index.get_loc(pd.Timestamp(trade['opened_at'], unit='s'))
        actual.append((int(data.index[entry_index - 1].timestamp()) - base,
                       trade['opened_at'] - base, trade['entry_price']))
    assert actual == expected
    if errors:
        raise errors[0]
    assert body['result_status'] == 'success', body
    records, trades, log = captured[-1]
    assert blocked(records)['status'] == 'skipped_in_position', (records, trades, log)
    assert len(trades) == 2
    if kind != 'event_window':
        assert blocked(records)['reason'] == 'native_exit_no_reentry'
        assert body['raw_report'][kind]['skipped_in_position_count'] == 1
    if kind == 'liquidation_reversal':
        assert body['assumptions'][kind]['signal_counts']['native_exit_no_reentry'] == 1
