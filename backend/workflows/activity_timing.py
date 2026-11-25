"""Diagnostic timing from Temporal's activity-attempt timestamps."""

from datetime import datetime


def schedule_to_start_ms(scheduled: datetime, started: datetime) -> int | None:
    """Return elapsed milliseconds, omitting invalid or skewed timestamps."""
    if not isinstance(scheduled, datetime) or not isinstance(started, datetime):
        return None
    if scheduled.utcoffset() is None or started.utcoffset() is None:
        return None
    elapsed = started - scheduled
    if elapsed.days < 0:
        return None
    return (elapsed.days * 86400 + elapsed.seconds) * 1000 + elapsed.microseconds // 1000
