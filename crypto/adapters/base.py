"""Venue-neutral broker contract used by crypto execution routers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from core.events import OrderAck, Position
from core.risk import Approved


class CapabilityUnsupported(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Balance:
    currency: str
    available: Decimal
    held: Decimal


@dataclass(frozen=True, slots=True)
class BrokerOrder:
    client_order_id: str
    venue_order_id: str
    symbol: str
    status: str
    quantity: Decimal
    filled_quantity: Decimal


@dataclass(frozen=True, slots=True)
class ProductSpec:
    symbol: str
    product_type: str
    price_increment: Decimal
    size_increment: Decimal
    minimum_size: Decimal
    minimum_notional: Decimal
    trading_enabled: bool


@dataclass(frozen=True, slots=True)
class UserEvent:
    source: str
    kind: str
    observed_at: datetime
    payload: Mapping[str, object]


class CryptoBrokerAdapter(ABC):
    """All mutating methods consume a RiskManager ``Approved`` decision."""

    @abstractmethod
    async def verify_permissions(self) -> None:
        """Fail unless the key can trade and cannot transfer/withdraw."""

    @abstractmethod
    async def submit_order(self, approved: Approved) -> OrderAck:
        pass

    @abstractmethod
    async def cancel_order(self, venue_order_id: str, approved: Approved) -> bool:
        pass

    @abstractmethod
    async def get_open_orders(self) -> Sequence[BrokerOrder]:
        pass

    @abstractmethod
    async def get_balances(self) -> Sequence[Balance]:
        pass

    @abstractmethod
    async def get_positions(self) -> Sequence[Position]:
        pass

    @abstractmethod
    async def get_product_specs(self) -> Sequence[ProductSpec]:
        pass

    @abstractmethod
    def stream_user_events(self, product_ids: Sequence[str] = ()) -> AsyncIterator[UserEvent]:
        pass

    @abstractmethod
    async def close(self) -> None:
        pass
