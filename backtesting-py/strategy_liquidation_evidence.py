"""Versioned hourly liquidation evidence, only after settlement and outcome guards."""
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal

from canonical_json import canonical_decimal_str, canonical_json_sha256

EVIDENCE_VERSION = "liquidation_evidence.v2"
MODE = "events_and_24h_summary"
# Only ordinary non-trigger outcomes may be summarized. Unknown/new outcomes stay full.
ROUTINE_REASONS = frozenset(("liquidation_below_threshold", "drop_below_min", "no_prior_day_candle"))


def _routine(record):
    return (record["status"] == "skipped" and record.get("reason") in ROUTINE_REASONS
            and not any(key in record for key in ("entry_bar_utc", "entry_utc", "exits", "exit_reason")))


def _counts(records, key):
    return dict(Counter(record.get(key) for record in records if record.get(key) is not None))


def _range(records, key):
    values = [Decimal(record[key]) for record in records if record.get(key) is not None]
    return {"count": len(values), "min": canonical_decimal_str(str(min(values))) if values else None,
            "max": canonical_decimal_str(str(max(values))) if values else None}


def compress_hourly_evidence(report, config):
    """Keep legacy counters for the full trace; versioned arrays describe retained records only.

    UTC-day buckets span 24h. Their count/hash cover only ordinary non-trigger records,
    in original order; retained events plus summaries conserve the full event count.
    Hashes use canonical_json.v1; no hash claims downstream verification or restores rows.
    """
    events = report["events"]
    retained, buckets = [], {}
    for record in events:
        if not _routine(record):
            retained.append(record)
            continue
        hour = int(datetime.fromisoformat(record["day_utc"]).timestamp())
        buckets.setdefault(hour // 86400 * 86400, []).append(record)
    summaries = []
    for start, records in sorted(buckets.items()):
        summaries.append({
            "period_start_utc": datetime.fromtimestamp(start, timezone.utc).isoformat(),
            "first_hour_utc": records[0]["day_utc"], "last_hour_utc": records[-1]["day_utc"],
            "count": len(records), "status_counts": _counts(records, "status"),
            "reason_counts": _counts(records, "reason"),
            "liquidation_long": _range(records, "liquidation_long"),
            "threshold": _range(records, "threshold"), "drop_pct": _range(records, "drop_pct"),
            "sha256": canonical_json_sha256(records),
        })
    retained_times = {event["ts_utc"] for event in retained}
    rows = list(config.evidence_rows)
    series = {"symbol": config.assumptions["liquidation_series"].get("symbol"),
              "exchange": "AGGREGATED", "metric": "liquidation_long", "interval": "1h",
              "rows": rows}
    return {**report, "evidence_version": EVIDENCE_VERSION, "mode": MODE,
            "hash_schema": "canonical_json.v1", "events": retained,
            "skipped_events": [record for record in report["skipped_events"]
                               if record["ts_utc"] in retained_times],
            "event_count": len(events), "retained_count": len(retained),
            "summarized_count": sum(item["count"] for item in summaries),
            "reason_counts": _counts(events, "reason"), "summaries_24h": summaries,
            "trace_sha256": canonical_json_sha256(events),
            "external_series": {"symbol": series["symbol"], "exchange": series["exchange"],
                                "metric": series["metric"], "interval": series["interval"],
                                "first_ts": rows[0]["ts"] if rows else None,
                                "last_ts": rows[-1]["ts"] if rows else None,
                                "count": len(rows), "lookback_bars": config.lookback_bars,
                                "sha256": canonical_json_sha256(series)}}
