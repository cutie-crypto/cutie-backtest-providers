"""ARM-C0-P2-P: local synthetic HTTP, settlement and byte/hash conservation; no network."""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest
from fastapi.responses import JSONResponse

import test_arm_c0_p_hourly_metrics as hourly
from canonical_json import canonical_json_sha256
from strategy_liquidation_evidence import compress_hourly_evidence
from strategy_macro_events import macro_report
from strategy_event_window import settle_event_window_exits

p = hourly.p
CORE = ("schema", "trades", "metrics", "equity_curve", "data_manifest")
LIMIT = 1048576


def wire(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def run_pair(monkeypatch, tmp_path, hours):
    post, *_ = hourly.hourly(monkeypatch, tmp_path, "liq", hours=hours)
    monkeypatch.setattr(p.time, "time", lambda: 1800000000)
    captured = {}
    original = p.compress_hourly_evidence
    def compress(report, config):
        captured.update(report=deepcopy(report), config=config)
        return original(report, config)
    monkeypatch.setattr(p, "compress_hourly_evidence", compress)
    new = post()
    with monkeypatch.context() as legacy:
        legacy.setattr(p, "compress_hourly_evidence", lambda report, config: report)
        # Legacy long windows exceed the callback cap; capture their unchanged core before the cap.
        legacy.setattr(p, "_bounded_template_response", lambda run_id, body: JSONResponse(content=body))
        old = post()
    assert old["result_status"] == new["result_status"] == "success", new
    return old, new, captured


@pytest.mark.parametrize("hours", [3606, 4326])
def test_hourly_sparse_3606_4326_budget(monkeypatch, tmp_path, hours):
    old, new, _ = run_pair(monkeypatch, tmp_path, hours)
    assert len(wire(old["raw_report"])) > LIMIT
    assert len(wire(new["raw_report"])) < LIMIT
    for key in CORE:
        assert wire(old[key]) == wire(new[key]), key
    # Keep all other raw-report consumers byte-identical as well.
    assert {k: v for k, v in old["raw_report"].items() if k != "liquidation_reversal"} == {
        k: v for k, v in new["raw_report"].items() if k != "liquidation_reversal"}
    print(f"hours={hours} raw_report_bytes={len(wire(new['raw_report']))}")


def test_summary_conservation_hash(monkeypatch, tmp_path):
    _, new, captured = run_pair(monkeypatch, tmp_path, 77)
    source = captured["report"]["events"]
    report = new["raw_report"]["liquidation_reversal"]
    routine = [r for r in source if r["status"] == "skipped" and r.get("reason") in (
        "liquidation_below_threshold", "drop_below_min", "no_prior_day_candle")]
    assert report["event_count"] == len(source) == report["retained_count"] + report["summarized_count"]
    assert report["summarized_count"] == len(routine)
    assert report["trace_sha256"] == canonical_json_sha256(source)
    statuses, reasons, seen = Counter(r["status"] for r in report["events"]), Counter(
        r["reason"] for r in report["events"] if "reason" in r), []
    for section in report["summaries_24h"]:
        start = int(datetime.fromisoformat(section["period_start_utc"]).timestamp())
        rows = [r for r in routine if start <= datetime.fromisoformat(r["day_utc"]).timestamp() < start + 86400]
        assert section["count"] == len(rows) <= 24
        assert section["first_hour_utc"] == rows[0]["day_utc"]
        assert section["last_hour_utc"] == rows[-1]["day_utc"]
        assert section["sha256"] == canonical_json_sha256(rows)
        from decimal import Decimal
        for key in ("liquidation_long", "threshold", "drop_pct"):
            values = [Decimal(r[key]) for r in rows if key in r]
            assert section[key]["count"] == len(values)
            assert (Decimal(section[key]["min"]) if values else section[key]["min"]) == (min(values) if values else None)
            assert (Decimal(section[key]["max"]) if values else section[key]["max"]) == (max(values) if values else None)
        statuses.update(section["status_counts"])
        reasons.update(section["reason_counts"])
        seen.extend(r["ts_utc"] for r in rows)
    assert seen == [r["ts_utc"] for r in routine] and len(seen) == len(set(seen))
    assert dict(statuses) == report["counts"] == dict(Counter(r["status"] for r in source))
    assert dict(reasons) == report["reason_counts"] == dict(Counter(r["reason"] for r in source if "reason" in r))
    assert report["skipped_count"] == sum(r["status"] != "entered" for r in source)
    config = captured["config"]
    rows = list(config.evidence_rows)
    series = {"symbol": "BTC", "exchange": "AGGREGATED", "metric": "liquidation_long", "interval": "1h", "rows": rows}
    assert report["external_series"]["count"] == 720 + len(source)
    assert report["external_series"]["first_ts"] == hourly.T0 - 720 * hourly.H
    assert report["external_series"]["last_ts"] == hourly.T0 + 75 * hourly.H
    assert report["external_series"]["sha256"] == canonical_json_sha256(series)
    # Change warmup only: event outcomes remain identical, external input fingerprint must change.
    changed = deepcopy(config)
    changed.evidence_rows[0]["value"] = "101"
    again = compress_hourly_evidence(captured["report"], changed)
    assert again["trace_sha256"] == report["trace_sha256"]
    assert again["external_series"]["sha256"] != report["external_series"]["sha256"]
    altered = deepcopy(captured["report"])
    altered["events"][-1]["liquidation_long"] = "101"
    assert compress_hourly_evidence(altered, config)["trace_sha256"] != report["trace_sha256"]


def test_retain_attempts_native_and_fill(monkeypatch, tmp_path):
    _, _, captured = run_pair(monkeypatch, tmp_path, 48)
    config = captured["config"]
    events = deepcopy(captured["report"]["events"][:10])
    # Outcomes from actual post-run attribution: fill, rejection, native exit, early break.
    events[0].update(status="submitted", entry_bar_utc="2026-01-01T01:00:00+00:00")
    events[1].update(status="submitted", entry_bar_utc="2026-01-01T02:00:00+00:00")
    events[2].update(status="blocked_by_time_or_filter", reason="entry_gate")
    events[3].update(status="skipped_in_position", reason="previous_event_still_open")
    events[4].update(status="skipped_in_position", reason="native_exit_no_reentry")
    events[5].pop("status")
    events[6].update(status="skipped_out_of_range", reason="window_past_data_end")
    events[7].update(status="new_outcome", reason="future_reason")
    trade = dict(seq=0, opened_at=1767229200, closed_at=1767232800, entry_price="100", exit_price="97")
    settle_event_window_exits(events, [trade], None, decisions={},
                             bar_opens=[1767225600, 1767229200, 1767232800, 1767236400],
                             stop_bar=3, native_decisions={2: "stop_loss"})
    assert events[0]["exits"] == [{"time": "2026-01-01T02:00:00+00:00", "price": "97", "reason": "stop_loss"}]
    assert events[1]["status"] == "rejected_at_fill" and events[5]["status"] == "skipped_run_ended"
    source = macro_report(events)
    before = deepcopy(source)
    report = compress_hourly_evidence(source, config)
    assert report["events"] == events[:8]
    assert source == before  # Compression never edits settled source rows/counters.
    assert report["counts"] == source["counts"] and report["skipped_count"] == source["skipped_count"]
    assert report["skipped_events"] == source["skipped_events"][:7]
    # Missing-state guard must execute before compression can omit an ordinary hour.
    events[9].pop("status")
    with pytest.raises(ValueError, match="has no status"):
        settle_event_window_exits([events[9]], [], None, decisions={},
                                 bar_opens=[1767225600, 1767229200, 1767232800, 1767236400], stop_bar=None)


def test_consumers_and_legacy(monkeypatch, tmp_path):
    old, new, _ = run_pair(monkeypatch, tmp_path, 48)
    assert old["schema"] == new["schema"]
    assert "evidence_version" not in old["raw_report"]["liquidation_reversal"]
    assert new["raw_report"]["liquidation_reversal"]["evidence_version"] == "liquidation_evidence.v2"
    assert new["raw_report"]["liquidation_reversal"]["mode"] == "events_and_24h_summary"
    # Actual consumer input contracts: callback verification/replay and demo/UI core readings.
    for key in CORE:
        assert wire(old[key]) == wire(new[key])
    # Independently captured from merge baseline 5e0a360, not through today's provider path.
    frozen = json.loads((Path(__file__).parent / "fixtures/arm_c0_p2_hourly_core.json").read_text())
    assert {key: new[key] for key in CORE} == frozen
    assert new["raw_report"]["trade_exit_kinds"] == old["raw_report"]["trade_exit_kinds"]
    assert new["raw_report"]["legacy_metrics"] == old["raw_report"]["legacy_metrics"]
    from test_arm_c0_p_hourly_metrics import test_daily_and_4h_unchanged
    test_daily_and_4h_unchanged(monkeypatch, tmp_path)


def test_limit_message_and_dense_fail_closed(monkeypatch, tmp_path):
    _, _, captured = run_pair(monkeypatch, tmp_path, 48)
    # Dense real-outcome shape remains complete, and capacity gate still rejects it.
    events = []
    for i in range(6000):
        record = deepcopy(captured["report"]["events"][0])
        record.update(status="rejected_at_fill", reason="stop_wrong_side_of_fill", label="dense-" + str(i))
        events.append(record)
    report = compress_hourly_evidence(macro_report(events), captured["config"])
    assert report["events"] == events and report["summarized_count"] == 0
    assert len(wire(report)) > LIMIT
    failed = json.loads(p._bounded_template_response("dense", {"raw_report": {"liquidation_reversal": report}}).body)
    assert failed["result_status"] == "failed" and failed["error_type"] == "INVALID_PARAMS"
    assert failed["limitations"] == {"reason": "report_evidence_too_large", "field": "raw_report", "limit_bytes": LIMIT}
    assert "shorten the backtest range" in failed["error_message"]
    # Preserve the same limit boundary, including UTF-8 byte accounting.
    assert json.loads(p._bounded_template_response("exact", {"raw_report": "x" * (LIMIT - 2)}).body)["raw_report"] == "x" * (LIMIT - 2)
    failed = json.loads(p._bounded_template_response("unicode", {"raw_report": "清" * (LIMIT // 3)}).body)
    assert failed["limitations"]["reason"] == "report_evidence_too_large"
    ordinary = json.loads(p._validation_failure("INVALID_PARAMS", "invalid params").body)
    assert ordinary["limitations"]["reason"] != "report_evidence_too_large"
