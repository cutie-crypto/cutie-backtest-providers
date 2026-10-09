"""Independent OHLC arithmetic, mirrored fixtures and causal position filters."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from strategy_candle_patterns import PatternConfig, candle_geometry, candle_patterns

BASE = [(100, 101.5, 99.5, 101)] * 20  # body=1, range=2; independent hand fixture


def geometry(rows, **kwargs):
    return candle_geometry(*zip(*rows), **kwargs)


def reflected(rows):
    # Price reflection independently mirrors every bullish fixture.
    return [(200 - o, 200 - l, 200 - h, 200 - c) for o, h, l, c in rows]


def test_geometry_arithmetic_and_prior_only_means():
    g = geometry(BASE + [(100, 103, 99, 102)])
    assert g.body[-1] == 2
    assert g.span[-1] == 4
    assert g.upper[-1] == g.lower[-1] == 1
    assert g.mean_body == (None,) * 20 + (1,)
    assert g.mean_span == (None,) * 20 + (2,)


@pytest.mark.parametrize('body,large,small', [(1.5, True, False), (1.49, False, False),
    (.5, False, True), (.51, False, False), (0, False, True)])
def test_large_small_hand_calculated_boundaries(body, large, small):
    result = candle_patterns('inside_bar', geometry(BASE + [(100, 102, 99, 100 + body)]))
    assert result.large_body[-1] is large  # thresholds 1 * 1.5 and 1 * .5
    assert result.small_body[-1] is small
    assert not any(result.large_body[:20]) and not any(result.small_body[:20])


CASES = [
    ('engulfing', [(102, 102.5, 99.8, 100), (100, 103, 99.7, 102.5)], 99.7),
    ('pin_bar', [(102, 103.5, 98, 103)], 98),
    ('star', [(104, 104.5, 99.5, 100), (99.5, 100, 99, 99.2), (99.2, 103.5, 99, 103)], 99),
    ('soldiers', [(90, 92.5, 89, 92), (91, 93.5, 90, 93), (92, 94.5, 91, 94)], 89),
    ('doji', [(100, 101, 99, 100.125)], 99),
    ('inside_bar', [(100, 105, 95, 102), (101, 104, 96, 102)], 95),
]


@pytest.mark.parametrize('kind,suffix,anchor', CASES)
@pytest.mark.parametrize('mirror', [False, True])
def test_each_family_manual_recognition_position_and_anchor(kind, suffix, anchor, mirror):
    prefix = BASE[:-1] + [(91, 91.5, 88.5, 89)] if kind == 'soldiers' else BASE
    rows = prefix + suffix
    if mirror:
        rows = reflected(rows)
    lines = {'rsi': [80 if mirror else 20] * len(rows)} if kind == 'doji' else None
    r = candle_patterns(kind, geometry(rows), indicators=lines)
    shape = r.bearish_shape if mirror else r.bullish_shape
    signal = r.bearish if mirror else r.bullish
    anchors = r.bearish_anchor if mirror else r.bullish_anchor
    assert shape[-1] is True
    assert signal[-1] is True
    assert anchors[-1] == pytest.approx(200 - anchor if mirror else anchor)
    if kind not in ('inside_bar', 'doji'):
        opposite = r.bullish_shape if mirror else r.bearish_shape
        assert opposite[-1] is False


@pytest.mark.parametrize('kind,suffix,_anchor', CASES)
def test_pattern_outputs_are_identical_on_every_prefix(kind, suffix, _anchor):
    prefix = BASE[:-1] + [(91, 91.5, 88.5, 89)] if kind == 'soldiers' else BASE
    rows = prefix + suffix + [(100, 150, 50, 101)] * 3
    full = candle_patterns(kind, geometry(rows), indicators={'rsi': [20] * len(rows)})
    for length in range(1, len(rows) + 1):
        prefix = candle_patterns(kind, geometry(rows[:length]), indicators={'rsi': [20] * length})
        for name in full.__dataclass_fields__:
            assert getattr(prefix, name) == getattr(full, name)[:length]


def test_engulf_ratio_rejects_full_body_containment_without_required_size():
    rows = BASE + [(102, 103, 99, 100), (100, 103, 99, 102)]
    assert candle_patterns('engulfing', geometry(rows)).bullish_shape[-1] is False


def test_engulf_body_equality_at_one_point_two_is_inclusive():
    # Previous body=5, current body=6: 6 == 5*1.2, and full containment.
    rows = BASE + [(105, 106, 99, 100), (100, 107, 99, 106)]
    assert candle_patterns('engulfing', geometry(rows)).bullish[-1] is True


def test_engulf_location_can_fail_or_be_supplied_by_bollinger_touch():
    rows = BASE + [(120, 121, 117, 118), (118, 121.5, 117, 121)]
    g = geometry(rows)
    absent = candle_patterns('engulfing', g)
    assert absent.bullish_shape[-1] is True and absent.bullish[-1] is False
    assert absent.bullish_anchor[-1] is None
    supplied = candle_patterns('engulfing', g, indicators={'bb_lower': [None] * 21 + [118]})
    assert supplied.bullish[-1] is True and supplied.bullish_anchor[-1] == 117
    outside = candle_patterns('engulfing', g, indicators={'bb_lower': [None] * 21 + [116]})
    assert outside.bullish[-1] is False


def test_pin_equal_previous_low_requires_ema_touch():
    rows = BASE + [(102, 103.2, 99.5, 103)]
    g = geometry(rows)
    assert candle_patterns('pin_bar', g).bullish_shape[-1] is True
    assert candle_patterns('pin_bar', g).bullish[-1] is False
    for name in ('ema20', 'ema60'):
        assert candle_patterns('pin_bar', g, indicators={name: [None] * 20 + [100]}).bullish[-1] is True


def test_pin_body_must_be_entirely_in_upper_third():
    # Body=3, lower=6, upper=1, range=10: wicks pass, but 102 < 106-10/3.
    g = geometry(BASE + [(102, 106, 96, 105)])
    assert candle_patterns('pin_bar', g).bullish_shape[-1] is False


@pytest.mark.parametrize('rows', [[(100, 100, 100, 100)], [(100, 102, 98, 100)]])
def test_zero_body_and_zero_span_are_not_pin_bars(rows):
    r = candle_patterns('pin_bar', geometry(BASE + rows))
    assert not r.bullish_shape[-1] and not r.bearish_shape[-1]


def test_star_small_middle_and_third_close_are_both_required():
    # First body=4; second body=1: <=4*.3, but >previous mean(1.15)*.5.
    suffix = [(104, 104.5, 99.5, 100), (99.5, 100, 98, 98.5), (99, 103.5, 98, 103)]
    assert not candle_patterns('star', geometry(BASE + suffix)).bullish_shape[-1]
    suffix = CASES[2][1][:-1] + [(99.2, 103, 99, 102)]  # equals first midpoint
    assert not candle_patterns('star', geometry(BASE + suffix)).bullish_shape[-1]


def test_soldiers_need_prior_downtrend_even_when_shape_is_valid():
    suffix = [(110, 112.5, 109, 112), (111, 113.5, 110, 113), (112, 114.5, 111, 114)]
    r = candle_patterns('soldiers', geometry(BASE[:-1] + [(111, 111.5, 108.5, 109)] + suffix))
    assert r.bullish_shape[-1] and not r.bullish[-1]


@pytest.mark.parametrize('rsi,bull,bear', [(29, True, False), (30, False, False), (70, False, False),
    (71, False, True), (None, False, False), (float('nan'), False, False)])
def test_doji_rsi_filter_strict_thresholds(rsi, bull, bear):
    rows = BASE + CASES[4][1]
    r = candle_patterns('doji', geometry(rows), indicators={'rsi': [None] * 20 + [rsi]})
    assert r.bullish_shape[-1] and r.bearish_shape[-1]
    assert r.bullish[-1] is bull and r.bearish[-1] is bear


def test_doji_rejects_tiny_range_and_zero_range():
    for row in [(100, 100.5, 99.5, 100), (100, 100, 100, 100)]:
        assert not candle_patterns('doji', geometry(BASE + [row])).bullish_shape[-1]


@pytest.mark.parametrize('row', [(101, 105, 96, 102), (101, 104, 95, 102)])
def test_inside_bar_equality_is_not_containment(row):
    r = candle_patterns('inside_bar', geometry(BASE + [(100, 105, 95, 102), row]))
    assert not r.bullish_shape[-1] and not r.bearish_shape[-1]


@pytest.mark.parametrize('kind', ['star', 'soldiers', 'doji'])
def test_required_history_does_not_become_implicit_confirmation(kind):
    r = candle_patterns(kind, geometry([(100, 101.5, 99.5, 101)]))
    assert not r.bullish[-1] and not r.bearish[-1]


def test_custom_config_changes_only_requested_threshold():
    g = geometry(BASE + [(102, 103, 99, 100), (100, 103, 99, 102)])
    assert not candle_patterns('engulfing', g).bullish_shape[-1]
    assert candle_patterns('engulfing', g, config=PatternConfig(engulf_body_multiple=1)).bullish[-1]


@pytest.mark.parametrize('lookback', [0, -1, 501, True, 20.0, '20', None])
def test_invalid_lookback(lookback):
    with pytest.raises(ValueError, match='INVALID_PARAMS:'):
        geometry(BASE, lookback=lookback)


def test_lookback_boundaries_and_empty_geometry():
    assert geometry(BASE, lookback=1).mean_body[:2] == (None, 1)
    assert geometry(BASE, lookback=500).mean_body == (None,) * 20
    g = candle_geometry([], [], [], [])
    assert candle_patterns('inside_bar', g).bullish == ()


@pytest.mark.parametrize('rows', [[(100, 99, 98, 100)], [(100, 102, 101, 102)],
    [(100, float('inf'), 99, 101)], [(float('nan'), 102, 99, 101)],
    [(True, 102, 99, 101)], [('100', 102, 99, 101)], [(0, 1, 0, 1)]])
def test_invalid_ohlc(rows):
    with pytest.raises(ValueError, match='INVALID_PARAMS:'):
        geometry(rows)


def test_mismatched_ohlc_lengths():
    with pytest.raises(ValueError, match='INVALID_PARAMS:'):
        candle_geometry([100], [102, 103], [99], [101])


@pytest.mark.parametrize('kwargs', [dict(large_body_multiple=0), dict(small_body_multiple=2),
    dict(pin_body_zone_fraction=1.1), dict(doji_body_fraction=True), dict(engulf_body_multiple=float('nan'))])
def test_invalid_config(kwargs):
    with pytest.raises(ValueError, match='INVALID_PARAMS:'):
        PatternConfig(**kwargs)


@pytest.mark.parametrize('kwargs', [dict(indicators={'rsi': [20]}), dict(indicators={'rsi': [float('inf')] * 20}),
    dict(indicators={'future': [20] * 20}), dict(config={})])
def test_invalid_pattern_inputs(kwargs):
    with pytest.raises(ValueError, match='INVALID_PARAMS:'):
        candle_patterns('doji', geometry(BASE), **kwargs)


def test_unknown_kind_rejected():
    with pytest.raises(ValueError, match='INVALID_PARAMS:'):
        candle_patterns('future', geometry(BASE))


def test_soldiers_first_open_must_be_inside_preceding_body():
    # All three bars rise with short upper shadows and a prior downtrend.
    # First opening price 90 is outside the preceding body [100,101].
    r = candle_patterns('soldiers', geometry(BASE + CASES[3][1]))
    assert not r.bullish_shape[-1]


def test_soldiers_first_close_must_exceed_preceding_close():
    # First open 90 is inside [89,93], but first close 92 does not beat 93.
    prefix = BASE[:-1] + [(89, 93.5, 88.5, 93)]
    r = candle_patterns('soldiers', geometry(prefix + CASES[3][1]))
    assert not r.bullish_shape[-1]


@pytest.mark.parametrize('mirror', [False, True])
def test_position_half_percent_boundary_is_inclusive(mirror):
    prefix = [(101, 105, 100, 102)] * 20
    rows = prefix + [(104, 105, 101, 102), (102, 105, 100.5, 104.5)]
    r = candle_patterns('engulfing', geometry(reflected(rows) if mirror else rows))
    # Lowest=100.5, reference=100, exactly .5%; mirror highest=99.5 vs 100.
    assert (r.bearish if mirror else r.bullish)[-1] is True


def test_pin_wick_ratio_and_body_zone_equality_are_inclusive():
    # Body=2, lower=4, upper=0, range=6; body begins exactly in upper third.
    r = candle_patterns('pin_bar', geometry(BASE + [(103, 105, 99, 105)]))
    assert r.bullish[-1] is True


def test_doji_span_equality_is_inclusive():
    prefix = [(100, 105, 95, 101)] * 20  # prior mean range=10
    r = candle_patterns('doji', geometry(prefix + [(100, 104, 96, 100)]), indicators={'rsi': [20] * 21})
    assert r.bullish[-1] is True  # range=8 ==10*.8, zero body, RSI<30


@pytest.mark.parametrize('close,small', [(100, True), (101, False)])
def test_zero_mean_is_known_history_not_missing_history(close, small):
    # Prior 20 bodies all zero; either current body is >= 0*1.5.
    prefix = [(100, 101, 99, 100)] * 20
    r = candle_patterns('inside_bar', geometry(prefix + [(100, 102, 99, close)]))
    assert r.large_body[-1] is True
    assert r.small_body[-1] is small
