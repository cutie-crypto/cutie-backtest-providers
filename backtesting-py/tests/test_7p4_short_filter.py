"""7-P4 short-side single-entry filter layer.

Short mirrors each predicate strictly: close < EMA, DIF < 0, Supertrend trend == -1; equality passes
neither side. Warmup (first required_bars - 1 bars) and the closed higher-timeframe clock are
direction-neutral. Long stays byte-identical. Expected values are hand arithmetic or an independent
scalar EMA recurrence, never produced by the code under test.
"""
from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from backtesting import Backtest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / 'fixtures'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _10b2b_cases as c  # noqa: E402
import capture_7p4_off_1d66844 as off4  # noqa: E402
import capture_7p3b2_off_c72c4a1 as off2  # noqa: E402
import cutie_backtesting_provider as p  # noqa: E402
from strategy_entry_filters import (FILTER_PARAM_SCHEMA_PROPERTIES, FilterConfig,  # noqa: E402
                                    HigherTimeframeContext, entry_mask)
from test_7p3b_pattern_range_filter import direct2, ema, flat_prefix  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = json.loads((Path(__file__).parent / 'fixtures/7p4_off_1d66844.json').read_text())
DEFAULTS = {key: value['default'] for key, value in FILTER_PARAM_SCHEMA_PROPERTIES.items()}
SHORT = ('macd_bearish_divergence', 'rsi_bearish_divergence', 'double_top', 'head_shoulders', 'chan_3sell')


def ema_on(period, **extra):
    return dict(filter_layer_enabled=True, filter_ema_enabled=True, filter_ema_period=period, **extra)


def frame(closes, freq='h', start='2026-01-01'):
    closes = np.asarray(closes, dtype='float64')
    return pd.DataFrame(dict(Open=closes, High=closes + 1, Low=closes - 1, Close=closes, Volume=100.),
                        index=pd.date_range(start, periods=len(closes), freq=freq))


def masks(values, data, supertrend=p._supertrend_arrays):
    config = FilterConfig.parse(values)
    return (entry_mask(config, data, supertrend, direction='short').tolist(),
            entry_mask(config, data, supertrend, direction='long').tolist())


def short_judged(closes, bar, period):
    return closes[bar] < ema(closes, period)[bar]


# ---- entry_mask: short mirrors, hand arithmetic ----

def test_ema_short_strict_and_equality_blocks_both_sides():
    # EMA3 (alpha 1/2): 100, 95, 87.5, 87.5, 83.75, 91.875.
    # Bar 1 closes 90 < 95 but sits in warmup (required_bars 3 => bars 0-1 closed).
    # Bar 3 closes 87.5 == EMA 87.5: neither side passes. Bar 5: 100 > 91.875 long only.
    short, long = masks(dict(filter_layer_enabled=True, filter_ema_enabled=True, filter_ema_period=3),
                        frame([100, 90, 80, 87.5, 80, 100]))
    assert short == [False, False, True, False, True, False]
    assert long == [False, False, False, False, False, True]


def test_macd_short_dif_zero_blocks_both_sides():
    # DIF(2,3) on a flat 100 is exactly 0: neither side. Warmup = 3*3 = 9 => bars 0-7 closed.
    # At 90: EMA2 = 100 - 20/3, EMA3 = 95 => DIF = -5/3 < 0. At 110: EMA2 = 104 4/9, EMA3 = 102.5 => +35/18.
    short, long = masks(dict(filter_layer_enabled=True, filter_macd_enabled=True, filter_macd_fast=2,
                             filter_macd_slow=3), frame([100] * 10 + [90, 110]))
    assert short == [False] * 10 + [True, False]
    assert long == [False] * 11 + [True]


