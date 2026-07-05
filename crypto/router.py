"""Capability-aware routing with reduce-first, reconcile, then migrate failover."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from core.events import OrderRequest, Side
from core.risk import Approved
from crypto.adapters.base import CryptoBrokerAdapter
from crypto.capabilities import VenueCapabilities
from crypto.health import HealthState, VenueHealth
from ops.journal import AppendOnlyJournal


class InstrumentType(StrEnum):
    SPOT = "spot"
    PERPETUAL = "perpetual"


@dataclass(frozen=True, slots=True)
class RouteDecision:
    venue: str | None
    adapter: CryptoBrokerAdapter | None
    approved: Approved | None
    fitted_order: OrderRequest | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class PendingMigration:
    failed_venue: str
    instrument_type: InstrumentType
    approved: Approved


class ExecutionRouter:
    PRIORITIES: Mapping[InstrumentType, tuple[str, ...]] = {
        InstrumentType.PERPETUAL: ("coinbase",),
        InstrumentType.SPOT: ("coinbase", "kraken"),
    }

    def __init__(
        self,
        adapters: Mapping[str, CryptoBrokerAdapter],
        capabilities: Mapping[str, VenueCapabilities],
        health: Mapping[str, VenueHealth],
        journal: AppendOnlyJournal,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._adapters = dict(adapters)
        self._capabilities = dict(capabilities)
        self._health = dict(health)
        self._journal = journal
        self._clock = clock
        self._dirty: dict[str, set[str]] = {}
        self._pending: list[PendingMigration] = []

    def route(
        self,
        approved: Approved,
        instrument_type: InstrumentType,
        *,
        current_position: Decimal,
        exclude: frozenset[str] = frozenset(),
    ) -> RouteDecision:
        order = approved.order
        if instrument_type is InstrumentType.SPOT:
            projected = current_position + (
                order.quantity if order.side is Side.BUY else -order.quantity
            )
            if projected < 0:
                fitted = None
                if current_position > 0:
                    fitted = replace(
                        order,
                        client_order_id=f"{order.client_order_id}:fit-flat",
                        quantity=current_position,
                        is_exit=True,
                    )
                self._journal.append(
                    {
                        "event": "capability_fit_flat",
                        "timestamp": self._clock().isoformat(),
                        "symbol": order.symbol,
                        "client_order_id": order.client_order_id,
                        "requested_quantity": str(order.quantity),
                        "current_position": str(current_position),
                        "reason": "spot_short_unsupported",
                    }
                )
                return RouteDecision(None, None, None, fitted, "spot_short_unsupported")

        for venue in self.PRIORITIES[instrument_type]:
            if venue in exclude:
                continue
            adapter = self._adapters.get(venue)
            capability = self._capabilities.get(venue)
            monitor = self._health.get(venue)
            if adapter is None or capability is None or monitor is None:
                continue
            if monitor.state is not HealthState.HEALTHY:
                continue
            supports_type = (
                capability.spot
                if instrument_type is InstrumentType.SPOT
                else capability.perpetual_futures
            )
            if not supports_type:
                continue
            if capability.active_symbols and order.symbol not in capability.active_symbols:
                continue
            return RouteDecision(venue, adapter, approved)
        self._journal.append(
            {
                "event": "order_unroutable",
                "timestamp": self._clock().isoformat(),
                "symbol": order.symbol,
                "client_order_id": order.client_order_id,
                "instrument_type": instrument_type.value,
            }
        )
        return RouteDecision(None, None, None, reason="no_healthy_capable_venue")

    async def failover(
        self,
        failed_venue: str,
        flatten_orders: Sequence[Approved],
        migrations: Sequence[tuple[InstrumentType, Approved]],
    ) -> None:
        adapter = self._adapters[failed_venue]
        monitor = self._health[failed_venue]
        if monitor.state is not HealthState.FAILED:
            monitor.force_failed("router_failover")
        for approved in flatten_orders:
            symbol = approved.order.symbol
            self._dirty.setdefault(failed_venue, set()).add(symbol)
            try:
                await adapter.submit_order(approved)
                outcome = "submitted"
            except Exception as error:  # best effort is deliberate during venue failure
                outcome = f"failed:{type(error).__name__}"
            self._journal.append(
                {
                    "event": "failover_flatten_attempt",
                    "timestamp": self._clock().isoformat(),
                    "venue": failed_venue,
                    "symbol": symbol,
                    "outcome": outcome,
                }
            )
        monitor.quarantine("venue_failure")
        self._pending.extend(
            PendingMigration(failed_venue, instrument_type, approved)
            for instrument_type, approved in migrations
        )

    async def reconcile_and_migrate(self, failed_venue: str) -> bool:
        adapter = self._adapters[failed_venue]
        positions = {
            position.symbol: position.quantity for position in await adapter.get_positions()
        }
        dirty = self._dirty.get(failed_venue, set())
        if any(positions.get(symbol, Decimal("0")) != 0 for symbol in dirty):
            self._journal.append(
                {
                    "event": "migration_blocked_position_open",
                    "timestamp": self._clock().isoformat(),
                    "venue": failed_venue,
                    "symbols": sorted(dirty),
                }
            )
            return False

        pending = [item for item in self._pending if item.failed_venue == failed_venue]
        for item in pending:
            decision = self.route(
                item.approved,
                item.instrument_type,
                current_position=Decimal("0"),
                exclude=frozenset({failed_venue}),
            )
            if decision.adapter is None or decision.approved is None or decision.venue is None:
                return False
            await decision.adapter.submit_order(decision.approved)
            self._journal.append(
                {
                    "event": "migration_submitted",
                    "timestamp": self._clock().isoformat(),
                    "from_venue": failed_venue,
                    "to_venue": decision.venue,
                    "symbol": decision.approved.order.symbol,
                }
            )
        self._pending = [item for item in self._pending if item.failed_venue != failed_venue]
        self._dirty.pop(failed_venue, None)
        return True

    @property
    def dirty_positions(self) -> Mapping[str, frozenset[str]]:
        return {venue: frozenset(symbols) for venue, symbols in self._dirty.items()}
