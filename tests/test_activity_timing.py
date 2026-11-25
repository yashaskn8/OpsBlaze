"""B4: real attempt scheduling delay, never a fabricated zero on bad clocks."""

from datetime import datetime, timedelta, timezone

import pytest

from backend.workflows.activity_timing import schedule_to_start_ms


NOW = datetime(2026, 11, 25, tzinfo=timezone.utc)


@pytest.mark.parametrize("started, expected", [
    (NOW, 0),
    (NOW + timedelta(milliseconds=1234, microseconds=999), 1234),
    (NOW + timedelta(days=1, seconds=3), 86403000),
    (NOW.astimezone(timezone(timedelta(hours=5, minutes=30))), 0),
    (NOW - timedelta(microseconds=1), None),
    (NOW.replace(tzinfo=None), None),
    (None, None),
])
def test_attempt_schedule_to_start(started, expected):
    assert schedule_to_start_ms(NOW, started) == expected


def test_missing_schedule_timestamp_is_not_reported_as_zero():
    assert schedule_to_start_ms(None, NOW) is None