def test_supertrend_short_is_trend_minus_one_only():
    # Stub trend: required_bars = atr 5 + 1 => bars 0-4 closed; nan and 0 never pass.
    trend = np.array([-1, -1, -1, -1, -1, -1, 1, np.nan, 0, -1], dtype='float64')
    stub = lambda *args: {'trend': trend}  # noqa: E731
    values = dict(filter_layer_enabled=True, filter_supertrend_enabled=True, filter_supertrend_atr_period=5,
                  filter_supertrend_multiplier=1)
    short, long = masks(values, frame([100.] * 10), stub)
    assert short == [False] * 5 + [True, False, False, False, True]
    assert long == [False] * 6 + [True, False, False, False]
    # Real ST(5,1) (test_entry_filters golden): the flat stretch starts at trend -1 (why long fails there),
    # 110 crosses up, then 90 crosses down => short after warmup on the flat stretch and at 90, never at 110.
    short, long = masks(values, frame([100] * 10 + [110, 90]))
    assert short == [False] * 5 + [True] * 5 + [False, True]
    assert long == [False] * 10 + [True, False]


def test_long_default_matches_explicit_long_and_bad_direction_raises():
    rng = np.random.default_rng(7)
    data = frame(100 + np.cumsum(rng.normal(0, 2, 300)))
    combos = [ema_on(5), dict(filter_layer_enabled=True, filter_macd_enabled=True, filter_macd_fast=3,
                              filter_macd_slow=7),
              dict(filter_layer_enabled=True, filter_supertrend_enabled=True, filter_supertrend_atr_period=5,
                   filter_supertrend_multiplier=2),
              {**ema_on(9), 'filter_macd_enabled': True, 'filter_supertrend_enabled': True}]
    for values in combos:
        config = FilterConfig.parse(values)
        default = entry_mask(config, data, p._supertrend_arrays)
        np.testing.assert_array_equal(default, entry_mask(config, data, p._supertrend_arrays, direction='long'))
        short = entry_mask(config, data, p._supertrend_arrays, direction='short')
        assert not (default & short).any()
        assert config.report() == config.report('long')
    with pytest.raises(ValueError):
        entry_mask(FilterConfig.parse(ema_on(5)), data, p._supertrend_arrays, direction='both')


def test_report_rules_mirror_for_short():
    config = FilterConfig.parse({**ema_on(5), 'filter_macd_enabled': True, 'filter_supertrend_enabled': True})
    long, short = config.report(), config.report('short')
    assert long['direction'] == 'long' and short['direction'] == 'short'
    assert [v['rule'] for v in long['predicates'].values()] == ['close > ema', 'dif > 0', 'trend == +1']
    assert [v['rule'] for v in short['predicates'].values()] == ['close < ema', 'dif < 0', 'trend == -1']
    assert {k: v for k, v in long.items() if k not in ('direction', 'predicates')} == \
        {k: v for k, v in short.items() if k not in ('direction', 'predicates')}


def test_long_filter_fixture_bytes_unchanged_since_base():
    relative = 'backtesting-py/tests/fixtures/entry_filters_5b8320a.json'
    assert (ROOT / relative).read_bytes() == subprocess.check_output(['git', 'show', '52064d4:' + relative], cwd=ROOT)


# ---- multi-timeframe: closed coarse bars only, mirrored direction ----

def coarse(levels, freq, start, end):
    index = pd.date_range(start, end, freq=freq)
    closes = np.array([levels.get(ts.isoformat(), 100.) for ts in index], dtype='float64')
    return pd.DataFrame(dict(Open=closes, High=closes + 1, Low=closes - 1, Close=closes, Volume=100.), index=index)


def test_higher_timeframe_short_uses_closed_bar_and_mirrors():
    # 4h coarse closes: ... 100, 16:00 => 110 (closes 20:00), 20:00 => 90 (closes 00:00), 00:00 => 0.01 (unclosed).
    # EMA2 short: at 110 => 110 > 106 2/3 fails; at 90 => 90 < 95 5/9 passes.
    source = coarse({'2026-01-02T16:00:00': 110., '2026-01-02T20:00:00': 90., '2026-01-03T00:00:00': 0.01},
                    '4h', '2026-01-01', '2026-01-03 04:00')
    opens = pd.date_range('2026-01-02 19:00', periods=24, freq='15min')
    config = FilterConfig.parse(ema_on(2, filter_timeframe='4h'))
    short = HigherTimeframeContext.build(config, '15m', opens, lambda *a: source.copy(), p._supertrend_arrays,
                                         direction='short').mask
    long = HigherTimeframeContext.build(config, '15m', opens, lambda *a: source.copy(), p._supertrend_arrays).mask
    at = {ts.strftime('%H:%M'): i for i, ts in enumerate(opens)}
    # Decision 20:00 and 23:45 see the 16:00 bar (110); decision 00:00 first sees the 20:00 bar (90).
    assert [bool(short[at[k]]) for k in ('19:45', '23:30', '23:45', '00:30')] == [False, False, True, True]
    assert [bool(long[at[k]]) for k in ('19:45', '23:30', '23:45', '00:30')] == [True, True, False, False]
    assert not short[at['19:00']] and not short[at['19:30']]  # decisions before 20:00 see the 12:00 bar (flat 100)


