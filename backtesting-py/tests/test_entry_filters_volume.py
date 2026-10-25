"""JES22-B provider unit tests: the causal, direction-neutral volume entry filter."""
from __future__ import annotations
import importlib.util
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest
from backtesting import Backtest, Strategy
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p
from strategy_entry_filters import FilterConfig, FILTER_PARAM_SCHEMA_PROPERTIES, entry_mask

spec = importlib.util.spec_from_file_location(
    'filter_volume_capture', Path(__file__).parent / 'fixtures/capture_entry_filters_5b8320a.py')
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)

VOLUME_ON = dict(filter_layer_enabled=True, filter_volume_enabled=True)


def volume_frame(closes, volumes):
    return pd.DataFrame(dict(Open=closes, High=np.array(closes)+1, Low=np.array(closes)-1,
                             Close=closes, Volume=volumes),
                        index=pd.date_range('2026-01-01', periods=len(closes), freq='h'))


def config(**overrides):
    return FilterConfig.parse({**VOLUME_ON, **overrides})


# --- schema / parse / report ------------------------------------------------------------------------

def test_volume_schema_three_keys_published_to_every_filter_template():
    volume = {
        'filter_volume_enabled': {'type': 'boolean', 'default': False},
        'filter_volume_period': {'type': 'integer', 'default': 20, 'minimum': 5, 'maximum': 100},
        'filter_volume_multiple': {'type': 'number', 'default': 1.5, 'minimum': 1, 'maximum': 5},
    }
    for key, value in volume.items():
        assert FILTER_PARAM_SCHEMA_PROPERTIES[key] == value
    templates = [key for key, value in p.TOOL_SPECS.items()
                 if 'filter_layer_enabled' in value['param_schema_properties']]
    assert templates
    for key in templates:
        assert set(volume) <= set(p.TOOL_SPECS[key]['param_schema_properties']), key


def test_parse_accepts_volume_group_and_reports_predicate():
    cfg = config(filter_volume_period=7, filter_volume_multiple=2)
    assert cfg.volume_enabled and cfg.volume_period == 7 and cfg.volume_multiple == 2
    assert cfg.required_bars == 8  # N prior bars + the signal bar
    report = cfg.report('short')
    assert report['predicates']['volume'] == dict(
        rule='volume > multiple * mean(prev period)', period=7, multiple=2, timeframe='primary')
    assert 'volume' not in FilterConfig.parse({'filter_layer_enabled': True,
                                               'filter_ema_enabled': True}).report()['predicates']


def test_volume_is_a_complete_enabled_filter():
    # A volume-enabled layer satisfies "at least one filter" without any indicator filter.
    cfg = FilterConfig.parse({'filter_layer_enabled': True, 'filter_volume_enabled': True})
    assert cfg.volume_enabled and not cfg.ema_enabled and cfg.required_bars == 21


@pytest.mark.parametrize('values,message', [
    ({**VOLUME_ON, 'filter_volume_period': 4}, 'filter_volume_period must be within 5-100'),
    ({**VOLUME_ON, 'filter_volume_period': 101}, 'filter_volume_period must be within 5-100'),
    ({**VOLUME_ON, 'filter_volume_multiple': 0.5}, 'filter_volume_multiple must be within 1-5'),
    ({**VOLUME_ON, 'filter_volume_multiple': 6}, 'filter_volume_multiple must be within 1-5'),
    # non-default volume parameters while the volume filter stays off
    (dict(filter_layer_enabled=True, filter_ema_enabled=True, filter_volume_period=30),
     'non-default volume parameters require filter_volume_enabled=true'),
    # v1: primary-timeframe only, so the pair is rejected outright
    ({**VOLUME_ON, 'filter_ema_enabled': True, 'filter_timeframe': '1d'},
     'filter_volume cannot be combined with filter_timeframe'),
])
def test_invalid_volume_configs_rejected_before_fetch(values, message):
    with pytest.raises(ValueError, match=message):
        FilterConfig.parse(values)


