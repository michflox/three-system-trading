"""Injectable timezone-aware clocks and trading calendars."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast

import exchange_calendars as xcals  # type: ignore[import-untyped]


def require_aware(value: datetime) -> datetime:
    """Validate and return a timezone-aware datetime."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value


class Clock(Protocol):
    def now(self) -> datetime:
        """Return the current timezone-aware instant."""


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class FixedClock:
    instant: datetime

    def __post_init__(self) -> None:
        require_aware(self.instant)

    def now(self) -> datetime:
        return self.instant


class TradingCalendar(Protocol):
    @property
    def name(self) -> str: ...

    def is_open(self, instant: datetime) -> bool: ...


class ExchangeTradingCalendar:
    """Adapter for an exchange_calendars regular-trading-hours calendar."""

    def __init__(self, calendar_name: str) -> None:
        self._name = calendar_name
        self._calendar: Any = xcals.get_calendar(calendar_name)

    @property
    def name(self) -> str:
        return self._name

    def is_open(self, instant: datetime) -> bool:
        aware = require_aware(instant).astimezone(UTC)
        return cast(bool, self._calendar.is_open_on_minute(aware, ignore_breaks=False))


@dataclass(frozen=True, slots=True)
class CryptoCalendar:
    """A continuously open 24/7 crypto calendar."""

    name: str = "24/7"

    def is_open(self, instant: datetime) -> bool:
        require_aware(instant)
        return True


def cme_calendar() -> ExchangeTradingCalendar:
    return ExchangeTradingCalendar("CMES")


def nyse_calendar() -> ExchangeTradingCalendar:
    return ExchangeTradingCalendar("XNYS")


def crypto_calendar() -> CryptoCalendar:
    return CryptoCalendar()