# ---- off-state: five short templates byte-identical to 1d66844 ----

@pytest.mark.parametrize('case', sorted(off4.cases()))
@pytest.mark.parametrize('explicit_defaults', [False, True])
def test_short_templates_off_state_byte_identical_to_base(case, explicit_defaults, monkeypatch, tmp_path):
    monkeypatch.setenv('CUTIE_BACKTEST_CHAN_DEBUG', '1')  # CHANSLIM: frozen bytes predate the slim chan evidence
    tool, params = off4.cases()[case]
    if explicit_defaults:
        params = {**params, **DEFAULTS}
    monkeypatch.setattr(p, 'entry_mask', lambda *a, **k: pytest.fail('disabled filter performed indicator work'))
    assert off4.snapshot(monkeypatch, tmp_path, tool, params) == GOLDEN['cases'][case]


def test_short_off_golden_covers_every_capture_case():
    assert GOLDEN['baseline_sha'] == '1d66844'
    assert set(GOLDEN['cases']) == set(off4.cases())
    assert {case.split('/')[0] for case in GOLDEN['cases']} == set(SHORT)
    assert all(json.loads(v)['trades'] for k, v in GOLDEN['cases'].items() if k.endswith('/default'))


# ---- on-state: five short templates, judgment bar = signal close, entry at the next open ----

def direct(name, data, params, prefix=None):
    return direct2(name, data, {**c.CASES[name][1], **params}, prefix)


SIGNAL = {'macd_bearish_divergence': 23, 'rsi_bearish_divergence': 23, 'double_top': 53, 'head_shoulders': 53,
          'chan_3sell': 12}


@pytest.mark.parametrize('name', SHORT)
def test_short_template_filter_true_enters_next_open(name):
    data = c.CASES[name][0]()
    signal, closes = SIGNAL[name], data.Close.tolist()
    assert direct(name, data, {})['_trades'].EntryBar.tolist() == [signal + 1]
    assert short_judged(closes, signal, 10)
    stats = direct(name, data, ema_on(10))
    assert stats['_trades'].EntryBar.tolist() == [signal + 1]
    assert stats['_trades'].EntryPrice.tolist() == [data.Open.iloc[signal + 1]]
    assert stats['_trades'].Size.iloc[0] < 0


@pytest.mark.parametrize('name', ['macd_bearish_divergence', 'rsi_bearish_divergence'])
def test_divergence_filter_false_discards_signal(name):
    # Bar 23 closes 106 after a 111 high: EMA20 over the frame stays above 106 (not warmup: 23 >= 19).
    data = c.CASES[name][0]()
    assert not short_judged(data.Close.tolist(), 23, 20)
    assert direct(name, data, ema_on(20))['_trades'].empty


@pytest.mark.parametrize('name', ['double_top', 'head_shoulders'])
def test_pattern_filter_false_discards_signal_without_replay(name):
    # A flat 10 prefix keeps EMA100 far below the signal close: close > EMA, the short is filtered.
    data = c.CASES[name][0]()
    signal = SIGNAL[name]
    prefix = flat_prefix(10., 100, data.index[0], '1h')
    assert direct(name, data, {}, prefix)['_trades'].EntryBar.tolist() == [signal + 1]
    closes = prefix.Close.tolist() + data.Close.tolist()
    assert not short_judged(closes, 100 + signal, 100)
    assert direct(name, data, ema_on(100), prefix)['_trades'].empty


