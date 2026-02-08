from __future__ import annotations

from datetime import datetime, timedelta, timezone

from demi.jobs.worker import EventWorker


def test_event_retry_policy_defaults_without_scheduler_metadata():
    retry_after, max_attempts = EventWorker._event_retry_policy({}, default_retry_after=7)

    assert retry_after == 7
    assert max_attempts == 5


def test_event_retry_policy_uses_scheduler_retry_window_future():
    retry_until = datetime.now(tz=timezone.utc) + timedelta(minutes=5)
    payload = {
        "payload": {
            "_scheduler": {
                "retry_backoff_seconds": 13,
                "retry_until": retry_until.isoformat(),
            }
        }
    }

    retry_after, max_attempts = EventWorker._event_retry_policy(payload, default_retry_after=7)

    assert retry_after == 13
    assert max_attempts == 1000000


def test_event_retry_policy_stops_retrying_after_window_expires():
    retry_until = datetime.now(tz=timezone.utc) - timedelta(seconds=1)
    payload = {
        "payload": {
            "_scheduler": {
                "retry_backoff_seconds": 11,
                "retry_until": retry_until.isoformat(),
            }
        }
    }

    retry_after, max_attempts = EventWorker._event_retry_policy(payload, default_retry_after=7)

    assert retry_after == 11
    assert max_attempts == 1
