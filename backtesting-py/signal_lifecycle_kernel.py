"""Portable signal lifecycle candle detector, shared by runtime and F108 replay.

No database, configuration, network or application imports. Vendored provider
copies must match this source exactly; the runtime service re-exports its types.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Optional

ExecutionEventType = Literal[
    "entry_hit",
    "tp_hit",
    "sl_hit",
    "expired_unfilled",
    "manual_entry",
    "manual_tp",
    "manual_close",
    "feed_close",
    "strategy_exit",
    "cancelled",
    "limit_entry_hit",
    "entry_pending",
    "entry_pending_revoked",
]

logger = logging.getLogger(__name__)


def _warn(message, *args):
    logger.warning(message.format(*args))


@dataclass
class Candle:
    open_time: int
    close_time: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass
class ExecutionEvent:
    event_type: ExecutionEventType
    observed_price: Optional[Decimal]
    observed_at: int
    evidence_source: str
    tp_index: Optional[int] = None
    evidence: Optional[dict[str, Any]] = None
    manual_reason: Optional[str] = None
    created_by: Optional[int] = None


def _first_non_none(mapping: dict, *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return default


def _dec(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _target_prices(signal: dict) -> list[Decimal]:
    raw = signal.get("target_prices") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            _warn("signal {} target_prices JSON parse failed: {!r}", signal.get("id"), raw)
            return []
    values: list[Decimal] = []
    if isinstance(raw, list):
        for index, item in enumerate(raw):
            parsed = _dec(item)
            if parsed is not None:
                values.append(parsed)
            else:
                _warn(
                    "signal {} target_prices item parse failed index={} value={!r}",
                    signal.get("id"),
                    index,
                    item,
                )
    elif raw:
        _warn("signal {} target_prices unsupported type: {!r}", signal.get("id"), raw)
    return values


def _entry_bounds(signal: dict) -> tuple[Optional[Decimal], Optional[Decimal]]:
    start = _dec(signal.get("entry_price"))
    end = _dec(signal.get("entry_price_end"))
    if start is None:
        return None, None
    if end is None:
        return start, start
    return min(start, end), max(start, end)


class SignalLifecycleKernel:
    @staticmethod
    def _prepare_candle_scan(
        signal: dict,
        candles: list[Candle],
        direction: str,
        entry_low: Optional[Decimal],
        entry_high: Optional[Decimal],
        is_market_base_limit: bool,
        has_entry: bool,
        has_limit_entry: bool,
        needs_limit_entry: bool,
        now: int,
    ) -> tuple[list[ExecutionEvent], bool, bool, Optional[Decimal], int, bool]:
        """detect_candle_events 的循环前置块（market_base 兜底 + legacy 回补 + 参考价）。

        末位 bool 为 terminate：True 表示已产出自包含结果（空事件），调用方直接返回。
        """
        events: list[ExecutionEvent] = []

        create_price = _dec(signal.get("create_snapshot_price"))
        if is_market_base_limit and not has_entry and signal.get("status") == "published":
            created_at = int(signal.get("created_at") or 0)
            if create_price is None:
                if now - created_at < 120:
                    return [], has_entry, needs_limit_entry, None, created_at, True
                fallback_candle = next((c for c in candles if c.open_time >= created_at), None)
                if fallback_candle is None:
                    _warn(
                        "market_base_limit signal {} create snapshot missing and no post-create candle, "
                        "base entry deferred",
                        signal.get("id"),
                    )
                    return [], has_entry, needs_limit_entry, None, created_at, True
                create_price = fallback_candle.open
                evidence = {
                    "provider": "binance_futures",
                    "interval": "5m",
                    "candle_open_time": fallback_candle.open_time,
                    "candle_close_time": fallback_candle.close_time,
                    "open": str(fallback_candle.open),
                    "high": str(fallback_candle.high),
                    "low": str(fallback_candle.low),
                    "close": str(fallback_candle.close),
                    "matched_level": "market_base",
                    "level_price": str(create_price),
                    "reason": "snapshot_missing_fallback_candle_open",
                }
            else:
                evidence = {
                    "provider": "signal_snapshots",
                    "matched_level": "market_base",
                    "level_price": str(create_price),
                }
            events.append(
                ExecutionEvent(
                    event_type="entry_hit",
                    observed_price=create_price,
                    observed_at=created_at,
                    evidence_source="system",
                    evidence=evidence,
                )
            )
            has_entry = True
            needs_limit_entry = not has_limit_entry

        legacy_active_without_entry_event = (
            not signal.get("lifecycle_status") and signal.get("status") == "active" and not signal.get("entry_hit_at")
        )
        if legacy_active_without_entry_event:
            if create_price is None:
                return [], has_entry, needs_limit_entry, None, int(signal.get("created_at") or 0), True
            tolerance = Decimal("0.003")
            if (direction == "long" and create_price > entry_high * (1 + tolerance)) or (
                direction == "short" and create_price < entry_low * (1 - tolerance)
            ):
                return [], has_entry, needs_limit_entry, None, int(signal.get("created_at") or 0), True

        created_at = int(signal.get("created_at") or 0)
        reference_price = create_price
        if reference_price is None:
            first_post_create = next((c for c in candles if c.open_time >= created_at), None)
            reference_price = first_post_create.open if first_post_create else None

        return events, has_entry, needs_limit_entry, reference_price, created_at, False

    @staticmethod
    def _detect_candle_candidates(
        candle: Candle,
        signal: dict,
        direction: str,
        entry_low: Optional[Decimal],
        entry_high: Optional[Decimal],
        targets: list[Decimal],
        stop_loss: Optional[Decimal],
        has_entry: bool,
        needs_limit_entry: bool,
        fully_after_create: bool,
    ) -> tuple[bool, bool, list, bool]:
        """detect_candle_events 的单根 K 线候选事件探测（纯提取）。"""
        entry_hit = False
        if not has_entry and fully_after_create:
            entry_hit = candle.low <= entry_high if direction == "long" else candle.high >= entry_low

        limit_entry_hit = False
        if (
            needs_limit_entry
            and fully_after_create
            and (not signal.get("expires_at") or candle.close_time <= int(signal["expires_at"]))
        ):
            limit_entry_hit = candle.low <= entry_high if direction == "long" else candle.high >= entry_low

        tp_hits = SignalLifecycleKernel._detect_tp_hits(signal, candle, targets) if fully_after_create else []
        sl_state = SignalLifecycleKernel._detect_sl_hit(signal, candle, stop_loss) if fully_after_create else False
        return entry_hit, limit_entry_hit, tp_hits, sl_state

    @staticmethod
    def _process_candle(
        candle: Candle,
        signal: dict,
        direction: str,
        entry_low: Optional[Decimal],
        entry_high: Optional[Decimal],
        targets: list[Decimal],
        stop_loss: Optional[Decimal],
        has_entry: bool,
        needs_limit_entry: bool,
        reference_price: Optional[Decimal],
        created_at: int,
    ) -> tuple[bool, list[ExecutionEvent], bool, bool]:
        """detect_candle_events 的单根 K 线冲突裁决 + 事件产出（纯提取）。

        返回 (terminate, events, has_entry, needs_limit_entry)：
        - terminate=True 表示本根 K 线已产出 expired_unfilled 早退事件，调用方直接返回该 events。
        """
        fully_after_create = candle.open_time >= created_at
        entry_hit, limit_entry_hit, tp_hits, sl_hit = SignalLifecycleKernel._detect_candle_candidates(
            candle,
            signal,
            direction,
            entry_low,
            entry_high,
            targets,
            stop_loss,
            has_entry,
            needs_limit_entry,
            fully_after_create,
        )

        # 同根 K 线多事件冲突：系统自动裁决，不再卡 needs_review
        if entry_hit and tp_hits and sl_hit:
            sl_hit = False
        if entry_hit and sl_hit:
            return (
                True,
                [
                    SignalLifecycleKernel._event_from_candle(
                        "expired_unfilled",
                        stop_loss,
                        candle,
                        evidence_source="binance_futures",
                        matched_level="sl_before_viable_entry",
                    )
                ],
                has_entry,
                needs_limit_entry,
            )
        if has_entry and tp_hits and sl_hit:
            sl_hit = False

        if not has_entry and not entry_hit and tp_hits and fully_after_create:
            # 未入场且本根 K 线未触达入场区间，但已先触达止盈位
            tp_price = tp_hits[0][1]
            if reference_price is None:
                _warn(
                    "tp_before_viable_entry skipped: signal {} missing reference price",
                    signal.get("id"),
                )
            elif (direction == "long" and reference_price < tp_price) or (
                direction == "short" and reference_price > tp_price
            ):
                return (
                    True,
                    [
                        SignalLifecycleKernel._event_from_candle(
                            "expired_unfilled",
                            tp_price,
                            candle,
                            evidence_source="binance_futures",
                            matched_level="tp_before_viable_entry",
                        )
                    ],
                    has_entry,
                    needs_limit_entry,
                )

        events: list[ExecutionEvent] = []
        if entry_hit:
            observed_dec = entry_high if direction == "long" else entry_low
            events.append(
                SignalLifecycleKernel._event_from_candle(
                    "entry_hit",
                    observed_dec,
                    candle,
                    evidence_source="binance_futures",
                    matched_level="entry",
                )
            )
            has_entry = True

        if limit_entry_hit:
            observed_dec = max(entry_low, candle.low) if direction == "long" else min(entry_high, candle.high)
            events.append(
                SignalLifecycleKernel._event_from_candle(
                    "limit_entry_hit",
                    observed_dec,
                    candle,
                    evidence_source="binance_futures",
                    matched_level="limit_entry",
                )
            )
            needs_limit_entry = False

        if has_entry:
            for tp_index, tp_price in tp_hits:
                events.append(
                    SignalLifecycleKernel._event_from_candle(
                        "tp_hit",
                        tp_price,
                        candle,
                        tp_index=tp_index,
                        evidence_source="binance_futures",
                        matched_level=f"tp{tp_index + 1}",
                    )
                )
            if sl_hit and stop_loss is not None:
                events.append(
                    SignalLifecycleKernel._event_from_candle(
                        "sl_hit",
                        stop_loss,
                        candle,
                        evidence_source="binance_futures",
                        matched_level="stop_loss",
                    )
                )

        return False, events, has_entry, needs_limit_entry

    @staticmethod
    def detect_candle_events(signal: dict, candles: list[Candle], *, now: Optional[int] = None) -> list[ExecutionEvent]:
        """Return execution events proven by candles; ambiguous same-candle conflicts auto-resolve."""
        if signal.get("signal_type") != "crypto":
            return []
        direction = signal.get("direction")
        if direction not in ("long", "short"):
            return []

        now = int(time.time()) if now is None else now
        if (
            signal.get("expires_at")
            and int(signal["expires_at"]) < now
            and signal.get("entry_execution_mode") != "market_base_limit"
        ):
            if not signal.get("entry_hit_at") and signal.get("status") == "published":
                return [
                    ExecutionEvent(
                        event_type="expired_unfilled",
                        observed_price=None,
                        observed_at=int(signal["expires_at"]),
                        evidence_source="system",
                        evidence={"reason": "expires_at_passed"},
                    )
                ]

        entry_low, entry_high = _entry_bounds(signal)
        if entry_low is None or entry_high is None:
            return []

        targets = _target_prices(signal)
        stop_loss = _dec(signal.get("stop_loss"))
        lifecycle = signal.get("lifecycle_status")
        lifecycle_has_entry = lifecycle in ("entered", "tp_partial", "tp_full", "stopped_out")
        has_entry = lifecycle_has_entry or signal.get("status") == "active"
        is_market_base_limit = signal.get("entry_execution_mode") == "market_base_limit"
        has_limit_entry = bool(signal.get("has_limit_entry_event"))
        has_limit_entry_expired = bool(signal.get("has_limit_entry_expired_update"))
        needs_limit_entry = is_market_base_limit and has_entry and not has_limit_entry and not has_limit_entry_expired
        events, has_entry, needs_limit_entry, reference_price, created_at, scan_terminated = (
            SignalLifecycleKernel._prepare_candle_scan(
                signal,
                candles,
                direction,
                entry_low,
                entry_high,
                is_market_base_limit,
                has_entry,
                has_limit_entry,
                needs_limit_entry,
                now,
            )
        )
        if scan_terminated:
            return events

        for candle in candles:
            if candle.close_time < created_at:
                continue

            terminate, cand_events, has_entry, needs_limit_entry = SignalLifecycleKernel._process_candle(
                candle,
                signal,
                direction,
                entry_low,
                entry_high,
                targets,
                stop_loss,
                has_entry,
                needs_limit_entry,
                reference_price,
                created_at,
            )
            if terminate:
                return cand_events
            events.extend(cand_events)
            if len(events) > 0 and any(
                e.evidence_source != "system" or (e.evidence or {}).get("matched_level") != "market_base"
                for e in events
            ):
                break

        return events

    @staticmethod
    def _detect_tp_hits(signal: dict, candle: Candle, targets: list[Decimal]) -> list[tuple[int, Decimal]]:
        hit_count = int(signal.get("tp_hit_count") or 0)
        direction = signal.get("direction")
        hits: list[tuple[int, Decimal]] = []
        for index, target in enumerate(targets):
            if index < hit_count:
                continue
            matched = candle.high >= target if direction == "long" else candle.low <= target
            if matched:
                hits.append((index, target))
        return hits

    @staticmethod
    def _detect_sl_hit(signal: dict, candle: Candle, stop_loss: Optional[Decimal]) -> bool:
        if stop_loss is None:
            return False
        touched = candle.low <= stop_loss if signal.get("direction") == "long" else candle.high >= stop_loss
        if not touched:
            return False
        effective_at = signal.get("stop_loss_effective_at")
        if effective_at is not None:
            try:
                effective_at_int = int(effective_at)
            except (TypeError, ValueError):
                effective_at_int = 0
            if effective_at_int > 0:
                # 生效时刻在 K 线结束之后 → 止损还没生效
                if candle.close_time <= effective_at_int:
                    return False
                # 生效时刻在 K 线中间 → 保守当作未触达，等下根完整 K 线
                if candle.open_time < effective_at_int < candle.close_time:
                    return False
        return True

    @staticmethod
    def _event_from_candle(
        event_type: str,
        observed_price: Decimal,
        candle: Candle,
        *,
        evidence_source: str,
        matched_level: str,
        tp_index: Optional[int] = None,
    ) -> ExecutionEvent:
        evidence = {
            "provider": "binance_futures",
            "interval": "5m",
            "candle_open_time": candle.open_time,
            "candle_close_time": candle.close_time,
            "open": str(candle.open),
            "high": str(candle.high),
            "low": str(candle.low),
            "close": str(candle.close),
            "matched_level": matched_level,
            "level_price": str(observed_price),
        }
        return ExecutionEvent(
            event_type=event_type,
            observed_price=observed_price,
            observed_at=candle.close_time,
            evidence_source=evidence_source,
            tp_index=tp_index,
            evidence=evidence,
        )