@pytest.mark.parametrize('closed_close,entries', [(200., []), (50., [13])])
def test_chan_3sell_filter_judged_at_signal_close(closed_close, entries):
    # Chan's recognizer reads High/Low only and its falling closes pass any EMA period, so the judgment
    # comes from a 4h context: signal bar 12 (decision 13:00) sees the 08:00 bar (closes 12:00).
    # After flat 100s EMA2 = 166 2/3 at 200 (close > EMA, filtered) or 66 2/3 at 50 (passes).
    # Every other 4h bar is flat 100: close == EMA, which blocks the short.
    data = c.CASES['chan_3sell'][0]()
    source = coarse({'2026-01-01T08:00:00': closed_close}, '4h', '2025-12-31', '2026-01-02')
    params = ema_on(2, filter_timeframe='4h')
    cls = p.TOOL_SPECS['local.backtesting_py.chan_3sell']['build'](params)['strategy']
    cls._filter_context = HigherTimeframeContext.build(cls._filter_config, '1h', data.index,
                                                       lambda *a: source.copy(), p._supertrend_arrays,
                                                       direction=cls._filter_direction)
    stats = Backtest(data, cls, cash=100000, commission=0, exclusive_orders=True, finalize_trades=True).run()
    assert stats['_trades'].EntryBar.tolist() == entries


# ---- ORB short branch: the first closed breakdown is the judgment bar ----

def orb_short_frame(count=16, signal=4):
    """Mirror of off2.range_frame: range 90..110; first close below 90 at `signal`."""
    rows = [[100., 110., 90., 100., 1.] for _ in range(count)]
    for i in range(signal, count):
        rows[i] = [88., 89., 87., 88., 1.]
    rows[signal] = [100., 100., 88., 89., 1.]
    return pd.DataFrame(rows, columns=['Open', 'High', 'Low', 'Close', 'Volume'],
                        index=pd.date_range('2026-01-01T00:00', periods=count, freq='15min'))


SHORT_ORB = {'direction': 'short'}


def test_orb_short_filter_false_consumes_cycle_and_is_recorded():
    data = orb_short_frame()
    prefix = flat_prefix(50., 10, data.index[0], '15min')
    off = direct2('opening_range_breakout', data, SHORT_ORB, prefix, '15m')
    assert off['_trades'].EntryBar.tolist()[:1] == [5]
    assert all('filter_blocked' not in day for day in off['_strategy'].daily_ranges.values())
    closes = prefix.Close.tolist() + data.Close.tolist()
    # EMA10 from 50 through four 100 closes ~ 77.6, then 89 => ~79.7: 89 > EMA, filtered.
    assert not short_judged(closes, 10 + 4, 10)
    # Bar 5 is still a breakdown and passes the short filter; the cycle is already consumed.
    data.iloc[5] = [88., 89., 0.5, 1., 1.]
    closes = prefix.Close.tolist() + data.Close.tolist()
    assert short_judged(closes, 10 + 5, 10)
    stats = direct2('opening_range_breakout', data, {**SHORT_ORB, **ema_on(10)}, prefix, '15m')
    assert stats['_trades'].empty
    days = list(stats['_strategy'].daily_ranges.values())
    assert days[0]['filter_blocked'] is True
    assert days[0]['filter_blocked_decision_utc'] == '2026-01-01T01:15:00+00:00'


def test_orb_short_filter_true_enters_next_open():
    data = orb_short_frame()
    prefix = flat_prefix(300., 10, data.index[0], '15min')
    assert short_judged(prefix.Close.tolist() + data.Close.tolist(), 10 + 4, 10)
    stats = direct2('opening_range_breakout', data, {**SHORT_ORB, **ema_on(10)}, prefix, '15m')
    trades = stats['_trades']
    assert trades.EntryBar.tolist()[:1] == [5] and trades.EntryPrice.tolist()[:1] == [88.]
    assert trades.Size.iloc[0] < 0
    assert all('filter_blocked' not in day for day in stats['_strategy'].daily_ranges.values())


