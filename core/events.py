"""Immutable domain events shared by backtest and live engines."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


class OrderStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    PENDING = "PENDING"


class SignalAction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    EXIT = "EXIT"


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _require_decimal(value: Decimal | None, field_name: str) -> None:
    if value is not None and not isinstance(value, Decimal):
        raise TypeError(f"{field_name} must be Decimal, not {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        _require_aware(self.timestamp, "timestamp")
        for name in ("open", "high", "low", "close", "volume"):
            _require_decimal(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class Fill:
    order_id: str
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    fee: Decimal
    timestamp: datetime

    def __post_init__(self) -> None:
        _require_aware(self.timestamp, "timestamp")
        for name in ("quantity", "price", "fee"):
            _require_decimal(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class OrderRequest:
    client_order_id: str
    symbol: str
    side: Side
    quantity: Decimal
    order_type: OrderType
    created_at: datetime
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    strategy_id: str = ""
    expected_price: Decimal | None = None
    is_exit: bool = False

    def __post_init__(self) -> None:
        _require_aware(self.created_at, "created_at")
        for name in ("quantity", "limit_price", "stop_price", "expected_price"):
            _require_decimal(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class OrderAck:
    client_order_id: str
    venue_order_id: str | None
    status: OrderStatus
    acknowledged_at: datetime
    reason: str | None = None

    def __post_init__(self) -> None:
        _require_aware(self.acknowledged_at, "acknowledged_at")


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    quantity: Decimal
    average_entry_price: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_aware(self.updated_at, "updated_at")
        for name in (
            "quantity",
            "average_entry_price",
            "realized_pnl",
            "unrealized_pnl",
        ):
            _require_decimal(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class Signal:
    strategy_id: str
    symbol: str
    action: SignalAction
    generated_at: datetime
    quantity: Decimal | None = None
    reference_price: Decimal | None = None

    def __post_init__(self) -> None:
        _require_aware(self.generated_at, "generated_at")
        _require_decimal(self.quantity, "quantity")
        _require_decimal(self.reference_price, "reference_price")
