"""Feature 62-3 paper_tick wire contract tests (SPEC §17.7).

Covers:
  1. KernelState snapshot round-trip fidelity (conformance fixtures, §17.2).
  2. Replay parity: a sequence of paper_tick calls threading the wire
     snapshot through JSON serialization between ticks must produce the same
     decisions/closed_trades/final state as a one-shot evaluate() sequence
     (§17.7 item 2, the paper_tick analogue of
     test_replay_loop_and_paper_tick_use_identical_evaluate).
  3. First-tick null-state vs non-first-tick exact-key validation (§17.7
     item 4).
  4. state hash mismatch rejected without executing (§17.7 item 3).
  5. Insufficient central data for the requested window fails closed as
     STRATEGY_COVERAGE_INCOMPLETE (§17.7 item 3, "tick frame 缺失").
  6. The paper_tick intent probe is independent of (and must be checked
     before) the historical_replay intent probe -- zero regression for the
     latter (§17.7 item 5, Provider-side half of the routing contract).
"""

from __future__ import annotations

import copy
import json
import sys
from decimal import Decimal, localcontext
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cutie_backtesting_provider as provider  # noqa: E402
import test_strategy_kernel as tsk  # noqa: E402
from canonical_json import canonical_decimal_str, canonical_json_sha256  # noqa: E402
from strategy_execution import (  # noqa: E402
    ERR_PAPER_STATE_MISMATCH,
    is_strategy_execution_intent,
    is_strategy_paper_tick_intent,
    validate_paper_tick_request,
)
from strategy_kernel import (  # noqa: E402
    _DECIMAL_CONTEXT,
    ERR_COVERAGE_INCOMPLETE,
    ERR_SPEC_INVALID,
    KernelState,
    PendingEntry,
    Position,
    StrategyContractError,
    StrategyKernel,
    build_frames,
    capability_hash,
    capability_payload,
    compile_strategy,
    from_snapshot,
    initial_state,
    simulate,
    to_snapshot,
)

REVISION = tsk.REVISION
STATE_CONFORMANCE_FIXTURE = (
    Path(__file__).parent / "fixtures" / "strategy_kernel_state_conformance_v1.json"
)