def test_orb_long_filter_block_is_recorded_too():
    data = off2.range_frame()
    prefix = flat_prefix(300., 10, data.index[0], '15min')
    stats = direct2('opening_range_breakout', data, ema_on(10), prefix, '15m')
    assert stats['_trades'].empty
    day = list(stats['_strategy'].daily_ranges.values())[0]
    assert day['filter_blocked'] is True and day['filter_blocked_decision_utc'] == '2026-01-01T01:15:00+00:00'


# ---- HTTP: F1/F2 under filter_timeframe; coarse close <= primary judgment close ----

def http(tool, params, data, timeframe, market, source):
    with ExitStack() as stack, tempfile.TemporaryDirectory() as reports:
        stack.enter_context(patch.object(p, 'AUTH_TOKEN', ''))
        stack.enter_context(patch.object(p, 'REPORTS_DIR', Path(reports)))
        stack.enter_context(patch.object(p, '_fetch_ohlcv', lambda ex, mk, sym, tf, since, until:
                                         (data if tf == timeframe else source).copy()))
        stack.enter_context(patch.object(p, '_fetch_template_warmup', lambda *a, **k: data.iloc[:0].copy()))
        stack.enter_context(patch.object(Backtest, 'plot', lambda *a, **kw: None))
        step = int((data.index[1] - data.index[0]).total_seconds())
        request = dict(run_id='7p4', provider_tool_id='local.backtesting_py.' + tool, provider_params=params,
                       symbol='BTCUSDT', market=market, timeframe=timeframe, start_at=int(data.index[0].timestamp()),
                       end_at=int(data.index[-1].timestamp()) + step, initial_capital='10000', fee_bps='0',
                       slippage_bps='0')
        return TestClient(p.app).post('/cutie/backtest', json={'backtest': request}).json()


PASS_LONG, PASS_SHORT = 200., 50.  # after flat 100s, EMA2 = 166 2/3 or 66 2/3
# case -> (tool, params, frame, primary, market, filter tf, closed coarse open, unclosed coarse open, pass level)
MTF = {
    'orb_long': ('opening_range_breakout', {}, off2.range_frame, '15m', 'futures', '1h',
                 '2026-01-01T00:00:00', '2026-01-01T01:00:00', PASS_LONG),
    'orb_short': ('opening_range_breakout', SHORT_ORB, orb_short_frame, '15m', 'futures', '1h',
                  '2026-01-01T00:00:00', '2026-01-01T01:00:00', PASS_SHORT),
    'asia_long': ('asia_range_breakout', {}, off2.asia_frame, '15m', 'futures', '1h',
                  '2026-01-01T06:00:00', '2026-01-01T07:00:00', PASS_LONG),
    # Calendar bar-0 event: decision 01:00 sees the 4h bar opened 20:00 the day before, not the 00:00 one.
    'calendar_bar0': ('calendar_schedule', dict(time_entry_at='01:00', time_max_holding_minutes=60),
                      off2.calendar_frame, '1h', 'spot', '4h', '2025-12-31T20:00:00', '2026-01-01T00:00:00',
                      PASS_LONG),
}


def fail_level(level):
    return PASS_SHORT if level == PASS_LONG else PASS_LONG


@pytest.mark.parametrize('case', sorted(MTF))
@pytest.mark.parametrize('closed_passes', [True, False])
def test_http_filter_timeframe_uses_closed_coarse_bar(case, closed_passes):
    tool, params, make, primary, market, tf, closed, unclosed, level = MTF[case]
    levels = {closed: level, unclosed: fail_level(level)} if closed_passes else \
        {closed: fail_level(level), unclosed: level}
    source = coarse({pd.Timestamp(k).isoformat(): v for k, v in levels.items()}, tf, '2025-12-30', '2026-01-03')
    body = http(tool, {**params, **ema_on(2, filter_timeframe=tf)}, make(), primary, market, source)
    assert body['result_status'] == 'success', body
    report = body['raw_report']['entry_filters']
    assert report['timeframe'] == tf and report['clock'] == 'higher_bar_close_lte_primary_bar_close'
    assert report['direction'] == params.get('direction', 'long')
    if tool == 'calendar_schedule':
        first = body['raw_report']['calendar_events'][0]
        assert first['event_utc'] == '2026-01-01T01:00:00+00:00'
        if closed_passes:
            assert first['status'] != 'skipped' and first['reason'] is None and body['trades']
        else:
            assert (first['status'], first['reason']) == ('skipped', 'entry_filter')
    else:
        first = body['raw_report']['range_breakout_days'][0]
        assert bool(body['trades']) is closed_passes
        assert first.get('filter_blocked', False) is (not closed_passes)


