"""Persistent paper broker using live quotes and conservative fill semantics."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from core.events import OrderAck, OrderStatus, OrderType, Position, Side
from core.risk import Approved
from core.state import StateStore
from crypto.adapters.base import (
    Balance,
    BrokerOrder,
    CryptoBrokerAdapter,
    ProductSpec,
    UserEvent,
)

ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class LiveQuote:
    symbol: str
    bid: Decimal
    ask: Decimal
    last_trade: Decimal
    timestamp: datetime

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("quote timestamp must be timezone-aware")
        if any(
            not value.is_finite() or value <= 0 for value in (self.bid, self.ask, self.last_trade)
        ):
            raise ValueError("quote prices must be positive finite Decimals")
        if self.bid > self.ask:
            raise ValueError("bid cannot exceed ask")


@dataclass(frozen=True, slots=True)
class PaperFill:
    client_order_id: str
    venue_order_id: str
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    fee: Decimal
    maker: bool
    timestamp: datetime


class PaperStreamDisconnected(ConnectionError):
    pass


class _Disconnect:
    pass


class PaperAdapter(CryptoBrokerAdapter):
    def __init__(
        self,
        venue: str,
        state: StateStore,
        *,
        maker_fee: Decimal,
        taker_fee: Decimal,
        initial_cash: Decimal = Decimal("100000"),
        product_specs: Sequence[ProductSpec] = (),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        crash_after_accept_once: bool = False,
    ) -> None:
        for value in (maker_fee, taker_fee, initial_cash):
            if not value.is_finite() or value < 0:
                raise ValueError("paper rates and cash must be non-negative finite Decimals")
        self.venue = venue
        self._state = state
        self._maker_fee = maker_fee
        self._taker_fee = taker_fee
        self._initial_cash = initial_cash
        self._specs = tuple(product_specs)
        self._clock = clock
        self._crash_once = crash_after_accept_once
        self._events: asyncio.Queue[UserEvent | _Disconnect] = asyncio.Queue()
        self._quotes: dict[str, LiveQuote] = {}
        self._orders, self._fills, self._positions, self._cash = self._load()

    async def verify_permissions(self) -> None:
        return None

    async def submit_order(self, approved: Approved) -> OrderAck:
        order = approved.order
        prior = self._orders.get(order.client_order_id)
        if prior is not None:
            return OrderAck(
                order.client_order_id,
                str(prior["venue_order_id"]),
                OrderStatus.ACCEPTED,
                self._clock(),
            )
        venue_order_id = f"{self.venue}-paper-{len(self._orders) + 1}"
        self._orders[order.client_order_id] = {
            "client_order_id": order.client_order_id,
            "venue_order_id": venue_order_id,
            "symbol": order.symbol,
            "side": order.side.value,
            "quantity": str(order.quantity),
            "filled_quantity": "0",
            "status": "OPEN",
            "order_type": order.order_type.value,
            "limit_price": None if order.limit_price is None else str(order.limit_price),
        }
        quote = self._quotes.get(order.symbol)
        filled_immediately = False
        if order.order_type is OrderType.MARKET:
            if quote is None:
                raise ValueError("paper market order requires a current live quote")
            self._fill(order.client_order_id, quote, maker=False)
            filled_immediately = True
        self._persist()
        if filled_immediately:
            await self._emit(self._orders[order.client_order_id], "FILLED")
        if self._crash_once:
            self._crash_once = False
            raise ConnectionError("simulated crash after paper venue accepted order")
        return OrderAck(
            order.client_order_id,
            venue_order_id,
            OrderStatus.ACCEPTED,
            self._clock(),
        )

    async def cancel_order(self, venue_order_id: str, approved: Approved) -> bool:
        if not approved.order.is_exit:
            raise ValueError("cancel authorization must be an approved exit/control order")
        for order in self._orders.values():
            if order["venue_order_id"] == venue_order_id and order["status"] == "OPEN":
                order["status"] = "CANCELED"
                self._persist()
                await self._emit(order, "CANCELED")
                return True
        return False

    async def get_open_orders(self) -> Sequence[BrokerOrder]:
        return tuple(
            self._broker_order(order)
            for order in self._orders.values()
            if order["status"] in {"OPEN", "PARTIALLY_FILLED"}
        )

    async def get_order_history(self) -> Sequence[BrokerOrder]:
        return tuple(self._broker_order(order) for order in self._orders.values())

    async def get_balances(self) -> Sequence[Balance]:
        balances = [Balance("USD", self._cash, ZERO)]
        balances.extend(
            Balance(symbol.split("-")[0], quantity, ZERO)
            for symbol, quantity in sorted(self._positions.items())
            if quantity != 0
        )
        return tuple(balances)

    async def get_positions(self) -> Sequence[Position]:
        return tuple(
            Position(
                symbol=symbol,
                quantity=quantity,
                average_entry_price=ZERO,
                realized_pnl=ZERO,
                unrealized_pnl=ZERO,
                updated_at=self._clock(),
            )
            for symbol, quantity in sorted(self._positions.items())
            if quantity != 0
        )

    async def get_product_specs(self) -> Sequence[ProductSpec]:
        return self._specs

    async def stream_user_events(self, product_ids: Sequence[str] = ()) -> AsyncIterator[UserEvent]:
        allowed = set(product_ids)
        while True:
            event = await self._events.get()
            if isinstance(event, _Disconnect):
                raise PaperStreamDisconnected("simulated WebSocket loss")
            symbol = event.payload.get("symbol")
            if not allowed or symbol in allowed or event.kind == "heartbeat":
                yield event

    async def close(self) -> None:
        return None

    async def update_quote(self, quote: LiveQuote) -> tuple[PaperFill, ...]:
        self._quotes[quote.symbol] = quote
        before = len(self._fills)
        for client_id, order in tuple(self._orders.items()):
            if order["symbol"] != quote.symbol or order["status"] != "OPEN":
                continue
            if order["order_type"] != OrderType.LIMIT.value:
                continue
            limit = Decimal(str(order["limit_price"]))
            side = Side(str(order["side"]))
            trade_through = (
                quote.last_trade < limit if side is Side.BUY else quote.last_trade > limit
            )
            if trade_through:
                self._fill(client_id, quote, maker=True)
        self._persist()
        for fill in self._fills[before:]:
            await self._emit(self._orders[fill.client_order_id], "FILLED")
        return tuple(self._fills[before:])

    async def disconnect_stream(self) -> None:
        await self._events.put(_Disconnect())

    def set_quote(self, quote: LiveQuote) -> None:
        """Set an initial quote without processing resting orders."""

        self._quotes[quote.symbol] = quote

    @property
    def fills(self) -> tuple[PaperFill, ...]:
        return tuple(self._fills)

    def _fill(self, client_id: str, quote: LiveQuote, *, maker: bool) -> None:
        order = self._orders[client_id]
        side = Side(str(order["side"]))
        quantity = Decimal(str(order["quantity"]))
        if maker:
            price = Decimal(str(order["limit_price"]))
            rate = self._maker_fee
        else:
            # Crossing at ask/bid pays the complete quoted spread, plus taker fee.
            price = quote.ask if side is Side.BUY else quote.bid
            rate = self._taker_fee
        fee = quantity * price * rate
        signed = quantity if side is Side.BUY else -quantity
        self._positions[str(order["symbol"])] = (
            self._positions.get(str(order["symbol"]), ZERO) + signed
        )
        self._cash += (-quantity * price - fee) if side is Side.BUY else (quantity * price - fee)
        order["status"] = "FILLED"
        order["filled_quantity"] = str(quantity)
        self._fills.append(
            PaperFill(
                client_id,
                str(order["venue_order_id"]),
                str(order["symbol"]),
                side,
                quantity,
                price,
                fee,
                maker,
                quote.timestamp,
            )
        )

    async def _emit(self, order: Mapping[str, object], status: str) -> None:
        await self._events.put(
            UserEvent(
                source="websocket",
                kind="order",
                observed_at=self._clock(),
                payload={
                    "client_order_id": str(order["client_order_id"]),
                    "venue_order_id": str(order["venue_order_id"]),
                    "symbol": str(order["symbol"]),
                    "status": status,
                    "filled_quantity": str(order["filled_quantity"]),
                },
            )
        )

    @staticmethod
    def _broker_order(order: Mapping[str, object]) -> BrokerOrder:
        return BrokerOrder(
            client_order_id=str(order["client_order_id"]),
            venue_order_id=str(order["venue_order_id"]),
            symbol=str(order["symbol"]),
            status=str(order["status"]),
            quantity=Decimal(str(order["quantity"])),
            filled_quantity=Decimal(str(order["filled_quantity"])),
        )

    @property
    def _state_key(self) -> str:
        return f"paper:{self.venue}:broker"

    def _persist(self) -> None:
        document = {
            "orders": self._orders,
            "fills": [
                {
                    "client_order_id": fill.client_order_id,
                    "venue_order_id": fill.venue_order_id,
                    "symbol": fill.symbol,
                    "side": fill.side.value,
                    "quantity": str(fill.quantity),
                    "price": str(fill.price),
                    "fee": str(fill.fee),
                    "maker": fill.maker,
                    "timestamp": fill.timestamp.isoformat(),
                }
                for fill in self._fills
            ],
            "positions": {symbol: str(value) for symbol, value in self._positions.items()},
            "cash": str(self._cash),
        }
        self._state.set(
            self._state_key,
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode(),
        )

    def _load(
        self,
    ) -> tuple[dict[str, dict[str, object]], list[PaperFill], dict[str, Decimal], Decimal]:
        raw = self._state.get(self._state_key)
        if raw is None:
            return {}, [], {}, self._initial_cash
        document = json.loads(raw)
        orders = {str(key): dict(value) for key, value in document["orders"].items()}
        fills = [
            PaperFill(
                item["client_order_id"],
                item["venue_order_id"],
                item["symbol"],
                Side(item["side"]),
                Decimal(item["quantity"]),
                Decimal(item["price"]),
                Decimal(item["fee"]),
                bool(item["maker"]),
                datetime.fromisoformat(item["timestamp"]),
            )
            for item in document["fills"]
        ]
        positions = {
            str(symbol): Decimal(str(value)) for symbol, value in document["positions"].items()
        }
        return orders, fills, positions, Decimal(str(document["cash"]))
