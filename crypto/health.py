"""Deterministic venue-health state machine evaluated on a 30-second cadence."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum


class HealthState(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    QUARANTINED = "QUARANTINED"


@dataclass(frozen=True, slots=True)
class HealthPolicy:
    check_interval: timedelta = timedelta(seconds=30)
    ws_stale_after: timedelta = timedelta(seconds=45)
    book_stale_after: timedelta = timedelta(seconds=10)
    rest_stale_after: timedelta = timedelta(seconds=45)
    error_window: timedelta = timedelta(minutes=5)
    degraded_error_rate: float = 0.20
    failed_error_rate: float = 0.50
    minimum_error_samples: int = 5


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    venue: str
    state: HealthState
    reasons: tuple[str, ...]
    checked_at: datetime
    error_rate: float


class VenueHealth:
    def __init__(
        self,
        venue: str,
        *,
        policy: HealthPolicy | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.venue = venue
        self.policy = policy or HealthPolicy()
        self._clock = clock
        self._state = HealthState.HEALTHY
        self._last_check: datetime | None = None
        self._last_rest: datetime | None = None
        self._rest_ok = False
        self._last_ws: datetime | None = None
        self._last_book: datetime | None = None
        self._requests: deque[tuple[datetime, bool]] = deque()
        self._quarantine_reason: str | None = None

    @property
    def state(self) -> HealthState:
        return self._state

    def record_rest(self, ok: bool, at: datetime | None = None) -> None:
        observed = _aware(at or self._clock())
        self._last_rest = observed
        self._rest_ok = ok
        self.record_request(ok, observed)

    def record_ws_heartbeat(self, at: datetime | None = None) -> None:
        self._last_ws = _aware(at or self._clock())

    def record_book(self, at: datetime | None = None) -> None:
        self._last_book = _aware(at or self._clock())

    def record_request(self, ok: bool, at: datetime | None = None) -> None:
        self._requests.append((_aware(at or self._clock()), ok))

    def check(self, now: datetime | None = None, *, force: bool = False) -> HealthSnapshot:
        checked_at = _aware(now or self._clock())
        if (
            not force
            and self._last_check is not None
            and checked_at - self._last_check < self.policy.check_interval
        ):
            return self._snapshot(checked_at, ())
        self._last_check = checked_at
        self._trim_errors(checked_at)
        rate = self._error_rate()
        degraded: list[str] = []
        severe: list[str] = []
        self._classify_age(
            "rest", self._last_rest, self.policy.rest_stale_after, checked_at, degraded, severe
        )
        self._classify_age(
            "websocket", self._last_ws, self.policy.ws_stale_after, checked_at, degraded, severe
        )
        self._classify_age(
            "book", self._last_book, self.policy.book_stale_after, checked_at, degraded, severe
        )
        if not self._rest_ok:
            degraded.append("rest_status_failed")
            if self._last_rest is not None:
                severe.append("rest_status_failed")
        if len(self._requests) >= self.policy.minimum_error_samples:
            if rate >= self.policy.failed_error_rate:
                severe.append("error_rate_failed")
            elif rate >= self.policy.degraded_error_rate:
                degraded.append("error_rate_degraded")

        reasons: tuple[str, ...]
        if self._state is HealthState.QUARANTINED:
            reasons = (self._quarantine_reason or "manual_quarantine",)
        elif severe:
            # Never skip a state: a severe first observation degrades, and a
            # subsequent 30-second check advances to FAILED.
            self._state = (
                HealthState.FAILED
                if self._state in {HealthState.DEGRADED, HealthState.FAILED}
                else HealthState.DEGRADED
            )
            reasons = tuple(sorted(set(severe + degraded)))
        elif degraded:
            self._state = HealthState.DEGRADED
            reasons = tuple(sorted(set(degraded)))
        else:
            self._state = HealthState.HEALTHY
            reasons = ()
        return HealthSnapshot(self.venue, self._state, reasons, checked_at, rate)

    def quarantine(self, reason: str) -> None:
        if self._state is not HealthState.FAILED:
            raise RuntimeError("only a FAILED venue can be quarantined")
        self._state = HealthState.QUARANTINED
        self._quarantine_reason = reason

    def manual_rearm(self) -> None:
        """The only operation allowed to leave QUARANTINED."""

        if self._state is not HealthState.QUARANTINED:
            raise RuntimeError("venue is not quarantined")
        self._state = HealthState.DEGRADED
        self._quarantine_reason = None
        self._last_check = None

    def force_failed(self, reason: str = "forced_failure") -> HealthSnapshot:
        self._state = HealthState.FAILED
        now = _aware(self._clock())
        return HealthSnapshot(self.venue, self._state, (reason,), now, self._error_rate())

    def _trim_errors(self, now: datetime) -> None:
        cutoff = now - self.policy.error_window
        while self._requests and self._requests[0][0] < cutoff:
            self._requests.popleft()

    def _error_rate(self) -> float:
        if not self._requests:
            return 0.0
        failures = sum(not ok for _, ok in self._requests)
        return failures / len(self._requests)

    @staticmethod
    def _classify_age(
        name: str,
        observed: datetime | None,
        threshold: timedelta,
        now: datetime,
        degraded: list[str],
        severe: list[str],
    ) -> None:
        if observed is None:
            severe.append(f"{name}_missing")
            return
        age = now - observed
        if age >= threshold * 2:
            severe.append(f"{name}_stale")
        elif age >= threshold:
            degraded.append(f"{name}_stale")

    def _snapshot(self, now: datetime, reasons: tuple[str, ...]) -> HealthSnapshot:
        return HealthSnapshot(self.venue, self._state, reasons, now, self._error_rate())


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("health timestamps must be timezone-aware")
    return value
