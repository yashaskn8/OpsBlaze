"""Structured telemetry events, secret redaction, and metrics registry for B1."""

from __future__ import annotations

import contextvars
import json
import logging
import re
import threading
import uuid
from typing import Any

logger = logging.getLogger("opsblaze.b1")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

correlation_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)

_SECRET_KEY_PATTERN = re.compile(
    r"(password|secret|token|api_key|apikey|authorization|credential|dsn)",
    re.IGNORECASE,
)
_SECRET_VALUE_PATTERN = re.compile(
    r"(postgresql(?:\+asyncpg)?://[^:\s]+:)([^@\s]+)(@)",
    re.IGNORECASE,
)


def get_correlation_id() -> str:
    cid = correlation_id_ctx.get()
    if not cid:
        cid = str(uuid.uuid4())
        correlation_id_ctx.set(cid)
    return cid


def redact_value(val: Any) -> Any:
    """Recursively redact secrets and sanitize control characters against log injection."""
    if isinstance(val, str):
        # Strip CR/LF/NUL to prevent log injection
        sanitized = val.replace("\r", "\\r").replace("\n", "\\n").replace("\x00", "")
        return _SECRET_VALUE_PATTERN.sub(r"\1[REDACTED]\3", sanitized)
    if isinstance(val, dict):
        out: dict[str, Any] = {}
        for k, v in val.items():
            key_str = str(k)
            if _SECRET_KEY_PATTERN.search(key_str):
                out[key_str] = "[REDACTED]"
            else:
                out[key_str] = redact_value(v)
        return out
    if isinstance(val, (list, tuple)):
        return [redact_value(item) for item in val]
    return val


REQUIRED_B1_EVENTS: frozenset[str] = frozenset(
    {
        "incident.admitted",
        "outbox.claimed",
        "outbox.delivered",
        "outbox.retry",
        "outbox.stuck",
        "workflow.started",
        "workflow.completed",
        "workflow.failed",
        "workflow.cancelled",
        "activity.retry",
    }
)

REQUIRED_STAGE3_EVENTS: frozenset[str] = frozenset(
    {
        # B2 events
        "reservation.conflict",
        "fence.stale_reject",
        "target.version_conflict",
        "lease.expired",
        # B3 events
        "effect.unknown_age",
        "reconciliation.attempt",
        "effect.duplicate_reject",
        "verifier.false_success",
        # E8 events
        "adapter.conformance_pass",
        "adapter.stale_fence_violation",
        "adapter.duplicate_effect",
        "adapter.lookup_mismatch",
        "adapter.retention_gap",
        "adapter.enablement_revoked",
    }
)


class MetricsRegistry:
    """Thread-safe metrics and structured event buffer for B1, B2, B3, and E8 observability.

    No unbounded tenant identifiers are permitted in metric labels.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {
            # B1 counters
            "admission_requests_total": 0,
            "admission_created_total": 0,
            "admission_replayed_total": 0,
            "idempotency_conflicts_total": 0,
            "outbox_claimed_total": 0,
            "outbox_delivered_total": 0,
            "outbox_retries_total": 0,
            "outbox_stuck_total": 0,
            "dispatcher_failures_total": 0,
            "workflow_started_total": 0,
            "workflow_completed_total": 0,
            "workflow_failures_total": 0,
            "workflow_cancelled_total": 0,
            "activity_retries_total": 0,
            # B2 counters
            "reservation_acquired_total": 0,
            "reservation_conflicts_total": 0,
            "fence_stale_rejects_total": 0,
            "target_version_conflicts_total": 0,
            "lease_expired_total": 0,
            # B3 counters
            "effect_prepared_total": 0,
            "effect_sent_total": 0,
            "effect_unknown_total": 0,
            "effect_confirmed_total": 0,
            "effect_verified_total": 0,
            "effect_failed_total": 0,
            "effect_needs_review_total": 0,
            "reconciliation_attempts_total": 0,
            "effect_duplicate_rejects_total": 0,
            "verifier_false_success_total": 0,
            # E8 counters
            "adapter_conformance_pass_total": 0,
            "adapter_conformance_fail_total": 0,
            "adapter_stale_fence_violations_total": 0,
            "adapter_duplicate_effects_total": 0,
            "adapter_lookup_mismatches_total": 0,
            "adapter_retention_gaps_total": 0,
            "adapter_enablement_revoked_total": 0,
        }
        self._gauges: dict[str, float] = {
            "outbox_age_seconds": 0.0,
            "workflow_schedule_to_start_seconds": 0.0,
            "stuck_workflow_age_seconds": 0.0,
            "projection_lag_seconds": 0.0,
            "effect_unknown_age_seconds": 0.0,
        }
        self._events: list[dict[str, Any]] = []
        self._max_events = 2000

    def inc(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + amount

    def set_gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = max(0.0, float(value))

    def record_event(self, payload: dict[str, Any]) -> None:
        with self._lock:
            if len(self._events) >= self._max_events:
                self._events.pop(0)
            self._events.append(payload)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
                "recent_events_count": len(self._events),
            }

    def get_events(self, event_name: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if event_name is None:
                return list(self._events)
            return [e for e in self._events if e.get("event") == event_name]

    def reset_for_tests(self) -> None:
        with self._lock:
            for k in self._counters:
                self._counters[k] = 0
            for k in self._gauges:
                self._gauges[k] = 0.0
            self._events.clear()


metrics = MetricsRegistry()


def emit_event(
    event_name: str,
    *,
    incident_id: str | uuid.UUID | None = None,
    workflow_id: str | None = None,
    correlation_id: str | None = None,
    level: str = "INFO",
    **fields: Any,
) -> dict[str, Any]:
    """Emit a structured, secret-redacted observability event and record it."""
    cid = correlation_id or get_correlation_id()
    record: dict[str, Any] = {
        "event": redact_value(event_name),
        "level": level,
        "correlation_id": redact_value(cid),
    }
    if incident_id is not None:
        record["incident_id"] = str(incident_id)
    if workflow_id is not None:
        record["workflow_id"] = redact_value(workflow_id)
    for k, v in fields.items():
        if _SECRET_KEY_PATTERN.search(k):
            record[k] = "[REDACTED]"
        else:
            record[k] = redact_value(v)

    metrics.record_event(record)
    logger.info(json.dumps(record, default=str))
    return record
