"""Coinbase Advanced Trade adapter for spot and CFM futures products.

Official documentation checked 2026-07-05:
- REST JWT auth: https://docs.cdp.coinbase.com/coinbase-app/authentication-authorization/api-key-authentication
- Orders: https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/orders/create-order
- Cancels: https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/orders/cancel-order
- Orders list: https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/orders/list-orders
- Accounts: https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/accounts/list-accounts
- CFM positions: https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/futures/list-futures-positions
- Permissions: https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/data-api/get-api-key-permissions
- WebSocket: https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/websocket/websocket-overview
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from websockets.asyncio.client import connect

from core.events import OrderAck, OrderStatus, OrderType, Position
from core.risk import Approved
from core.state import StateStore
from crypto.adapters.base import (
    Balance,
    BrokerOrder,
    CryptoBrokerAdapter,
    ProductSpec,
    UserEvent,
)
from crypto.capabilities import COINBASE_PUBLIC_BUDGET, TokenBucket
from crypto.normalize import verify_coinbase_key_permissions

REST_HOST = "api.coinbase.com"
REST_BASE_URL = f"https://{REST_HOST}"
WS_USER_URL = "wss://advanced-trade-ws-user.coinbase.com"
ORDER_STATE_PREFIX = "coinbase:order:"


class AdapterNotReadyError(RuntimeError):
    pass


class IdempotencyConflictError(RuntimeError):
    pass


_PrivateKey = ec.EllipticCurvePrivateKey | ed25519.Ed25519PrivateKey


@dataclass(frozen=True, slots=True)
class CdpJwtAuth:
    """ES256 (ECDSA) or EdDSA (Ed25519) signer; repr excludes secret material."""

    key_name: str
    _private_key: _PrivateKey = field(repr=False)
    clock: Callable[[], float] = time.time

    @property
    def _algorithm(self) -> str:
        return "ES256" if isinstance(self._private_key, ec.EllipticCurvePrivateKey) else "EdDSA"

    @classmethod
    def from_secret(
        cls,
        key_name: str,
        key_secret: str,
        *,
        clock: Callable[[], float] = time.time,
    ) -> CdpJwtAuth:
        if not key_name or not key_secret:
            raise ValueError("Coinbase key name and secret are required")
        secret = key_secret.replace("\\n", "\n").encode()
        private_key = serialization.load_pem_private_key(secret, password=None)
        if not isinstance(private_key, (ec.EllipticCurvePrivateKey, ed25519.Ed25519PrivateKey)):
            raise ValueError("Coinbase requires an EC (ES256) or Ed25519 (EdDSA) private key")
        return cls(key_name=key_name, _private_key=private_key, clock=clock)

    @classmethod
    def from_env(cls) -> CdpJwtAuth:
        try:
            return cls.from_secret(
                os.environ["COINBASE_API_KEY"], os.environ["COINBASE_API_SECRET"]
            )
        except KeyError as error:
            raise RuntimeError(f"missing required environment variable {error.args[0]}") from None

    def rest_token(self, method: str, path: str) -> str:
        now = int(self.clock())
        return jwt.encode(
            {
                "sub": self.key_name,
                "iss": "cdp",
                "nbf": now,
                "exp": now + 120,
                "uri": f"{method.upper()} {REST_HOST}{path}",
            },
            self._private_key,
            algorithm=self._algorithm,
            headers={"kid": self.key_name, "nonce": secrets.token_hex()},
        )

    def websocket_token(self) -> str:
        now = int(self.clock())
        return jwt.encode(
            {"sub": self.key_name, "iss": "cdp", "nbf": now, "exp": now + 120},
            self._private_key,
            algorithm=self._algorithm,
            headers={"kid": self.key_name, "nonce": secrets.token_hex()},
        )


class CoinbaseAdapter(CryptoBrokerAdapter):
    def __init__(
        self,
        auth: CdpJwtAuth,
        state: StateStore,
        *,
        client: httpx.AsyncClient | None = None,
        limiter: TokenBucket | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        reconciliation_interval: float = 60.0,
    ) -> None:
        self._auth = auth
        self._state = state
        self._client = client or httpx.AsyncClient(base_url=REST_BASE_URL, timeout=30.0)
        self._owns_client = client is None
        self._limiter = limiter or TokenBucket(COINBASE_PUBLIC_BUDGET)
        self._clock = clock
        self._reconciliation_interval = reconciliation_interval
        self._permissions_verified = False

    async def verify_permissions(self) -> None:
        payload = await self._request("GET", "/api/v3/brokerage/key_permissions")
        verify_coinbase_key_permissions(payload)
        self._permissions_verified = True

    async def submit_order(self, approved: Approved) -> OrderAck:
        self._require_trade_ready()
        order = approved.order
        payload = self._order_payload(approved)
        state_key = ORDER_STATE_PREFIX + order.client_order_id
        encoded_payload = _canonical_json(payload)
        fingerprint = hashlib.sha256(encoded_payload).hexdigest()
        prior = self._load_submission(state_key)
        if prior is not None:
            if prior["fingerprint"] != fingerprint:
                raise IdempotencyConflictError(
                    "client_order_id was already persisted with a different order"
                )
            venue_order_id = prior.get("venue_order_id")
            if isinstance(venue_order_id, str) and venue_order_id:
                return OrderAck(
                    order.client_order_id,
                    venue_order_id,
                    OrderStatus.ACCEPTED,
                    self._clock(),
                )
        else:
            self._state.set(
                state_key,
                _canonical_json(
                    {"fingerprint": fingerprint, "payload": payload, "status": "PREPARED"}
                ),
            )

        # Coinbase documents client_order_id as idempotent: a duplicate returns the
        # existing order. Retrying a PREPARED record after an ambiguous crash is safe.
        response = await self._request("POST", "/api/v3/brokerage/orders", json_body=payload)
        if response.get("success") is not True:
            error = response.get("error_response")
            reason = (
                "Coinbase rejected order"
                if not isinstance(error, Mapping)
                else str(error.get("message") or error.get("error") or "Coinbase rejected order")
            )
            self._persist_result(state_key, fingerprint, payload, "REJECTED", None)
            return OrderAck(
                order.client_order_id, None, OrderStatus.REJECTED, self._clock(), reason
            )
        success = response.get("success_response")
        if not isinstance(success, Mapping) or not success.get("order_id"):
            raise ValueError("Coinbase success response omitted order_id")
        venue_order_id = str(success["order_id"])
        self._persist_result(state_key, fingerprint, payload, "ACCEPTED", venue_order_id)
        return OrderAck(
            order.client_order_id,
            venue_order_id,
            OrderStatus.ACCEPTED,
            self._clock(),
        )

    async def cancel_order(self, venue_order_id: str, approved: Approved) -> bool:
        self._require_trade_ready()
        if not approved.order.is_exit:
            raise ValueError("cancel authorization must be an approved exit/control order")
        response = await self._request(
            "POST",
            "/api/v3/brokerage/orders/batch_cancel",
            json_body={"order_ids": [venue_order_id]},
        )
        results = response.get("results")
        return bool(
            isinstance(results, list)
            and results
            and isinstance(results[0], Mapping)
            and results[0].get("success") is True
        )

    async def get_open_orders(self) -> Sequence[BrokerOrder]:
        return await self._get_orders(["OPEN", "PENDING"])

    async def get_order_history(self) -> Sequence[BrokerOrder]:
        return await self._get_orders([])

    async def _get_orders(self, statuses: Sequence[str]) -> Sequence[BrokerOrder]:
        orders: list[BrokerOrder] = []
        cursor: str | None = None
        while True:
            params = [("order_status", status) for status in statuses]
            params.append(("limit", "250"))
            if cursor is not None:
                params.append(("cursor", cursor))
            payload = await self._request(
                "GET", "/api/v3/brokerage/orders/historical/batch", params=params
            )
            raw_orders = payload.get("orders")
            if not isinstance(raw_orders, list):
                raise ValueError("Coinbase orders response omitted orders")
            orders.extend(_parse_order(item) for item in raw_orders if isinstance(item, Mapping))
            cursor = _next_cursor(payload)
            if cursor is None:
                return tuple(orders)

    async def get_balances(self) -> Sequence[Balance]:
        accounts: list[Mapping[str, object]] = []
        cursor: str | None = None
        while True:
            params = {"limit": "250"}
            if cursor is not None:
                params["cursor"] = cursor
            payload = await self._request(
                "GET", "/api/v3/brokerage/accounts", params=list(params.items())
            )
            page = payload.get("accounts")
            if not isinstance(page, list):
                raise ValueError("Coinbase accounts response omitted accounts")
            accounts.extend(item for item in page if isinstance(item, Mapping))
            if payload.get("has_next") is not True:
                break
            next_cursor = payload.get("cursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                raise ValueError("Coinbase paginated accounts response omitted cursor")
            cursor = next_cursor
        return tuple(_parse_balance(account) for account in accounts)

    async def get_positions(self) -> Sequence[Position]:
        payload = await self._request("GET", "/api/v3/brokerage/cfm/positions")
        raw_positions = payload.get("positions")
        if not isinstance(raw_positions, list):
            raise ValueError("Coinbase CFM response omitted positions")
        return tuple(
            _parse_position(item, self._clock())
            for item in raw_positions
            if isinstance(item, Mapping)
        )

    async def get_product_specs(self) -> Sequence[ProductSpec]:
        specs: list[ProductSpec] = []
        cursor: str | None = None
        while True:
            params = [("limit", "250")]
            if cursor is not None:
                params.append(("cursor", cursor))
            payload = await self._request("GET", "/api/v3/brokerage/products", params=params)
            products = payload.get("products")
            if not isinstance(products, list):
                raise ValueError("Coinbase products response omitted products")
            specs.extend(_parse_product(item) for item in products if isinstance(item, Mapping))
            cursor = _next_cursor(payload)
            if cursor is None:
                return tuple(specs)

    async def reconcile(self) -> Mapping[str, object]:
        orders, balances, positions = await asyncio.gather(
            self.get_open_orders(), self.get_balances(), self.get_positions()
        )
        return {
            "orders": [asdict(item) for item in orders],
            "balances": [asdict(item) for item in balances],
            "positions": [asdict(item) for item in positions],
        }

    async def stream_user_events(self, product_ids: Sequence[str] = ()) -> AsyncIterator[UserEvent]:
        async with connect(WS_USER_URL, open_timeout=30) as websocket:
            user_message: dict[str, object] = {
                "type": "subscribe",
                "channel": "user",
                "jwt": self._auth.websocket_token(),
            }
            if product_ids:
                user_message["product_ids"] = list(product_ids)
            await websocket.send(json.dumps(user_message, separators=(",", ":")))
            await websocket.send(
                json.dumps(
                    {
                        "type": "subscribe",
                        "channel": "heartbeats",
                        "jwt": self._auth.websocket_token(),
                    },
                    separators=(",", ":"),
                )
            )
            receive_task = asyncio.create_task(websocket.recv())
            reconcile_task = asyncio.create_task(asyncio.sleep(self._reconciliation_interval))
            try:
                while True:
                    done, _ = await asyncio.wait(
                        {receive_task, reconcile_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if receive_task in done:
                        raw = receive_task.result()
                        payload = json.loads(raw)
                        if not isinstance(payload, dict):
                            raise ValueError("Coinbase WebSocket message must be an object")
                        yield UserEvent(
                            "websocket",
                            str(payload.get("channel", "unknown")),
                            self._clock(),
                            payload,
                        )
                        receive_task = asyncio.create_task(websocket.recv())
                    if reconcile_task in done:
                        reconciled = await self.reconcile()
                        yield UserEvent("rest", "reconciliation", self._clock(), reconciled)
                        reconcile_task = asyncio.create_task(
                            asyncio.sleep(self._reconciliation_interval)
                        )
            finally:
                receive_task.cancel()
                reconcile_task.cancel()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: list[tuple[str, str]] | None = None,
        json_body: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        await self._limiter.acquire()
        query = httpx.QueryParams()
        for name, value in params or []:
            query = query.add(name, value)
        response = await self._client.request(
            method,
            path,
            params=query,
            json=json_body,
            headers={"Authorization": f"Bearer {self._auth.rest_token(method, path)}"},
        )
        response.raise_for_status()
        payload: Any = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Coinbase response must be an object")
        return payload

    def _require_trade_ready(self) -> None:
        if not self._permissions_verified:
            raise AdapterNotReadyError("verify_permissions() must pass before order operations")

    @staticmethod
    def _order_payload(approved: Approved) -> dict[str, object]:
        order = approved.order
        if not order.quantity.is_finite() or order.quantity <= 0:
            raise ValueError("order quantity must be positive and finite")
        size = _decimal_text(order.quantity)
        if order.is_exit and order.order_type is OrderType.MARKET:
            configuration: dict[str, object] = {"market_market_ioc": {"base_size": size}}
        else:
            price = order.limit_price or order.expected_price
            if price is None or not price.is_finite() or price <= 0:
                raise ValueError("post-only entry requires a positive limit price")
            configuration = {
                "limit_limit_gtc": {
                    "base_size": size,
                    "limit_price": _decimal_text(price),
                    "post_only": True,
                }
            }
        return {
            "client_order_id": order.client_order_id,
            "product_id": order.symbol,
            "side": order.side.value,
            "order_configuration": configuration,
        }

    def _load_submission(self, key: str) -> dict[str, object] | None:
        value = self._state.get(key)
        if value is None:
            return None
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise ValueError("persisted Coinbase order state is corrupt")
        return parsed

    def _persist_result(
        self,
        key: str,
        fingerprint: str,
        payload: Mapping[str, object],
        status: str,
        venue_order_id: str | None,
    ) -> None:
        self._state.set(
            key,
            _canonical_json(
                {
                    "fingerprint": fingerprint,
                    "payload": payload,
                    "status": status,
                    "venue_order_id": venue_order_id,
                }
            ),
        )


def _parse_balance(account: Mapping[str, object]) -> Balance:
    available = account.get("available_balance")
    hold = account.get("hold")
    if not isinstance(available, Mapping) or not isinstance(hold, Mapping):
        raise ValueError("Coinbase account omitted balance fields")
    return Balance(
        currency=str(account["currency"]),
        available=_decimal(available["value"]),
        held=_decimal(hold["value"]),
    )


def _parse_order(item: Mapping[str, object]) -> BrokerOrder:
    return BrokerOrder(
        client_order_id=str(item["client_order_id"]),
        venue_order_id=str(item["order_id"]),
        symbol=str(item["product_id"]),
        status=str(item["status"]),
        quantity=_decimal(item.get("base_size", "0")),
        filled_quantity=_decimal(item.get("filled_size", "0")),
    )


def _parse_position(item: Mapping[str, object], observed_at: datetime) -> Position:
    contracts = _decimal(item["number_of_contracts"])
    side = str(item.get("side", "")).upper()
    if side in {"SHORT", "SELL"}:
        contracts = -contracts
    return Position(
        symbol=str(item["product_id"]),
        quantity=contracts,
        average_entry_price=_decimal(item["avg_entry_price"]),
        realized_pnl=_decimal(item.get("daily_realized_pnl", "0")),
        unrealized_pnl=_decimal(item.get("unrealized_pnl", "0")),
        updated_at=observed_at,
    )


def _parse_product(item: Mapping[str, object]) -> ProductSpec:
    return ProductSpec(
        symbol=str(item["product_id"]),
        product_type=str(item["product_type"]),
        price_increment=_decimal(item["price_increment"]),
        size_increment=_decimal(item["base_increment"]),
        minimum_size=_decimal(item["base_min_size"]),
        minimum_notional=_decimal(item["quote_min_size"]),
        trading_enabled=item.get("status") == "online" and item.get("trading_disabled") is False,
    )


def _decimal(value: object) -> Decimal:
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        raise ValueError("Coinbase numeric value must be finite")
    return parsed


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _next_cursor(payload: Mapping[str, object]) -> str | None:
    if payload.get("has_next") is not True:
        return None
    cursor = payload.get("cursor")
    if not isinstance(cursor, str) or not cursor:
        raise ValueError("Coinbase paginated response omitted cursor")
    return cursor
