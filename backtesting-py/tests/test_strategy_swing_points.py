"""Hand-counted swing examples and confirmation-prefix invariants."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from strategy_swing_points import SwingPoint, confirmed_points, swing_points


def test_five_right_bars_must_close_before_point_is_visible():
    # Index 5 is below all ten neighbours; its confirmation is index 10.
    low = [10, 9, 8, 7, 6, 1, 2, 3, 4, 5, 6, 7]
    high = [20] * len(low)
    series = swing_points(high, low)
    assert series.lows == (None,) * 10 + (SwingPoint(5, 10, 1), None)
    assert confirmed_points(series, as_of=9, kind='low') == ()
    assert confirmed_points(series, as_of=10, kind='low') == (SwingPoint(5, 10, 1),)
    assert swing_points(high[:10], low[:10]).lows == (None,) * 10


@pytest.mark.parametrize('as_of', range(12))
def test_future_edits_and_appends_cannot_change_confirmed_prefix(as_of):
    low = [10, 9, 8, 7, 6, 1, 2, 3, 4, 5, 6, 7]
    high = [20] * len(low)
    full = swing_points(high, low)
    prefix = swing_points(high[:as_of + 1], low[:as_of + 1])
    # Change i+1..i+N AND append extra extremes, without touching <=i.
    changed = swing_points(high[:as_of + 1] + [50] * 8, low[:as_of + 1] + [.5] * 8)
    for candidate in (prefix, changed):
        assert candidate.lows[:as_of + 1] == full.lows[:as_of + 1]
        assert candidate.highs[:as_of + 1] == full.highs[:as_of + 1]
        for kind in ('low', 'high'):
            assert confirmed_points(candidate, as_of=as_of, kind=kind) == confirmed_points(full, as_of=as_of, kind=kind)


@pytest.mark.parametrize('low', [[3, 1, 1], [1, 1, 3]])
def test_equal_left_or_right_neighbour_is_not_a_low(low):
    assert swing_points([5] * 3, low, n=1).lows == (None,) * 3


@pytest.mark.parametrize('high', [[1, 3, 3], [3, 3, 1]])
def test_equal_left_or_right_neighbour_is_not_a_high(high):
    assert swing_points(high, [.5] * 3, n=1).highs == (None,) * 3


def test_minimum_n_and_chronological_count_and_price_index_pairing():
    series = swing_points([6, 7, 6, 7, 6, 7, 6], [4, 1, 4, 2, 4, 3, 4], n=1)
    assert confirmed_points(series, as_of=6, kind='low', count=2) == (SwingPoint(3, 4, 2), SwingPoint(5, 6, 3))
    assert confirmed_points(series, as_of=6, kind='high', count=1) == (SwingPoint(5, 6, 7),)
    assert confirmed_points(series, as_of=3, kind='low') == (SwingPoint(1, 2, 1),)


def test_maximum_n_has_exact_confirmation_and_empty_input_is_valid():
    low = [2] * 500 + [1] + [2] * 500
    points = swing_points([3] * 1001, low, n=500)
    assert points.lows[:-1] == (None,) * 1000
    assert points.lows[-1] == SwingPoint(500, 1000, 1)
    assert swing_points([], []).lows == ()


@pytest.mark.parametrize('n', [0, -1, 501, True, False, 1.0, 5.5, '5', None])
def test_invalid_n(n):
    with pytest.raises(ValueError, match='INVALID_PARAMS:'):
        swing_points([3], [1], n=n)


@pytest.mark.parametrize('high,low', [([3], []), ([1], [2]), ([float('nan')], [1]),
    ([3], [float('inf')]), ([0], [0]), ([True], [1]), (['3'], [1])])
def test_invalid_prices(high, low):
    with pytest.raises(ValueError, match='INVALID_PARAMS:'):
        swing_points(high, low)


@pytest.mark.parametrize('kwargs', [dict(as_of=-1, kind='low'), dict(as_of=3, kind='low'),
    dict(as_of=True, kind='low'), dict(as_of=1.0, kind='low'), dict(as_of=2, kind='bottom'),
    dict(as_of=2, kind='low', count=0), dict(as_of=2, kind='low', count=True),
    dict(as_of=2, kind='low', count=1.0)])
def test_invalid_accessor_parameters(kwargs):
    with pytest.raises(ValueError, match='INVALID_PARAMS:'):
        confirmed_points(swing_points([3] * 3, [2, 1, 2], n=1), **kwargs)