# --- independent goldens ----------------------------------------------------------------------------

def test_independent_volume_golden():
    cfg = config(filter_volume_period=5, filter_volume_multiple=2)
    data = volume_frame([100]*8, [10, 10, 10, 10, 10, 30, 20, 100])
    # bar 5: mean(10..10)=10, 30>20 pass; bar 6: mean(10,10,10,10,30)=14, 20>28 fail;
    # bar 7: mean(10,10,10,30,20)=16, 100>32 pass; bars 0-4 are warmup.
    assert entry_mask(cfg, data, p._supertrend_arrays).tolist() == [False]*5 + [True, False, True]


def test_equal_threshold_does_not_pass():
    cfg = config(filter_volume_period=5, filter_volume_multiple=2)
    # bar 5: mean(10 x5)=10, threshold 20, and 20 > 20 is false (strictly greater only).
    data = volume_frame([100]*6, [10, 10, 10, 10, 10, 20])
    assert entry_mask(cfg, data, p._supertrend_arrays).tolist() == [False]*6


def test_average_excludes_the_signal_bar():
    cfg = config(filter_volume_period=5, filter_volume_multiple=2)
    # Prior mean of 10 sets the threshold at 20; 25 passes only because the current bar is excluded
    # (including it would give mean(10 x5,25)=12.5, threshold 25, and 25 > 25 would fail).
    data = volume_frame([100]*6, [10, 10, 10, 10, 10, 25])
    assert entry_mask(cfg, data, p._supertrend_arrays)[5]


def test_first_n_bars_never_pass():
    cfg = config(filter_volume_period=5, filter_volume_multiple=1)
    data = volume_frame([100]*7, [5, 5, 5, 5, 5, 10, 10])
    mask = entry_mask(cfg, data, p._supertrend_arrays)
    assert mask[:5].tolist() == [False]*5
    assert cfg.required_bars == 6
    assert mask[5]  # first bar with N completed prior bars


def test_nan_or_zero_average_never_passes():
    cfg = config(filter_volume_period=5, filter_volume_multiple=1)
    # zero average: any positive current volume is not trusted
    zero = volume_frame([100]*6, [0, 0, 0, 0, 0, 10])
    assert entry_mask(cfg, zero, p._supertrend_arrays).tolist() == [False]*6
    # NaN average from a NaN prior volume
    nan = volume_frame([100]*6, [10, 10, 10, 10, float('nan'), 100])
    assert entry_mask(cfg, nan, p._supertrend_arrays).tolist() == [False]*6
    # NaN / zero signal-bar volume never passes
    assert not entry_mask(cfg, volume_frame([100]*6, [10, 10, 10, 10, 10, float('nan')]),
                          p._supertrend_arrays)[5]
    assert not entry_mask(cfg, volume_frame([100]*6, [10, 10, 10, 10, 10, 0]),
                          p._supertrend_arrays)[5]


def test_short_uses_the_same_volume_judgement():
    cfg = config(filter_volume_period=5, filter_volume_multiple=2)
    data = volume_frame([100]*8, [10, 10, 10, 10, 10, 30, 20, 100])
    long = entry_mask(cfg, data, p._supertrend_arrays, direction='long')
    short = entry_mask(cfg, data, p._supertrend_arrays, direction='short')
    np.testing.assert_array_equal(long, short)


def test_volume_and_ema_direction_are_anded():
    values = dict(filter_layer_enabled=True, filter_ema_enabled=True, filter_ema_period=2,
                  filter_volume_enabled=True, filter_volume_period=5, filter_volume_multiple=1)
    cfg = FilterConfig.parse(values)
    data = volume_frame([100]*7, [10, 10, 10, 10, 10, 10, 100])
    combined = entry_mask(cfg, data, p._supertrend_arrays)
    ema_only = FilterConfig.parse({**values, 'filter_volume_enabled': False,
                                   'filter_volume_period': 20, 'filter_volume_multiple': 1.5})
    volume_only = FilterConfig.parse({**values, 'filter_ema_enabled': False, 'filter_ema_period': 200})
    expected = (entry_mask(ema_only, data, p._supertrend_arrays)
                & entry_mask(volume_only, data, p._supertrend_arrays))
    np.testing.assert_array_equal(combined, expected)


