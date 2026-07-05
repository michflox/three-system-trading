"""Kraken Spot REST and WebSocket v2 broker adapter.

Official documentation checked 2026-07-05:
- REST signing/nonce: https://docs.kraken.com/exchange/guides/rest/authentication
- Add order: https://docs.kraken.com/api-reference/trading/add-order
- Cancel: https://docs.kraken.com/api-reference/trading/cancel-order
- Open orders: https://docs.kraken.com/api-reference/account-data/get-open-orders
- Extended balances: https://docs.kraken.com/api-reference/account-data/get-extended-balance
- Key permissions: https://docs.kraken.com/api/docs/rest-api/get-api-key-info
- WS token: https://docs.kraken.com/api-reference/trading/get-websockets-token
- WS v2 executions: https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/executions
- WS v2 balances: https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/balances
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from websockets.asyncio.client import connect

from core.events import OrderAck, OrderStatus, OrderType, Position
from core.risk import Approved
from core.state import StateStore
from crypto.adapters.base import (
    Balance,
    BrokerOrder,
    CapabilityUnsupported,
    CryptoBrokerAdapter,
    ProductSpec,
    UserEvent,
)
from crypto.capabilities import KRAKEN_STARTER_BUDGET, TokenBucket
from crypto.normalize import CredentialPermissionError
from crypto.symbols import SymbolDialect, SymbolRegistry, Venue

REST_BASE_URL = "https://api.kraken.com"
WS_V2_AUTH_URL = "wss://ws-auth.kraken.com/v2"
ORDER_STATE_PREFIX = "kraken:order:"
NONCE_STATE_PREFIX = "kraken:nonce:"


class AdapterNotReadyError(RuntimeError):
    pass


class IdempotencyConflictError(RuntimeError):
    pass


class KrakenApiError(RuntimeError):
    def __init__(self, errors: Sequence[object]) -> None:
        self.errors = tuple(str(error) for error in errors)
        super().__init__("; ".join(self.errors))


class PersistentNonce:
    """Atomic per-key unsigned counter; use one dedicated key per bot process group."""

    def __init__(
        self,
        state: StateStore,
        api_key: str,
        *,
        clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
    ) -> None:
        key_id = hashlib.sha256(api_key.encode()).hexdigest()
        self._state = state
        self._state_key = NONCE_STATE_PREFIX + key_id
        self._clock_ms = clock_ms

    def next(self) -> int:
        with self._state.transaction() as connection:
            row = connection.execute(
                "SELECT value FROM state WHERE key = ?", (self._state_key,)
            ).fetchone()
            previous = 0 if row is None else int(bytes(row[0]).decode("ascii"))
            candidate = max(previous + 1, self._clock_ms(), 1)
            if candidate >= 2**64:
                raise OverflowError("Kraken nonce exhausted unsigned 64-bit range")
            connection.execute(
                "INSERT INTO state(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (self._state_key, str(candidate).encode("ascii")),
            )
        return candidate


class KrakenSigner:
    def __init__(self, api_key: str, api_secret: str) -> None:
        if not api_key or not api_secret:
            raise ValueError("Kraken API key and secret are required")
        try:
            self._secret = base64.b64decode(api_secret, validate=True)
        except ValueError as error:
            raise ValueError("Kraken API secret must be base64") from error
        self.api_key = api_key

    @classmethod
    def from_env(cls) -> KrakenSigner:
        try:
            return cls(os.environ["KRAKEN_API_KEY"], os.environ["KRAKEN_API_SECRET"])
        except KeyError as error:
            raise RuntimeError(f"missing required environment variable {error.args[0]}") from None

    def signature(self, path: str, nonce: int, encoded_body: str) -> str:
        digest = hashlib.sha256(f"{nonce}{encoded_body}".encode()).digest()
        message = path.encode() + digest
        return base64.b64encode(hmac.new(self._secret, message, hashlib.sha512).digest()).decode()


class KrakenAdapter(CryptoBrokerAdapter):
    def __init__(
        self,
        signer: KrakenSigner,
        state: StateStore,
        symbols: SymbolRegistry,
        *,
        client: httpx.AsyncClient | None = None,
        limiter: TokenBucket | None = None,
        nonce: PersistentNonce | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        reconciliation_interval: float = 60.0,
    ) -> None:
        self._signer = signer
        self._state = state
        self._symbols = symbols
        self._client = client or httpx.AsyncClient(base_url=REST_BASE_URL, timeout=30.0)
        self._owns_client = client is None
        self._limiter = limiter or TokenBucket(KRAKEN_STARTER_BUDGET)
        self._nonce = nonce or PersistentNonce(state, signer.api_key)
        self._clock = clock
        self._reconciliation_interval = reconciliation_interval
        self._permissions_verified = False
        # Kraken rejects out-of-order arrivals even when nonce values themselves
        # are increasing, so one dedicated key is also serialized on the wire.
        self._private_lock = asyncio.Lock()

    async def verify_permissions(self) -> None:
        payload = await self._private("/0/private/GetApiKeyInfo")
        result = _result_mapping(payload)
        permissions = result.get("permissions")
        if not isinstance(permissions, list) or not all(
            isinstance(permission, str) for permission in permissions
        ):
            raise CredentialPermissionError("Kraken key permissions are unverifiable")
        actual = set(permissions)
        required = {
            "query-funds",
            "query-open-trades",
            "query-closed-trades",
            "modify-trades",
            "close-trades",
            "create-ws-token",
        }
        missing = required - actual
        if missing:
            raise CredentialPermissionError(
                f"Kraken key lacks required trading permissions: {sorted(missing)}"
            )
        forbidden = actual & {
            "withdraw-funds",
            "add-withdraw-address",
            "update-withdraw-address",
        }
        if forbidden:
            raise CredentialPermissionError(
                f"Kraken key has forbidden withdrawal permissions: {sorted(forbidden)}"
            )
        self._permissions_verified = True

    async def submit_order(self, approved: Approved) -> OrderAck:
        self._require_trade_ready()
        order = approved.order
        payload = self._order_payload(approved)
        state_key = ORDER_STATE_PREFIX + order.client_order_id
        fingerprint = hashlib.sha256(_canonical_json(payload)).hexdigest()
        prior = self._load_submission(state_key)
        if prior is not None:
            if prior.get("fingerprint") != fingerprint:
                raise IdempotencyConflictError(
                    "client_order_id was already persisted with a different order"
                )
            venue_order_id = prior.get("venue_order_id")
            if isinstance(venue_order_id, str) and venue_order_id:
                return self._accepted(order.client_order_id, venue_order_id)
            recovered = await self._find_by_client_id(order.client_order_id)
            if recovered is not None:
                self._persist_result(state_key, fingerprint, payload, recovered)
                return self._accepted(order.client_order_id, recovered)
        else:
            self._state.set(
                state_key,
                _canonical_json(
                    {"fingerprint": fingerprint, "payload": payload, "status": "PREPARED"}
                ),
            )

        try:
            response = await self._private("/0/private/AddOrder", payload)
        except KrakenApiError:
            recovered = await self._find_by_client_id(order.client_order_id)
            if recovered is None:
                raise
            self._persist_result(state_key, fingerprint, payload, recovered)
            return self._accepted(order.client_order_id, recovered)
        result = _result_mapping(response)
        txids = result.get("txid")
        if not isinstance(txids, list) or not txids or not isinstance(txids[0], str):
            raise ValueError("Kraken AddOrder response omitted txid")
        venue_order_id = txids[0]
        self._persist_result(state_key, fingerprint, payload, venue_order_id)
        return self._accepted(order.client_order_id, venue_order_id)

    async def cancel_order(self, venue_order_id: str, approved: Approved) -> bool:
        self._require_trade_ready()
        if not approved.order.is_exit:
            raise ValueError("cancel authorization must be an approved exit/control order")
        payload = await self._private("/0/private/CancelOrder", {"txid": venue_order_id})
        result = _result_mapping(payload)
        return int(str(result.get("count", 0))) > 0

    async def get_open_orders(self) -> Sequence[BrokerOrder]:
        payload = await self._private("/0/private/OpenOrders", {"trades": "false"})
        return tuple(self._parse_orders(payload, "open"))

    async def get_order_history(self) -> Sequence[BrokerOrder]:
        open_payload, closed_payload = await asyncio.gather(
            self._private("/0/private/OpenOrders", {"trades": "false"}),
            self._private("/0/private/ClosedOrders", {"trades": "false"}),
        )
        return tuple(
            [
                *self._parse_orders(open_payload, "open"),
                *self._parse_orders(closed_payload, "closed"),
            ]
        )

    async def get_balances(self) -> Sequence[Balance]:
        payload = await self._private("/0/private/BalanceEx")
        result = _result_mapping(payload)
        balances: list[Balance] = []
        for asset, value in result.items():
            if not isinstance(value, Mapping):
                continue
            total = _decimal(value.get("balance", "0"))
            credit = _decimal(value.get("credit", "0"))
            credit_used = _decimal(value.get("credit_used", "0"))
            held = _decimal(value.get("hold_trade", "0"))
            balances.append(
                Balance(
                    currency=_canonical_asset(str(asset)),
                    available=total + credit - credit_used - held,
                    held=held,
                )
            )
        return tuple(balances)

    async def get_positions(self) -> Sequence[Position]:
        return ()

    async def get_product_specs(self) -> Sequence[ProductSpec]:
        payload = await self._public_get("/0/public/AssetPairs", {"assetVersion": "1"})
        result = _result_mapping(payload)
        specs: list[ProductSpec] = []
        for value in result.values():
            if not isinstance(value, Mapping):
                continue
            canonical = (
                f"{_canonical_asset(str(value['base']))}-{_canonical_asset(str(value['quote']))}"
            )
            specs.append(
                ProductSpec(
                    symbol=canonical,
                    product_type="SPOT",
                    price_increment=_decimal(value["tick_size"]),
                    size_increment=Decimal("1").scaleb(-int(value["lot_decimals"])),
                    minimum_size=_decimal(value["ordermin"]),
                    minimum_notional=_decimal(value["costmin"]),
                    trading_enabled=value.get("status") == "online",
                )
            )
        return tuple(specs)

    async def reconcile(self) -> Mapping[str, object]:
        orders, balances = await asyncio.gather(self.get_open_orders(), self.get_balances())
        return {
            "orders": [asdict(order) for order in orders],
            "balances": [asdict(balance) for balance in balances],
            "positions": [],
        }

    async def stream_user_events(self, product_ids: Sequence[str] = ()) -> AsyncIterator[UserEvent]:
        del product_ids  # Kraken account channels are account-wide.
        token_response = await self._private("/0/private/GetWebSocketsToken")
        token = _result_mapping(token_response).get("token")
        if not isinstance(token, str) or not token:
            raise ValueError("Kraken WebSocket token response omitted token")
        async with connect(WS_V2_AUTH_URL, open_timeout=30) as websocket:
            for channel in ("executions", "balances"):
                params: dict[str, object] = {"channel": channel, "token": token}
                if channel == "executions":
                    params.update({"snap_orders": True, "snap_trades": True})
                await websocket.send(
                    json.dumps({"method": "subscribe", "params": params}, separators=(",", ":"))
                )
            receive_task = asyncio.create_task(websocket.recv())
            reconcile_task = asyncio.create_task(asyncio.sleep(self._reconciliation_interval))
            try:
                while True:
                    done, _ = await asyncio.wait(
                        {receive_task, reconcile_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if receive_task in done:
                        payload = json.loads(receive_task.result())
                        if not isinstance(payload, dict):
                            raise ValueError("Kraken WebSocket message must be an object")
                        yield UserEvent(
                            "websocket",
                            str(payload.get("channel", "system")),
                            self._clock(),
                            payload,
                        )
                        receive_task = asyncio.create_task(websocket.recv())
                    if reconcile_task in done:
                        yield UserEvent(
                            "rest", "reconciliation", self._clock(), await self.reconcile()
                        )
                        reconcile_task = asyncio.create_task(
                            asyncio.sleep(self._reconciliation_interval)
                        )
            finally:
                receive_task.cancel()
                reconcile_task.cancel()

    async def submit_us_perpetual(self, approved: Approved) -> OrderAck:
        # Bitnomial US perpetuals are intentionally excluded. Re-evaluate Q4 2026.
        del approved
        raise CapabilityUnsupported("Kraken adapter supports spot only; Bitnomial is disabled")

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _private(
        self, path: str, body: Mapping[str, object] | None = None
    ) -> dict[str, object]:
        async with self._private_lock:
            await self._limiter.acquire()
            nonce = self._nonce.next()
            fields: dict[str, object] = {"nonce": nonce}
            if body:
                fields.update(body)
            encoded = urllib.parse.urlencode(fields)
            response = await self._client.post(
                path,
                content=encoded,
                headers={
                    "API-Key": self._signer.api_key,
                    "API-Sign": self._signer.signature(path, nonce, encoded),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            response.raise_for_status()
            return _validate_response(response.json())

    async def _public_get(self, path: str, params: Mapping[str, str]) -> dict[str, object]:
        await self._limiter.acquire()
        response = await self._client.get(path, params=params)
        response.raise_for_status()
        return _validate_response(response.json())

    async def _find_by_client_id(self, client_id: str) -> str | None:
        for path, body, section in (
            ("/0/private/OpenOrders", {"trades": "false"}, "open"),
            ("/0/private/ClosedOrders", {"trades": "false"}, "closed"),
        ):
            payload = await self._private(path, body)
            result = _result_mapping(payload)
            orders = result.get(section)
            if not isinstance(orders, Mapping):
                continue
            for txid, details in orders.items():
                if isinstance(details, Mapping) and details.get("cl_ord_id") == client_id:
                    return str(txid)
        return None

    def _parse_orders(self, payload: Mapping[str, object], section: str) -> list[BrokerOrder]:
        result = _result_mapping(payload)
        raw = result.get(section)
        if not isinstance(raw, Mapping):
            raise ValueError(f"Kraken response omitted {section} orders")
        orders: list[BrokerOrder] = []
        for txid, details in raw.items():
            if not isinstance(details, Mapping):
                continue
            description = details.get("descr")
            pair = ""
            if isinstance(description, Mapping):
                pair = str(description.get("pair", ""))
            canonical = self._native_to_canonical(pair)
            orders.append(
                BrokerOrder(
                    client_order_id=str(details.get("cl_ord_id", "")),
                    venue_order_id=str(txid),
                    symbol=canonical,
                    status=str(details.get("status", "unknown")),
                    quantity=_decimal(details.get("vol", "0")),
                    filled_quantity=_decimal(details.get("vol_exec", "0")),
                )
            )
        return orders

    def _native_to_canonical(self, pair: str) -> str:
        try:
            return self._symbols.to_canonical(pair, Venue.KRAKEN)
        except KeyError:
            return pair.replace("/", "-").replace("XBT", "BTC")

    def _order_payload(self, approved: Approved) -> dict[str, object]:
        order = approved.order
        _validate_client_id(order.client_order_id)
        if not order.quantity.is_finite() or order.quantity <= 0:
            raise ValueError("order quantity must be positive and finite")
        payload: dict[str, object] = {
            "ordertype": "market"
            if order.is_exit and order.order_type is OrderType.MARKET
            else "limit",
            "type": order.side.value.lower(),
            "volume": _decimal_text(order.quantity),
            "pair": self._symbols.to_venue(order.symbol, Venue.KRAKEN, SymbolDialect.LEGACY_REST),
            "cl_ord_id": order.client_order_id,
        }
        if payload["ordertype"] == "limit":
            price = order.limit_price or order.expected_price
            if price is None or not price.is_finite() or price <= 0:
                raise ValueError("post-only order requires a positive limit price")
            payload.update({"price": _decimal_text(price), "oflags": "post", "timeinforce": "GTC"})
        return payload

    def _require_trade_ready(self) -> None:
        if not self._permissions_verified:
            raise AdapterNotReadyError("verify_permissions() must pass before order operations")

    def _load_submission(self, key: str) -> dict[str, object] | None:
        value = self._state.get(key)
        if value is None:
            return None
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise ValueError("persisted Kraken order state is corrupt")
        return parsed

    def _persist_result(
        self,
        key: str,
        fingerprint: str,
        payload: Mapping[str, object],
        venue_order_id: str,
    ) -> None:
        self._state.set(
            key,
            _canonical_json(
                {
                    "fingerprint": fingerprint,
                    "payload": payload,
                    "status": "ACCEPTED",
                    "venue_order_id": venue_order_id,
                }
            ),
        )

    def _accepted(self, client_id: str, venue_order_id: str) -> OrderAck:
        return OrderAck(client_id, venue_order_id, OrderStatus.ACCEPTED, self._clock())


def _validate_response(value: Any) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Kraken response must be an object")
    errors = value.get("error")
    if not isinstance(errors, list):
        raise ValueError("Kraken response omitted error array")
    if errors:
        raise KrakenApiError(errors)
    return value


def _result_mapping(payload: Mapping[str, object]) -> Mapping[str, object]:
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise ValueError("Kraken response omitted result")
    return result


def _validate_client_id(value: str) -> None:
    is_uuid = len(value) in {32, 36}
    is_text = 1 <= len(value) <= 18 and value.isascii() and value.isprintable()
    if not (is_uuid or is_text):
        raise ValueError("Kraken cl_ord_id must be a UUID or at most 18 printable ASCII characters")


def _canonical_asset(value: str) -> str:
    asset = value.upper()
    if (asset.startswith("X") and len(asset) == 4) or (asset.startswith("Z") and len(asset) == 4):
        asset = asset[1:]
    return asset.replace("XBT", "BTC")


def _decimal(value: object) -> Decimal:
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        raise ValueError("Kraken numeric value must be finite")
    return parsed


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
