"""Persistent order lifecycle, stream supervision, and maker-first policy."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, cast

from core.events import OrderAck, OrderRequest, OrderStatus, OrderType, Side
from core.risk import Approved
from core.state import StateStore
from crypto.adapters.base import BrokerOrder, CryptoBrokerAdapter, UserEvent


class LifecycleState(StrEnum):
    CREATED = "CREATED"
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


TERMINAL_STATES = {
    LifecycleState.FILLED,
    LifecycleState.CANCELED,
    LifecycleState.REJECTED,
}


@dataclass(frozen=True, slots=True)
class ManagedOrder:
    client_order_id: str
    venue_order_id: str | None
    symbol: str
    quantity: Decimal
    filled_quantity: Decimal
    state: LifecycleState
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TouchQuote:
    bid: Decimal
    ask: Decimal

    def __post_init__(self) -> None:
        if any(not value.is_finite() or value <= 0 for value in (self.bid, self.ask)):
            raise ValueError("touch prices must be positive finite Decimals")
        if self.bid > self.ask:
            raise ValueError("bid cannot exceed ask")


class HistoricalOrders(Protocol):
    async def get_order_history(self) -> Sequence[BrokerOrder]: ...


class MakerFirstPolicy:
    MAX_REPRICES = 3

    @classmethod
    def plan(
        cls,
        order: OrderRequest,
        quote: TouchQuote,
        *,
        reprices: int,
        urgent: bool,
    ) -> OrderRequest | None:
        if reprices < 0:
            raise ValueError("reprices cannot be negative")
        if reprices <= cls.MAX_REPRICES:
            touch = quote.bid if order.side is Side.BUY else quote.ask
            suffix = "" if reprices == 0 else f":r{reprices}"
            return replace(
                order,
                client_order_id=f"{order.client_order_id}{suffix}",
                order_type=OrderType.LIMIT,
                limit_price=touch,
                expected_price=touch,
            )
        if urgent:
            if not order.is_exit:
                raise ValueError("spread crossing is restricted to urgent exits")
            cross = quote.ask if order.side is Side.BUY else quote.bid
            return replace(
                order,
                client_order_id=f"{order.client_order_id}:urgent",
                order_type=OrderType.MARKET,
                limit_price=None,
                expected_price=cross,
            )
        return None


class OrderManager:
    def __init__(
        self,
        state: StateStore,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._state_store = state
        self._clock = clock
        self._orders = self._load()

    async def submit(self, adapter: CryptoBrokerAdapter, approved: Approved) -> OrderAck:
        client_id = approved.order.client_order_id
        existing = self._orders.get(client_id)
        if existing is not None and existing.state in {
            LifecycleState.ACKNOWLEDGED,
            LifecycleState.OPEN,
            LifecycleState.PARTIALLY_FILLED,
            *TERMINAL_STATES,
        }:
            status = (
                OrderStatus.REJECTED
                if existing.state is LifecycleState.REJECTED
                else OrderStatus.ACCEPTED
            )
            return OrderAck(client_id, existing.venue_order_id, status, self._clock())
        self._orders[client_id] = ManagedOrder(
            client_id,
            None,
            approved.order.symbol,
            approved.order.quantity,
            Decimal("0"),
            LifecycleState.SUBMITTING,
            self._clock(),
        )
        self._persist()
        ack = await adapter.submit_order(approved)
        state = (
            LifecycleState.REJECTED
            if ack.status is OrderStatus.REJECTED
            else LifecycleState.ACKNOWLEDGED
        )
        self._transition(client_id, state, venue_order_id=ack.venue_order_id)
        return ack

    def process_event(self, event: UserEvent) -> None:
        # Coinbase nests orders under events[].orders[]; Kraken v2 uses data[].
        # Recursing also handles normalized REST reconciliation documents.
        for payload in _walk_mappings(event.payload):
            self._apply_mapping(payload)

    async def reconcile(self, adapter: CryptoBrokerAdapter) -> None:
        open_orders = await adapter.get_open_orders()
        snapshots: Sequence[BrokerOrder] = open_orders
        history_method = getattr(adapter, "get_order_history", None)
        if history_method is not None:
            historical = cast(HistoricalOrders, adapter)
            snapshots = await historical.get_order_history()
        seen: set[str] = set()
        for order in snapshots:
            seen.add(order.client_order_id)
            self._apply_broker_order(order)
        for client_id, managed in tuple(self._orders.items()):
            if (
                managed.state
                in {
                    LifecycleState.SUBMITTING,
                    LifecycleState.ACKNOWLEDGED,
                    LifecycleState.OPEN,
                    LifecycleState.PARTIALLY_FILLED,
                }
                and client_id not in seen
            ):
                self._transition(client_id, LifecycleState.UNKNOWN)

    async def monitor(self, adapter: CryptoBrokerAdapter) -> None:
        """Reconnect forever; every disconnect reconciles before reconnecting."""

        while True:
            try:
                async for event in adapter.stream_user_events():
                    self.process_event(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                await self.reconcile(adapter)
                await asyncio.sleep(0)

    def get(self, client_order_id: str) -> ManagedOrder | None:
        return self._orders.get(client_order_id)

    def _apply_broker_order(self, order: BrokerOrder) -> None:
        self._apply_mapping(
            {
                "client_order_id": order.client_order_id,
                "venue_order_id": order.venue_order_id,
                "symbol": order.symbol,
                "status": order.status,
                "quantity": str(order.quantity),
                "filled_quantity": str(order.filled_quantity),
            }
        )

    def _apply_mapping(self, payload: Mapping[str, object]) -> None:
        client_id = payload.get("client_order_id") or payload.get("cl_ord_id")
        if not isinstance(client_id, str) or client_id not in self._orders:
            return
        status = str(payload.get("status") or payload.get("order_status") or "UNKNOWN").upper()
        state = {
            "PENDING": LifecycleState.ACKNOWLEDGED,
            "ACKNOWLEDGED": LifecycleState.ACKNOWLEDGED,
            "OPEN": LifecycleState.OPEN,
            "NEW": LifecycleState.OPEN,
            "PARTIALLY_FILLED": LifecycleState.PARTIALLY_FILLED,
            "FILLED": LifecycleState.FILLED,
            "CANCELED": LifecycleState.CANCELED,
            "CANCELLED": LifecycleState.CANCELED,
            "REJECTED": LifecycleState.REJECTED,
            "FAILED": LifecycleState.REJECTED,
        }.get(status, LifecycleState.UNKNOWN)
        filled_raw = payload.get(
            "filled_quantity",
            payload.get(
                "filled_size",
                payload.get("cumulative_quantity", payload.get("cum_qty", "0")),
            ),
        )
        filled = Decimal(str(filled_raw))
        venue_id = payload.get("venue_order_id") or payload.get("order_id")
        self._transition(
            client_id,
            state,
            filled_quantity=filled,
            venue_order_id=str(venue_id) if venue_id else None,
        )

    def _transition(
        self,
        client_id: str,
        state: LifecycleState,
        *,
        filled_quantity: Decimal | None = None,
        venue_order_id: str | None = None,
    ) -> None:
        current = self._orders[client_id]
        if current.state in TERMINAL_STATES and state not in TERMINAL_STATES:
            return
        filled = current.filled_quantity if filled_quantity is None else filled_quantity
        if filled < 0 or filled > current.quantity:
            raise ValueError("filled quantity is outside order quantity")
        self._orders[client_id] = replace(
            current,
            state=state,
            filled_quantity=filled,
            venue_order_id=venue_order_id or current.venue_order_id,
            updated_at=self._clock(),
        )
        self._persist()

    @property
    def _state_key(self) -> str:
        return "crypto:order_manager"

    def _persist(self) -> None:
        document = {
            client_id: {
                "client_order_id": order.client_order_id,
                "venue_order_id": order.venue_order_id,
                "symbol": order.symbol,
                "quantity": str(order.quantity),
                "filled_quantity": str(order.filled_quantity),
                "state": order.state.value,
                "updated_at": order.updated_at.isoformat(),
            }
            for client_id, order in self._orders.items()
        }
        self._state_store.set(
            self._state_key,
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode(),
        )

    def _load(self) -> dict[str, ManagedOrder]:
        raw = self._state_store.get(self._state_key)
        if raw is None:
            return {}
        document = json.loads(raw)
        return {
            str(client_id): ManagedOrder(
                client_order_id=str(value["client_order_id"]),
                venue_order_id=value["venue_order_id"],
                symbol=str(value["symbol"]),
                quantity=Decimal(str(value["quantity"])),
                filled_quantity=Decimal(str(value["filled_quantity"])),
                state=LifecycleState(value["state"]),
                updated_at=datetime.fromisoformat(value["updated_at"]),
            )
            for client_id, value in document.items()
        }


def _walk_mappings(value: object) -> Sequence[Mapping[str, object]]:
    found: list[Mapping[str, object]] = []

    def visit(item: object) -> None:
        if isinstance(item, Mapping):
            found.append(item)
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return found