# --- runtime wiring: gate, discard, exits -----------------------------------------------------------

def test_volume_signal_close_gate_and_exit_unrestricted():
    cfg = config(filter_volume_period=5, filter_volume_multiple=1.5)

    class Manual(p._FixedRiskMixin, Strategy):
        _filter_config = cfg
        def init(self):
            self._risk_init()
        def next(self):
            if self.position and self._risk_check_exit():
                return
            if len(self.data) - 1 == 10:
                self._risk_buy()
            elif self.position:
                self.position.close()
    data = volume_frame([100]*10+[110, 90, 120, 80], [100]*10 + [200] + [100]*3)
    trades = Backtest(data, Manual, cash=100000, exclusive_orders=True, finalize_trades=True).run()['_trades']
    # Signal bar 10 passes (prior mean 100, 200 > 150); entry fills at 11 and the close exits at 12.
    assert trades[['EntryBar', 'ExitBar']].values.tolist() == [[11, 12]]


def test_blocked_signal_is_discarded_not_delayed_for_volume():
    cfg = config(filter_volume_period=5, filter_volume_multiple=1.5)

    class Manual(p._FixedRiskMixin, Strategy):
        _filter_config = cfg
        def init(self):
            self._risk_init()
        def next(self):
            if len(self.data) - 1 == 10:
                self._risk_buy()
    # Signal bar 10 volume 50 is below 1.5 * 100; later bars are never signalled, so it is dropped.
    data = volume_frame([100]*11 + [110, 120, 130], [100]*10 + [50, 500, 500, 500])
    assert Backtest(data, Manual, cash=100000, finalize_trades=True).run()['_trades'].empty


def test_risk_exit_still_fires_when_volume_filter_blocks():
    cfg = config(filter_volume_period=5, filter_volume_multiple=1.5)

    class Manual(p._FixedRiskMixin, Strategy):
        _filter_config = cfg
        _risk = {'stop_loss_pct': .05}
        def init(self):
            self._risk_init()
        def next(self):
            if self.position and self._risk_check_exit():
                return
            if len(self.data) - 1 == 10:
                self._risk_buy()
    data = volume_frame([100]*10+[110, 100, 90, 85, 80], [100]*10 + [200] + [100]*4)
    trades = Backtest(data, Manual, cash=100000, finalize_trades=True).run()['_trades']
    # Entry passes at signal bar 10; the frozen 5% stop keeps working regardless of the filter.
    assert trades[['EntryBar', 'ExitBar']].values.tolist() == [[11, 13]]


# --- request path: reject before any market fetch ---------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('invalid volume filter request fetched market data')
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, '_fetch_ohlcv', forbidden)
    monkeypatch.setattr(p, '_fetch_template_warmup', forbidden)
    return TestClient(p.app)


def rejected(client, values):
    body = capture.capture.request_body('ema_cross', values)
    res = client.post('/cutie/backtest', json=body)
    assert res.status_code == 200
    assert res.json()['error_type'] == 'INVALID_PARAMS', res.json()


@pytest.mark.parametrize('values', [
    {**VOLUME_ON, 'filter_volume_period': 4},
    {**VOLUME_ON, 'filter_volume_period': 101},
    {**VOLUME_ON, 'filter_volume_multiple': 0.5},
    {**VOLUME_ON, 'filter_volume_multiple': 6},
    dict(filter_volume_period=30),
    dict(filter_layer_enabled=True, filter_ema_enabled=True, filter_volume_period=30),
    {**VOLUME_ON, 'filter_ema_enabled': True, 'filter_timeframe': '1d'},
    dict(filter_volume_enabled=True),  # enabled without the layer
])
def test_invalid_volume_requests_fail_before_fetch(client, values):
    rejected(client, values)
