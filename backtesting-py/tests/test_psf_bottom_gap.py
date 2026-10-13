"""PSF-BOTTOM-GAP: use the actual adjusted next-open fill, including equality."""

import pytest
from backtesting import Backtest

import cutie_backtesting_provider as provider

import test_9t3_patterns as bottom
import test_short_pat1 as top


def check_target_gap(module, names, is_long, monkeypatch, tmp_path):
    reason = ('entry_open_at_or_above_frozen_target' if is_long
              else 'entry_open_at_or_below_frozen_target')
    for name in names:
        original = module.frame(name)
        target = module.signals(name, original)[0][53].target
        # Equality is rejected too; both cases exercise actual-fill sizing.
        for confirmation, offset in ((confirm, delta) for confirm in (False, True)
                                    for delta in (0, 1 if is_long else -1)):
            data = original.copy()
            opening = target + offset
            entry_bar = 55 if confirmation else 54
            if confirmation:
                close = original['High' if is_long else 'Low'].iloc[53] + (1 if is_long else -1)
                data.iloc[54, :4] = [close, close + 1, close - 1, close]
            data.iloc[entry_bar, :4] = [opening, opening + 1, opening - 1, opening]
            params = dict(position_size_risk_pct=1, position_size_qty_step=0.001)
            if confirmation:
                params.update(filter_layer_enabled=True, filter_pattern_confirm_enabled=True,
                              filter_pattern_confirm_bars=1)
            body = top.http(monkeypatch, tmp_path, name, data, params)
            assert body['result_status'] == 'success', body
            assert body['trades'] == []
            report = body['raw_report']['bottom_pattern' if is_long else 'top_pattern']
            assert report['skipped_entry_count'] == 1
            [skip] = report['skipped_entries']
            assert skip == dict(reason=reason, signal_bar=53, entry_bar=entry_bar,
                               entry_open=opening, frozen_target=target,
                               adjusted_open=pytest.approx(opening))
            assert body['raw_report']['position_sizing']['fills'] == []
            assert body['raw_report']['position_sizing']['rejections'] == []

        # Exercise engine spread directly: HTTP slippage is accounted for
        # separately and does not change the engine entry price.
        data = original.copy()
        opening = target + (-0.5 if is_long else 0.5)
        data.iloc[54, :4] = [opening, opening + 1, opening - 1, opening]
        cls = provider.TOOL_SPECS['local.backtesting_py.' + name]['build']({})['strategy']
        stats = Backtest(data, cls, cash=10000, spread=0.01, exclusive_orders=True).run()
        assert stats['_trades'].empty and not stats['_strategy'].trades
        report = getattr(stats['_strategy'], 'bottom_pattern_report' if is_long else 'top_pattern_report')
        assert report['skipped_entry_count'] == 1
        assert report['skipped_entries'][0]['adjusted_open'] == pytest.approx(
            opening * (1.01 if is_long else 0.99))

        # A signal close beyond TP is still usable when the actual next open
        # returns inside TP: never substitute the signal close for the fill.
        data = original.copy()
        close = target + (1 if is_long else -1)
        data.iloc[53, :4] = [close, close + 1, close - 1, close]
        body = top.http(monkeypatch, tmp_path, name, data)
        assert len(body['trades']) == 1
        assert body['raw_report']['bottom_pattern' if is_long else 'top_pattern']['skipped_entry_count'] == 0


def test_long_entry_at_or_above_target_has_no_trades(monkeypatch, tmp_path):
    check_target_gap(bottom, bottom.NAMES, True, monkeypatch, tmp_path)


def test_short_entry_at_or_below_target_has_no_trades(monkeypatch, tmp_path):
    check_target_gap(top, top.NAMES, False, monkeypatch, tmp_path)
