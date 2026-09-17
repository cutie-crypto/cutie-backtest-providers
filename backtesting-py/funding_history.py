"""F108 Binance USD-M public funding evidence (no account or trading APIs).

Source: developers.binance.com/docs/derivatives/usds-margined-futures/
market-data/rest-api/Get-Funding-Rate-History
Only this venue is supported; never substitute one exchange's funding for another.
"""

import hashlib
import json
import re
import time
import urllib.parse
import urllib.request
from decimal import Decimal


def _decimal(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("non-finite funding evidence")
    return result


def _request(params):
    url = "https://fapi.binance.com/fapi/v1/fundingRate?" + urllib.parse.urlencode(
        params
    )
    with urllib.request.urlopen(url, timeout=15) as response:
        return json.load(response)


def fetch_history(symbol, start_ms, end_ms, *, request=None, now_ms=None):
    """Exhaust an inclusive historical range; preserve special-rate events.

    Pagination overlaps its final timestamp so two rate types sharing a timestamp
    cannot be silently split across pages. Conflicts/non-progress fail closed.
    An exhausted API range is source evidence, not proof the exchange never omits
    historical data. Persist the returned manifest alongside its monetary result.
    """
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]+USDT", symbol):
        raise ValueError("funding requires a Binance USDT perpetual symbol")
    if (
        type(start_ms) is not int
        or type(end_ms) is not int
        or not 0 <= start_ms <= end_ms
    ):
        raise ValueError("invalid funding range")
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    if end_ms > now_ms:
        raise ValueError("future funding is not evidence")
    request = request or _request
    cursor = start_ms
    collected = {}
    for _ in range(100):
        rows = request(
            {"symbol": symbol, "startTime": cursor, "endTime": end_ms, "limit": 1000}
        )
        if not isinstance(rows, list) or len(rows) > 1000:
            raise ValueError("invalid funding response")
        timestamps = []
        added = 0
        for row in rows:
            ts = row.get("fundingTime")
            kind = row.get("rateType", "Regular")
            if (
                type(ts) is not int
                or not cursor <= ts <= end_ms
                or row.get("symbol") != symbol
            ):
                raise ValueError("funding row identity/range mismatch")
            if kind not in ("Regular", "Special"):
                raise ValueError("unknown funding rate type")
            rate, mark = _decimal(row["fundingRate"]), _decimal(row["markPrice"])
            if mark <= 0:
                raise ValueError("funding mark price must be positive")
            normalized = {
                "time_ms": ts,
                "rate_type": kind,
                "rate": str(rate),
                "mark_price": str(mark),
            }
            key = (ts, kind)
            if key in collected and collected[key] != normalized:
                raise ValueError("conflicting funding evidence")
            added += key not in collected
            collected[key] = normalized
            timestamps.append(ts)
        if timestamps != sorted(timestamps):
            raise ValueError("funding response is not ascending")
        if len(rows) < 1000:
            break
        if not added or timestamps[-1] <= cursor:
            raise ValueError("funding pagination made no progress")
        cursor = timestamps[-1]
    else:
        raise ValueError("funding range exceeds page budget")
    rows = [collected[key] for key in sorted(collected)]
    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    return {
        "exchange": "binance",
        "market": "usdt_perpetual",
        "symbol": symbol,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "fetched_at_ms": now_ms,
        "source": "https://fapi.binance.com/fapi/v1/fundingRate",
        "rows_sha256": hashlib.sha256(encoded).hexdigest(),
        "rows": rows,
    }


def funding_paid(history, segments, direction):
    """Sum signed quote-currency cost over (entry, exit] quantity segments.

    Funding is applied before exits and before new entries at the same timestamp.
    This deterministic platform convention must be shared with the backtest;
    it is not a claim about an individual subscriber's exchange fill ordering.
    """
    encoded = json.dumps(
        history["rows"], sort_keys=True, separators=(",", ":")
    ).encode()
    if hashlib.sha256(encoded).hexdigest() != history["rows_sha256"]:
        raise ValueError("funding evidence checksum mismatch")
    if history["exchange"] != "binance" or history["market"] != "usdt_perpetual":
        raise ValueError("unsupported funding evidence venue")
    if direction not in ("long", "short"):
        raise ValueError("invalid funding direction")
    sign = Decimal(1) if direction == "long" else Decimal(-1)
    total = Decimal(0)
    for segment in segments:
        start, end = segment["entry_ms"], segment["exit_ms"]
        quantity = _decimal(segment["quantity"])
        if (
            not history["start_ms"] <= start <= end <= history["end_ms"]
            or quantity <= 0
        ):
            raise ValueError("funding segment outside evidence coverage")
        for row in history["rows"]:
            if start < row["time_ms"] <= end:
                total += (
                    sign
                    * quantity
                    * _decimal(row["mark_price"])
                    * _decimal(row["rate"])
                )
    return total
