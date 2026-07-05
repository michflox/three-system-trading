from datetime import UTC, datetime

import pytest

from core.clock import FixedClock, cme_calendar, crypto_calendar, nyse_calendar


def test_clocks_require_timezone_awareness() -> None:
    instant = datetime(2026, 7, 6, 15, 0, tzinfo=UTC)
    assert FixedClock(instant).now() == instant
    with pytest.raises(ValueError, match="timezone-aware"):
        FixedClock(datetime(2026, 7, 6, 15, 0))


def test_crypto_calendar_is_always_open_and_rejects_naive_time() -> None:
    calendar = crypto_calendar()
    assert calendar.is_open(datetime(2026, 1, 1, tzinfo=UTC))
    assert calendar.is_open(datetime(2026, 7, 4, tzinfo=UTC))
    with pytest.raises(ValueError, match="timezone-aware"):
        calendar.is_open(datetime(2026, 1, 1))


def test_nyse_calendar_regular_session_and_weekend() -> None:
    calendar = nyse_calendar()
    assert calendar.is_open(datetime(2026, 7, 6, 15, 0, tzinfo=UTC))
    assert not calendar.is_open(datetime(2026, 7, 5, 15, 0, tzinfo=UTC))


def test_cme_calendar_available_and_observes_weekend() -> None:
    calendar = cme_calendar()
    assert calendar.name == "CMES"
    assert not calendar.is_open(datetime(2026, 7, 4, 15, 0, tzinfo=UTC))
