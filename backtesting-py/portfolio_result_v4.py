"""独立 v4 组装；BTC 基准价格、price manifests、冻结 BTC.D 由调用方注入。"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from fractions import Fraction
from typing import Mapping, Sequence

from canonical_json import canonical_json_sha256
from portfolio_ledger import decimal_text, decimal_value, reconcile_snapshots, timestamp

SCHEMA = "cutie.backtest_result.v4"
PRICE_KEYS = {"source", "symbol", "market", "timeframe", "start_at", "end_at", "kline_count", "checksum_algo", "checksum"}


def _hash(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _ratio(a, b=1) -> str:
    """有理数最终量化 8 位 ROUND_HALF_EVEN，无 Decimal 循环除法中间舍入。"""
    ratio = Fraction(a) / Fraction(b)
    q, r = divmod(abs(ratio.numerator) * 100000000, ratio.denominator)
    if 2 * r > ratio.denominator or (2 * r == ratio.denominator and q % 2):
        q += 1
    digits = str(q).rjust(9, "0")
    return decimal_text(Decimal(("-" if ratio < 0 and q else "") + digits[:-8] + "." + digits[-8:]))


def _metric_reference(series, start, end):
    if series is None:
        return None
    if set(series) != {"metric", "source", "unit", "points", "hash"} or (
        series["metric"], series["source"], series["unit"]
    ) != ("btc_dominance", "coingecko", "percent"):
        raise ValueError("invalid frozen BTC.D identity")
    points = series["points"]
    if not isinstance(points, list) or not 21 <= len(points) <= 4500:
        raise ValueError("invalid BTC.D coverage")
    for i, point in enumerate(points):
        if set(point) != {"ts", "available_at", "value"}:
            raise ValueError("invalid BTC.D point keys")
        timestamp(point["ts"])
        timestamp(point["available_at"])
        value = decimal_value(Decimal(point["value"]), "BTC.D")
        if not isinstance(point["value"], str) or decimal_text(value) != point["value"] or value > 100:
            raise ValueError("invalid BTC.D value")
        if point["ts"] % 86400 or point["available_at"] < point["ts"] or (i and point["ts"] != points[i-1]["ts"] + 86400):
            raise ValueError("invalid BTC.D point timeline")
    if points[0]["ts"] > start - 20 * 86400 or points[-1]["ts"] != end:
        raise ValueError("BTC.D needs 20 warmup days plus interval")
    expected = canonical_json_sha256({k: v for k, v in series.items() if k != "hash"})
    if series["hash"] != expected:
        raise ValueError("BTC.D frozen hash mismatch")
    return {"metric": series["metric"], "source": series["source"], "unit": series["unit"], "hash": expected,
            "start_at": points[0]["ts"], "end_at": points[-1]["ts"], "point_count": len(points)}


def assemble_result_v4(*, initial_cash: Decimal, snapshots: Sequence[dict], equity_curve: Sequence[dict],
                       fills: Sequence[dict], btc_benchmark: Sequence[dict], price_manifests: Sequence[dict],
                       metric_series: Mapping | None = None) -> dict:
    """接受规范字符串快照/成交和 BTC close_price 序列，核验后计算基准、指标与 manifests。"""
    initial_cash = decimal_value(initial_cash, "initial_cash", positive=True)
    snaps, curve, events, bench, manifests = deepcopy((list(snapshots), list(equity_curve), list(fills), list(btc_benchmark), list(price_manifests)))
    if not 1 <= len(snaps) <= 5000 or len(events) > 10000 or len(bench) != len(snaps) or len(curve) != len(snaps):
        raise ValueError("invalid v4 array lengths")
    symbols = None
    for i, (s, c, b) in enumerate(zip(snaps, curve, bench)):
        if set(s) != {"ts", "cash", "positions"} or set(c) != {"ts", "equity"} or set(b) != {"ts", "close_price"}:
            raise ValueError("invalid snapshot/curve/benchmark keys")
        ts = timestamp(s["ts"])
        timestamp(c["ts"])
        timestamp(b["ts"])
        if ts % 86400 or c["ts"] != ts or b["ts"] != ts or (i and ts != snaps[i-1]["ts"] + 86400):
            raise ValueError("snapshots must be consecutive daily labels")
        for obj, field, positive in ((s, "cash", False), (c, "equity", False), (b, "close_price", True)):
            _canonical_number(obj[field], field, positive=positive)
        if not isinstance(s["positions"], list) or not 1 <= len(s["positions"]) <= 30:
            raise ValueError("invalid position count")
        current = []
        for p in s["positions"]:
            if set(p) != {"symbol", "qty", "close_price"} or not isinstance(p["symbol"], str) or not p["symbol"]:
                raise ValueError("invalid position keys/identity")
            _canonical_number(p["qty"], "qty")
            _canonical_number(p["close_price"], "close_price", positive=True)
            current.append(p["symbol"])
            if p["symbol"] == "BTCUSDT" and p["close_price"] != b["close_price"]:
                raise ValueError("BTC position/benchmark price mismatch")
        if current != sorted(set(current)) or (symbols is not None and current != symbols):
            raise ValueError("position symbols must stay sorted and unchanged")
        symbols = current
    start, end = snaps[0]["ts"], snaps[-1]["ts"]
    if snaps[0]["cash"] != decimal_text(initial_cash) or any(p["qty"] != "0" for p in snaps[0]["positions"]):
        raise ValueError("initial point must be pretrade capital")
    for i, f in enumerate(events):
        if set(f) != {"seq", "ts", "symbol", "side", "qty", "price", "fee", "slippage"}:
            raise ValueError("invalid fill keys")
        if type(f["seq"]) is not int or f["seq"] != i+1 or not start < timestamp(f["ts"]) <= end or (i and f["ts"] < events[i-1]["ts"]):
            raise ValueError("invalid fill sequence/timeline")
        if f["symbol"] not in symbols or f["side"] not in {"buy", "sell"}:
            raise ValueError("invalid fill identity")
        for k in ("qty", "price", "fee", "slippage"):
            _canonical_number(f[k], k, positive=k in {"qty", "price"})
    reconcile_snapshots(initial_cash, snaps, curve, events)
    if not 1 <= len(manifests) <= 31:
        raise ValueError("invalid price manifests count")
    for m in manifests:
        if set(m) != PRICE_KEYS or any(not isinstance(m[k], str) or not m[k] for k in ("source", "symbol")):
            raise ValueError("invalid manifest keys/identity")
        if (m["market"], m["timeframe"], m["checksum_algo"]) != ("spot", "1d", "sha256") or not _hash(m["checksum"]):
            raise ValueError("invalid manifest market/timeframe/checksum")
        for k in ("start_at", "end_at", "kline_count"):
            timestamp(m[k])
        if m["start_at"] != start or m["end_at"] != end or m["kline_count"] < len(snaps):
            raise ValueError("manifest timeline/coverage mismatch")
    manifests.sort(key=lambda m: m["symbol"])
    if [m["symbol"] for m in manifests] != sorted(set(symbols) | {"BTCUSDT"}):
        raise ValueError("manifests must exactly cover positions plus BTC")
    initial = Fraction(initial_cash)
    peak, drawdown = initial, Fraction(0)
    for c, b in zip(curve, bench):
        equity = Fraction(c["equity"])
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak-equity) / peak)
        b["equity"] = _ratio(initial * Fraction(b["close_price"]), Fraction(bench[0]["close_price"]))
    return {"schema_version": SCHEMA, "fills": events, "snapshots": snaps, "equity_curve": curve, "btc_benchmark": bench,
            "metrics": {"total_return": _ratio(Fraction(curve[-1]["equity"])-initial, initial),
                        "max_drawdown": _ratio(drawdown), "fill_count": str(len(events))},
            "input_manifests": {"prices": manifests, "metric_series": _metric_reference(metric_series, start, end)}}


def _canonical_number(raw, name, *, positive=False):
    if not isinstance(raw, str):
        raise ValueError(f"{name} must be a canonical Decimal string")
    value = decimal_value(Decimal(raw), name, positive=positive)
    if decimal_text(value) != raw:
        raise ValueError(f"{name} must be canonical")
