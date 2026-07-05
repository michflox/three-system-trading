from datetime import UTC, datetime, timedelta

import pytest

from crypto.health import HealthState, VenueHealth


def test_health_progression_quarantine_and_manual_only_rearm() -> None:
    now = [datetime(2026, 7, 5, tzinfo=UTC)]
    health = VenueHealth("coinbase", clock=lambda: now[0])
    health.record_rest(True)
    health.record_ws_heartbeat()
    health.record_book()
    assert health.check(force=True).state is HealthState.HEALTHY

    now[0] += timedelta(seconds=100)
    assert health.check(force=True).state is HealthState.DEGRADED
    now[0] += timedelta(seconds=30)
    assert health.check(force=True).state is HealthState.FAILED
    health.quarantine("chaos")
    assert health.state is HealthState.QUARANTINED

    health.record_rest(True)
    health.record_ws_heartbeat()
    health.record_book()
    assert health.check(force=True).state is HealthState.QUARANTINED
    with pytest.raises(RuntimeError):
        health.quarantine("again")
    health.manual_rearm()
    assert health.check(force=True).state is HealthState.HEALTHY


def test_checks_are_throttled_to_thirty_seconds_and_error_window_degrades() -> None:
    now = [datetime(2026, 7, 5, tzinfo=UTC)]
    health = VenueHealth("kraken", clock=lambda: now[0])
    health.record_rest(True)
    health.record_ws_heartbeat()
    health.record_book()
    health.check(force=True)
    for _ in range(5):
        health.record_request(False)
    now[0] += timedelta(seconds=10)
    assert health.check().state is HealthState.HEALTHY
    now[0] += timedelta(seconds=20)
    assert health.check().state is HealthState.DEGRADED
