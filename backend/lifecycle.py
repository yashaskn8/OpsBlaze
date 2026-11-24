"""Shared persisted lifecycle classifications for admission and read-only monitors.

These sets classify durable state; they do not authorize transitions or effects.
"""

ACTIVE_STATES = (
    "DISPATCH_PENDING", "INVESTIGATING", "AWAITING_APPROVAL",
    "PLAN_DECIDED", "EXECUTING", "CANCEL_REQUESTED", "REVIEW_ACKNOWLEDGED",
)
TERMINAL_STATES = ("VERIFIED", "FAILED", "NEEDS_REVIEW", "CANCELLED")
UNRESOLVED_EFFECT_STATES = ("SENT", "UNKNOWN", "CONFIRMED")
