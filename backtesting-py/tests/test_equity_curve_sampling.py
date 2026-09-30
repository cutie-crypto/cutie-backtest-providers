"""0930 按市值资金曲线按段采样（strategy_kernel.sample_equity_curve，v2 与 v3 共用）。

拍板口径：按市值点超过 3000 时按段等距采样，每段保留局部最低点和最高点，起点与
closed_at 点全保留，max_drawdown 按采样后的曲线算。只留每段 max/min 在「峰和谷同段、
且该段最高点在最低点之后」时会丢掉回撤对，所以采样函数另外保留全量曲线的最大回撤
峰点和谷点；下面的用例证明采样后回撤与全量曲线严格相等。
"""
from __future__ import annotations

import random
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cutie_backtesting_provider as provider  # noqa: E402
from strategy_kernel import (  # noqa: E402
    MTM_POINT_LIMIT,
    canonical_decimal_str,
    _equity_curve_with_mtm,
    _max_drawdown,
    sample_equity_curve,
)


def _random_walk_curve(seed: int, mtm_count: int = 20000, closed_every: int = 997):
    rng = random.Random(seed)
    curve = [{"ts": 0, "equity": "10000"}]
    closed_ts: set[int] = set()
    equity = Decimal("10000")
    for step in range(1, mtm_count + 1):
        equity = max(Decimal("1"), equity + Decimal(str(round(rng.gauss(0, 25), 2))))
        curve.append({"ts": step * 900, "equity": str(equity)})
        if step % closed_every == 0:
            closed_ts.add(step * 900)
    return curve, closed_ts


def _is_ordered_subset(sub: list[dict], full: list[dict]) -> bool:
    it = iter(full)
    return all(any(point == other for other in it) for point in sub)


@pytest.mark.parametrize("seed", range(20))
def test_sampled_drawdown_equals_full_drawdown_on_random_walks(seed):
    curve, closed_ts = _random_walk_curve(seed)
    sampled = sample_equity_curve(curve, closed_ts)

    mtm_kept = [p for p in sampled[1:] if p["ts"] not in closed_ts]
    assert len(mtm_kept) <= MTM_POINT_LIMIT
    assert sampled[0] == curve[0]
    assert {p["ts"] for p in sampled} >= closed_ts
    assert _is_ordered_subset(sampled, curve)
    ts = [p["ts"] for p in sampled]
    assert all(b > a for a, b in zip(ts, ts[1:]))
    # 两条路径的回撤函数都按采样曲线算，与全量曲线逐值相等。
    assert _max_drawdown(sampled) == _max_drawdown(curve)
    assert provider._result_v2_max_drawdown(sampled) == provider._result_v2_max_drawdown(curve)


def test_peak_and_trough_in_one_segment_with_max_after_min_keep_the_drawdown_pair():
    """limit=6 -> 2 段。第一段 100、80、95、110：段内最低 80 在最高 110 之前，回撤对
    100 -> 80（20%）只靠段 max/min 会丢，只剩 110 -> 104；补回回撤对后保留 100。"""
    equities = ["50", "100", "80", "95", "110", "105", "108", "104", "107"]
    curve = [{"ts": i * 60, "equity": e} for i, e in enumerate(equities)]
    sampled = sample_equity_curve(curve, set(), limit=6)

    assert [p["equity"] for p in sampled] == ["50", "100", "80", "110", "108", "104"]
    assert _max_drawdown(sampled) == _max_drawdown(curve) == Decimal("0.2")


def test_curve_within_limit_is_returned_unchanged():
    curve, closed_ts = _random_walk_curve(1, mtm_count=MTM_POINT_LIMIT + 3, closed_every=1000)
    assert len([p for p in curve[1:] if p["ts"] not in closed_ts]) == MTM_POINT_LIMIT
    assert sample_equity_curve(curve, closed_ts) is curve


def test_v3_equity_curve_with_mtm_samples_long_holding():
    rng = random.Random(3)
    trades = [{"closed_at": 5001 * 900, "pnl": "-12.5"}]
    mtm_points = [
        (step * 900, 0, Decimal(str(round(rng.gauss(0, 40), 2)))) for step in range(1, 5001)
    ]
    curve, realized = _equity_curve_with_mtm(0, Decimal("10000"), trades, mtm_points)

    assert realized == Decimal("9987.5")
    assert curve[-1] == {"ts": 5001 * 900, "equity": "9987.5"}
    assert len(curve) - 2 <= MTM_POINT_LIMIT
    full = [{"ts": 0, "equity": "10000"}] + [
        {"ts": ts, "equity": canonical_decimal_str(Decimal("10000") + u)} for ts, _, u in mtm_points
    ] + [curve[-1]]
    assert _is_ordered_subset(curve, full)
    assert _max_drawdown(curve) == _max_drawdown(full)


def test_v2_equity_curve_samples_long_holding(monkeypatch):
    rng = random.Random(5)
    bars = []
    close = Decimal("100")
    for i in range(6000):
        close = max(Decimal("1"), close + Decimal(str(round(rng.gauss(0, 0.8), 1))))
        bars.append((i * 3600, (i + 1) * 3600, close))
    trades = [{
        "seq": 1, "opened_at": 3600, "closed_at": 5999 * 3600, "side": "long", "qty": "10",
        "entry_price": "100", "exit_price": str(bars[5999][2]), "fee": "0", "slippage": "0",
        "pnl": str((bars[5999][2] - Decimal("100")) * 10),
    }]
    kwargs = dict(bars=bars, fee_bps=Decimal(10), slippage_bps=Decimal(5))
    sampled = provider._build_result_v2_equity_curve(trades, Decimal("10000"), 0, **kwargs)
    monkeypatch.setattr(provider, "sample_equity_curve", lambda curve, closed_ts: curve)
    full = provider._build_result_v2_equity_curve(trades, Decimal("10000"), 0, **kwargs)

    assert len(full) - 2 > MTM_POINT_LIMIT
    assert len(sampled) - 2 <= MTM_POINT_LIMIT
    assert sampled[-1] == full[-1] == {"ts": 5999 * 3600, "equity": canonical_decimal_str(Decimal("10000") + Decimal(trades[0]["pnl"]))}
    assert _is_ordered_subset(sampled, full)
    assert provider._result_v2_max_drawdown(sampled) == provider._result_v2_max_drawdown(full)