def _load_state_conformance_cases() -> list[dict]:
    return json.loads(STATE_CONFORMANCE_FIXTURE.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize(
    "name",
    [case["name"] for case in _load_state_conformance_cases()],
)
def test_kernel_state_snapshot_round_trip_conformance(name: str) -> None:
    case = next(
        item for item in _load_state_conformance_cases() if item["name"] == name
    )
    snapshot = case["snapshot"]
    restored = from_snapshot(
        copy.deepcopy(snapshot), [], execution_start_at=0, execution_end_at=999_999_999
    )
    round_tripped = to_snapshot(restored)
    assert round_tripped == snapshot, f"{name}: round trip mismatch"
    # decimal128 double round trip: to -> from -> to -> from -> to must be
    # stable too (not just single-pass), per §17.2's exact wording.
    twice = to_snapshot(
        from_snapshot(
            copy.deepcopy(round_tripped),
            [],
            execution_start_at=0,
            execution_end_at=999_999_999,
        )
    )
    assert twice == snapshot


def test_kernel_state_snapshot_matches_initial_state_shape() -> None:
    plan = tsk.compile_spec(tsk.make_spec())
    state = initial_state(plan, tsk.execution_params())
    snapshot = to_snapshot(state)
    assert snapshot == {
        "schema": "cutie.strategy_kernel_state.v1",
        "equity": "10000",
        "initial_capital": "10000",
        "instrument_rules": {
            "symbol": "BTCUSDT",
            "price_tick": "0.1",
            "qty_step": "0.001",
            "min_qty": "0.001",
            "min_notional": "5",
        },
        "pending_entry": None,
        "position": None,
        "last_exit_index": None,
        "trade_seq": 0,
    }


def test_from_snapshot_rejects_missing_key() -> None:
    plan = tsk.compile_spec(tsk.make_spec())
    snapshot = to_snapshot(initial_state(plan, tsk.execution_params()))
    del snapshot["trade_seq"]
    with pytest.raises(StrategyContractError) as caught:
        from_snapshot(snapshot, [], execution_start_at=0, execution_end_at=10800)
    assert caught.value.code == ERR_SPEC_INVALID


def test_from_snapshot_rejects_unknown_key() -> None:
    plan = tsk.compile_spec(tsk.make_spec())
    snapshot = to_snapshot(initial_state(plan, tsk.execution_params()))
    snapshot["unknown"] = "x"
    with pytest.raises(StrategyContractError) as caught:
        from_snapshot(snapshot, [], execution_start_at=0, execution_end_at=10800)
    assert caught.value.code == ERR_SPEC_INVALID


def test_from_snapshot_rejects_wrong_schema() -> None:
    plan = tsk.compile_spec(tsk.make_spec())
    snapshot = to_snapshot(initial_state(plan, tsk.execution_params()))
    snapshot["schema"] = "cutie.strategy_kernel_state.v2"
    with pytest.raises(StrategyContractError) as caught:
        from_snapshot(snapshot, [], execution_start_at=0, execution_end_at=10800)
    assert caught.value.code == ERR_SPEC_INVALID


def test_from_snapshot_rejects_non_canonical_decimal() -> None:
    plan = tsk.compile_spec(tsk.make_spec())
    snapshot = to_snapshot(initial_state(plan, tsk.execution_params()))
    snapshot["equity"] = "10000.0"  # trailing zero -- not canonical
    with pytest.raises(StrategyContractError) as caught:
        from_snapshot(snapshot, [], execution_start_at=0, execution_end_at=10800)
    assert caught.value.code == ERR_SPEC_INVALID


def test_from_snapshot_rejects_position_missing_key() -> None:
    with localcontext(_DECIMAL_CONTEXT):
        boundary = +(Decimal(1) / Decimal(3))
    state = KernelState(
        equity=boundary,
        initial_capital=Decimal("10000"),
        instrument_rules={
            "symbol": "BTCUSDT",
            "price_tick": "0.1",
            "qty_step": "0.001",
            "min_qty": "0.001",
            "min_notional": "5",
        },
        execution_start_at=0,
        execution_end_at=36000,
        pending_entry=PendingEntry(3, boundary, None),
        position=Position(
            side="short",
            qty=Decimal("0.5"),
            entry_price=boundary,
            opened_at=3600,
            stop_loss=None,
            take_profit=boundary,
            bars_held=4,
            pending_signal_exit=True,
        ),
        last_exit_index=2,
        trade_seq=7,
    )
    snapshot = to_snapshot(state)
    del snapshot["position"]["bars_held"]
    with pytest.raises(StrategyContractError) as caught:
        from_snapshot(snapshot, [], execution_start_at=0, execution_end_at=36000)
    assert caught.value.code == ERR_SPEC_INVALID


def _paper_capability() -> tuple[dict, str]:
    capability = capability_payload(REVISION)
    return capability, capability_hash(capability)


def build_paper_request(
    spec: dict,
    *,
    bar_open_at: int,
    window_start_at: int,
    execution_start_at: int,
    end_at: int,
    state: dict | None,
    prev_state_hash: str | None,
    paper_run_id: str = "800001",
) -> dict:
    manifest = tsk.make_manifest(spec)
    spec_hash = canonical_json_sha256(spec)
    manifest_hash = canonical_json_sha256(manifest)
    artifact_hash = canonical_json_sha256(
        {
            "schema": "cutie.strategy_artifact_digest.v1",
            "spec_hash": spec_hash,
            "manifest_hash": manifest_hash,
        }
    )
    params = tsk.execution_params()
    params["start_at"] = execution_start_at
    params["end_at"] = end_at
    capability, cap_hash = _paper_capability()
    return {
        "schema": "cutie.strategy_paper_tick_request.v1",
        "execution_mode": "paper_tick",
        "paper_run_id": paper_run_id,
        "artifact": {
            "artifact_id": "800002",
            "artifact_version_id": "800003",
            "version_no": 1,
            "spec_hash": spec_hash,
            "manifest_hash": manifest_hash,
            "artifact_hash": artifact_hash,
        },
        "strategy_spec": spec,
        "artifact_manifest": manifest,
        "execution_params": params,
        "tick": {
            "bar_open_at": bar_open_at,
            "window_start_at": window_start_at,
            "execution_start_at": execution_start_at,
        },
        "state": state,
        "prev_state_hash": prev_state_hash,
        "expected_capability_hash": cap_hash,
        "expected_provider_revision": REVISION,
        "dispatch_nonce": f"nonce-{bar_open_at}",
        "result_contract": {
            "result_schema": "cutie.strategy_paper_tick_result.v1",
            "coverage_schema": "cutie.strategy_coverage_manifest.v1",
        },
    }


def test_validate_paper_tick_request_first_tick_exact_keys() -> None:
    capability, cap_hash = _paper_capability()
    request = build_paper_request(
        tsk.make_spec(),
        bar_open_at=0,
        window_start_at=0,
        execution_start_at=0,
        end_at=3600,
        state=None,
        prev_state_hash=None,
    )
    validated = validate_paper_tick_request(request, capability, cap_hash)
    assert validated.request["tick"]["bar_open_at"] == 0

    missing_tick = dict(request)
    del missing_tick["tick"]
    with pytest.raises(StrategyContractError) as caught:
        validate_paper_tick_request(missing_tick, capability, cap_hash)
    assert caught.value.code == ERR_SPEC_INVALID


def test_validate_paper_tick_request_first_tick_requires_null_prev_hash() -> None:
    capability, cap_hash = _paper_capability()
    request = build_paper_request(
        tsk.make_spec(),
        bar_open_at=0,
        window_start_at=0,
        execution_start_at=0,
        end_at=3600,
        state=None,
        prev_state_hash="0" * 64,
    )
    with pytest.raises(StrategyContractError) as caught:
        validate_paper_tick_request(request, capability, cap_hash)
    assert caught.value.code == ERR_SPEC_INVALID
    assert caught.value.path == "$.prev_state_hash"


def test_validate_paper_tick_request_non_first_tick_state_hash_match() -> None:
    spec = tsk.make_spec()
    plan = tsk.compile_spec(spec)
    state = initial_state(plan, tsk.execution_params())
    snapshot = to_snapshot(state)
    snapshot_hash = canonical_json_sha256(snapshot)
    capability, cap_hash = _paper_capability()
    request = build_paper_request(
        spec,
        bar_open_at=0,
        window_start_at=0,
        execution_start_at=0,
        end_at=3600,
        state=snapshot,
        prev_state_hash=snapshot_hash,
    )
    validated = validate_paper_tick_request(request, capability, cap_hash)
    assert validated.request["prev_state_hash"] == snapshot_hash


def test_validate_paper_tick_request_state_hash_mismatch_rejected() -> None:
    spec = tsk.make_spec()
    plan = tsk.compile_spec(spec)
    state = initial_state(plan, tsk.execution_params())
    snapshot = to_snapshot(state)
    capability, cap_hash = _paper_capability()
    request = build_paper_request(
        spec,
        bar_open_at=0,
        window_start_at=0,
        execution_start_at=0,
        end_at=3600,
        state=snapshot,
        prev_state_hash="0" * 64,
    )
    with pytest.raises(StrategyContractError) as caught:
        validate_paper_tick_request(request, capability, cap_hash)
    assert caught.value.code == ERR_PAPER_STATE_MISMATCH


def test_paper_tick_probe_is_independent_of_and_checked_before_execution_probe() -> None:
    replay_body = tsk.build_request(tsk.make_spec())
    assert is_strategy_execution_intent(replay_body) is True
    assert is_strategy_paper_tick_intent(replay_body) is False

    paper_body = build_paper_request(
        tsk.make_spec(),
        bar_open_at=0,
        window_start_at=0,
        execution_start_at=0,
        end_at=3600,
        state=None,
        prev_state_hash=None,
    )
    assert is_strategy_paper_tick_intent(paper_body) is True
    # is_strategy_execution_intent's own key-set fallback also matches a
    # paper_tick body (shared field names) -- this is exactly why
    # run_backtest() must check is_strategy_paper_tick_intent FIRST (see
    # cutie_backtesting_provider.py); the replay probe's own definition is
    # untouched and this assertion documents the overlap rather than
    # pretending it does not exist.
    assert is_strategy_execution_intent(paper_body) is True


def test_paper_tick_sequence_matches_one_shot_evaluate_through_snapshot_round_trip(
    monkeypatch,
) -> None:
    monkeypatch.setattr(provider, "PROVIDER_REVISION", REVISION)

    def fetch_klines(exchange, market, symbol, timeframe, start_ms, end_ms):
        return [[i * 3_600_000, 100, 101, 99, 100, 1] for i in range(6)]

    monkeypatch.setattr(provider, "_fetch_from_central", fetch_klines)

    # cooldown_bars=1000 blocks re-entry for the rest of the 6-bar window
    # after the single trade closes at frame index 2 (time_exit_bars=2), so
    # by frame index 5 there is no open position and no pending_entry --
    # finalize() is therefore a true no-op and a one-shot simulate() (which
    # always finalizes) is directly comparable to paper_tick's decisions/
    # closed_trades (which never finalizes, §17.6).
    spec = tsk.make_spec(time_exit_bars=2)
    spec["entry"]["cooldown_bars"] = 1000
    manifest = tsk.make_manifest(spec)
    plan = compile_strategy(spec, manifest, capability_payload(REVISION))

    primary_rows = [
        {
            "open_time": i * 3600,
            "open": "100",
            "high": "101",
            "low": "99",
            "close": "100",
            "volume": "1",
        }
        for i in range(6)
    ]
    coverage = {
        "summary": {"strict_eligible": True},
        "request_identity": {"symbol": "BTCUSDT"},
    }
    frames = build_frames({"binance.futures.kline.1h": primary_rows}, coverage, plan)
    assert len(frames) == 6

    reference_params = tsk.execution_params()
    reference_params["end_at"] = 21600  # covers all 6 bars (0..18000, close 21600)
    reference = simulate(plan, frames, initial_state(plan, reference_params))
    assert reference["diagnostics"] == []
    assert len(reference["trades"]) == 1

    state_json: dict | None = None
    prev_hash: str | None = None
    all_decisions: list[dict] = []
    all_closed_trades: list[dict] = []
    client = TestClient(provider.app)
    for bar_open_at in (0, 3600, 7200, 10800, 14400, 18000):
        request = build_paper_request(
            spec,
            bar_open_at=bar_open_at,
            window_start_at=0,
            execution_start_at=0,
            end_at=bar_open_at + 3600,
            state=state_json,
            prev_state_hash=prev_hash,
        )
        response = client.post(
            "/cutie/backtest",
            json=request,
            headers={"X-Cutie-Connector-Version": "1.2.3"},
        )
        assert response.status_code == 200
        body = response.json()
        assert "result_status" not in body, body
        assert set(body.keys()) == {
            "schema",
            "paper_run_id",
            "bar_open_at",
            "prev_state_hash",
            "next_state",
            "next_state_hash",
            "decisions",
            "closed_trades",
            "diagnostics",
            "coverage_manifest",
            "coverage_manifest_hash",
            "executed_artifact_hash",
            "capability_hash",
            "provider_revision",
            "provider_process_fingerprint",
        }
        assert body["diagnostics"] == []
        assert body["next_state_hash"] == canonical_json_sha256(body["next_state"])
        all_decisions.extend(body["decisions"])
        all_closed_trades.extend(body["closed_trades"])
        # Round-trip through JSON text (not just Python dict identity) to
        # emulate real Server storage / Connector wire transport before
        # threading into the next tick's request.
        state_json = json.loads(json.dumps(body["next_state"]))
        prev_hash = body["next_state_hash"]

    assert all_decisions == reference["decisions"]

    running_equity = Decimal(tsk.execution_params()["initial_capital"])
    expected_closed_trades = []
    for trade, trace in zip(reference["trades"], reference["trace_trades"]):
        running_equity = running_equity + Decimal(trade["pnl"])
        expected_closed_trades.append(
            {
                "trade_seq": trade["seq"],
                "symbol": "BTCUSDT",
                "direction": trade["side"],
                "entry_time": trade["opened_at"],
                "exit_time": trade["closed_at"],
                "entry_price": trade["entry_price"],
                "exit_price": trade["exit_price"],
                "stop_loss": trace["stop_loss"],
                "take_profit": trace["take_profit"],
                "exit_kind": trace["exit_kind"],
                "pnl_usd": trade["pnl"],
                "equity_after": canonical_decimal_str(running_equity),
            }
        )
    assert all_closed_trades == expected_closed_trades
    assert state_json["trade_seq"] == len(reference["trades"])
    assert Decimal(state_json["equity"]) == running_equity


def test_paper_tick_second_tick_reuses_restored_state_without_reopening_history(
    monkeypatch,
) -> None:
    """Narrow regression lock for the from_snapshot(frames=history) design:
    a second tick whose request state carries an already-open position must
    not re-run entry logic for bars a prior tick already evaluated."""
    monkeypatch.setattr(provider, "PROVIDER_REVISION", REVISION)

    def fetch_klines(exchange, market, symbol, timeframe, start_ms, end_ms):
        return [[i * 3_600_000, 100, 101, 99, 100, 1] for i in range(3)]

    monkeypatch.setattr(provider, "_fetch_from_central", fetch_klines)

    spec = tsk.make_spec(time_exit_bars=2)
    client = TestClient(provider.app)

    first = client.post(
        "/cutie/backtest",
        json=build_paper_request(
            spec,
            bar_open_at=0,
            window_start_at=0,
            execution_start_at=0,
            end_at=3600,
            state=None,
            prev_state_hash=None,
        ),
        headers={"X-Cutie-Connector-Version": "1.2.3"},
    ).json()
    assert first["closed_trades"] == []
    # entry_pending decision expected on frame 0 (condition always true).
    assert any(d["kind"] == "entry_pending" for d in first["decisions"])

    second = client.post(
        "/cutie/backtest",
        json=build_paper_request(
            spec,
            bar_open_at=3600,
            window_start_at=0,
            execution_start_at=0,
            end_at=7200,
            state=first["next_state"],
            prev_state_hash=first["next_state_hash"],
        ),
        headers={"X-Cutie-Connector-Version": "1.2.3"},
    ).json()
    # The pending entry from tick 1 fills on tick 2's open -- exactly one
    # entry_filled decision, no re-emitted entry_pending for frame 0.
    assert any(d["kind"] == "entry_filled" for d in second["decisions"])
    assert not any(d["kind"] == "entry_pending" for d in second["decisions"])
    assert second["next_state"]["position"] is not None


def test_paper_tick_insufficient_central_history_fails_closed_coverage_incomplete(
    monkeypatch,
) -> None:
    """§17.3: the tick's target frame must exist in the reconstructed window
    with available_at <= decision time, or STRATEGY_COVERAGE_INCOMPLETE
    (fail-closed, no execution). Requesting a window far beyond what the
    (mocked) central source actually has exercises this without needing to
    fabricate an internal build_frames bug."""
    monkeypatch.setattr(provider, "PROVIDER_REVISION", REVISION)

    def fetch_klines(exchange, market, symbol, timeframe, start_ms, end_ms):
        return [[i * 3_600_000, 100, 101, 99, 100, 1] for i in range(6)]

    monkeypatch.setattr(provider, "_fetch_from_central", fetch_klines)

    spec = tsk.make_spec()
    request = build_paper_request(
        spec,
        bar_open_at=360_000,  # far beyond the 6 bars the mock actually returns
        window_start_at=0,
        execution_start_at=0,
        end_at=360_000 + 3600,
        state=None,
        prev_state_hash=None,
    )
    response = TestClient(provider.app).post(
        "/cutie/backtest",
        json=request,
        headers={"X-Cutie-Connector-Version": "1.2.3"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["result_status"] == "failed"
    assert body["error_type"] == ERR_COVERAGE_INCOMPLETE


def test_historical_replay_end_to_end_still_succeeds_alongside_paper_tick(
    monkeypatch,
) -> None:
    """Zero-regression guard (§17.7 item 5, Provider-side half): a legal
    historical_replay request must still route and succeed exactly as
    before, unaffected by the new paper_tick probe/branch added ahead of it
    in run_backtest()."""
    monkeypatch.setattr(provider, "PROVIDER_REVISION", REVISION)

    def fetch_klines(exchange, market, symbol, timeframe, start_ms, end_ms):
        return [[i * 3_600_000, 100, 101, 99, 100, 1] for i in range(4)]

    monkeypatch.setattr(provider, "_fetch_from_central", fetch_klines)

    request = tsk.build_request(tsk.make_spec())
    response = TestClient(provider.app).post(
        "/cutie/backtest",
        json=request,
        headers={"X-Cutie-Connector-Version": "1.2.3"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["result_status"] == "success", body