# ---- generic templates: short allowed with the mirrored rule, both still rejected ----

GENERIC = {
    'breakout': {}, 'cci_rsi': {}, 'ema_pullback': {}, 'us_open_momentum': {}, 'cme_weekend_gap': {},
    'opening_range_breakout': {}, 'asia_range_breakout': {},
    'calendar_schedule': dict(time_entry_at='02:00', time_max_holding_minutes=120),
}


@pytest.mark.parametrize('name', sorted(GENERIC))
def test_generic_short_with_filter_builds_as_short(name):
    build = p.TOOL_SPECS['local.backtesting_py.' + name]['build']
    cls = build({**GENERIC[name], 'direction': 'short', **ema_on(5)})['strategy']
    assert cls._filter_direction == 'short' and cls._filter_config.ema_period == 5
    assert build({**GENERIC[name], 'direction': 'long', **ema_on(5)})['strategy']._filter_direction == 'long'
    if 'both' in p.TOOL_SPECS['local.backtesting_py.' + name]['param_schema_properties']['direction'].get('enum', []):
        with pytest.raises(ValueError, match='INVALID_PARAMS:entry filters support long or short; both'):
            build({**GENERIC[name], 'direction': 'both', **ema_on(5)})


@pytest.mark.parametrize('name', ['cci_rsi', 'us_open_momentum', 'cme_weekend_gap'])
def test_default_both_with_filter_still_rejected(name):
    with pytest.raises(ValueError, match='both is not supported'):
        p.TOOL_SPECS['local.backtesting_py.' + name]['build'](ema_on(5))


def test_ema_pullback_short_end_to_end_mirrored_filter():
    # Off: short signals at 29 and 36 (fills 30, 37). Independent EMA2: 29 passes (close < EMA), 36 fails.
    import test_ema_pullback_short as ep
    f = ep.fixture()
    closes = f['closes']
    assert short_judged(closes, 29, 2) and not short_judged(closes, 36, 2)
    off = ep.run(closes, f['highs'], f['lows'])['_trades'].EntryBar.tolist()
    assert off == [30, 37]
    on = ep.run(closes, f['highs'], f['lows'], {**ep.PARAMS, **ema_on(2)})['_trades'].EntryBar.tolist()
    assert 30 in on and 37 not in on


# ---- spot gates unchanged: allowing short does not open spot short ----

SPOT = [('macd_bearish_divergence', {}), ('rsi_bearish_divergence', {}), ('double_top', {}),
        ('head_shoulders', {}), ('chan_3sell', {}), ('breakout', {'direction': 'short'}),
        ('opening_range_breakout', {'direction': 'short'}),
        ('calendar_schedule', dict(direction='short', time_entry_at='02:00', time_max_holding_minutes=120))]


@pytest.mark.parametrize('name,params', SPOT)
def test_spot_short_with_filter_rejected_before_fetch(name, params, monkeypatch):
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a, **k: pytest.fail('spot short fetched data'))
    request = dict(run_id='7p4', provider_tool_id='local.backtesting_py.' + name,
                   provider_params={**params, **ema_on(5)}, symbol='BTCUSDT', market='spot', timeframe='1h',
                   start_at=1767225600, end_at=1767225600 + 86400, initial_capital='10000', fee_bps='0',
                   slippage_bps='0')
    body = TestClient(p.app).post('/cutie/backtest', json={'backtest': request}).json()
    assert body['error_type'] == 'INVALID_PARAMS', body
