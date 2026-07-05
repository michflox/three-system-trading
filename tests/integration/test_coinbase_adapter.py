from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)

from core.events import OrderRequest, OrderType, Side
from core.risk import Approved
from core.state import StateStore
from crypto.adapters.coinbase import (
    AdapterNotReadyError,
    CdpJwtAuth,
    CoinbaseAdapter,
)
from crypto.normalize import CredentialPermissionError


def signer() -> CdpJwtAuth:
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption())
    return CdpJwtAuth.from_secret(
        "organizations/test/apiKeys/key",
        pem.decode(),
        clock=lambda: 1_700_000_000.0,
    )


def approved(
    client_id: str = "client-1", *, symbol: str = "BTC-USD", is_exit: bool = False
) -> Approved:
    order = OrderRequest(
        client_order_id=client_id,
        symbol=symbol,
        side=Side.SELL if is_exit else Side.BUY,
        quantity=Decimal("0.001"),
        order_type=OrderType.MARKET if is_exit else OrderType.LIMIT,
        limit_price=None if is_exit else Decimal("50000.00"),
        created_at=datetime(2026, 7, 5, tzinfo=UTC),
        strategy_id="test",
        is_exit=is_exit,
    )
    return Approved(order, Decimal("50"))


def test_rest_and_websocket_jwt_claims_follow_cdp_contract() -> None:
    auth = signer()
    rest = auth.rest_token("GET", "/api/v3/brokerage/accounts")
    claims = jwt.decode(rest, options={"verify_signature": False})
    headers = jwt.get_unverified_header(rest)
    assert claims == {
        "sub": "organizations/test/apiKeys/key",
        "iss": "cdp",
        "nbf": 1_700_000_000,
        "exp": 1_700_000_120,
        "uri": "GET api.coinbase.com/api/v3/brokerage/accounts",
    }
    assert headers["alg"] == "ES256"
    assert headers["kid"] == "organizations/test/apiKeys/key"
    assert headers["nonce"]

    websocket = jwt.decode(auth.websocket_token(), options={"verify_signature": False})
    assert "uri" not in websocket


def test_permission_guard_rejects_transfer_and_blocks_orders(tmp_path) -> None:
    async def exercise() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/key_permissions")
            return httpx.Response(
                200,
                json={"can_view": True, "can_trade": True, "can_transfer": True},
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.coinbase.com"
        ) as client:
            adapter = CoinbaseAdapter(signer(), StateStore(tmp_path / "state.db"), client=client)
            with pytest.raises(CredentialPermissionError):
                await adapter.verify_permissions()
            with pytest.raises(AdapterNotReadyError):
                await adapter.submit_order(approved())

    asyncio.run(exercise())


def test_crash_between_submit_and_ack_does_not_duplicate_order(tmp_path) -> None:
    attempts = 0
    unique_creations = 0
    server_orders: dict[str, str] = {}
    submitted_bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts, unique_creations
        if request.url.path.endswith("/key_permissions"):
            return httpx.Response(
                200,
                json={"can_view": True, "can_trade": True, "can_transfer": False},
            )
        assert request.url.path.endswith("/orders")
        attempts += 1
        body = json.loads(request.content)
        submitted_bodies.append(body)
        client_id = body["client_order_id"]
        if client_id not in server_orders:
            unique_creations += 1
            server_orders[client_id] = "venue-order-1"
        if attempts == 1:
            raise httpx.ReadTimeout("process crashed after Coinbase accepted request")
        return httpx.Response(
            200,
            json={
                "success": True,
                "success_response": {
                    "order_id": server_orders[client_id],
                    "client_order_id": client_id,
                },
            },
        )

    async def exercise() -> None:
        state_path = tmp_path / "state.db"
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(
            transport=transport, base_url="https://api.coinbase.com"
        ) as client:
            first = CoinbaseAdapter(signer(), StateStore(state_path), client=client)
            await first.verify_permissions()
            with pytest.raises(httpx.ReadTimeout):
                await first.submit_order(approved("stable-client-id"))

            restarted = CoinbaseAdapter(signer(), StateStore(state_path), client=client)
            await restarted.verify_permissions()
            ack = await restarted.submit_order(approved("stable-client-id"))
            assert ack.venue_order_id == "venue-order-1"

    asyncio.run(exercise())
    assert attempts == 2
    assert unique_creations == 1
    assert submitted_bodies[0] == submitted_bodies[1]
    assert submitted_bodies[0]["order_configuration"] == {
        "limit_limit_gtc": {
            "base_size": "0.001",
            "limit_price": "50000.00",
            "post_only": True,
        }
    }


def test_cfm_and_urgent_exit_use_documented_order_shapes(tmp_path) -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/key_permissions"):
            return httpx.Response(
                200,
                json={"can_view": True, "can_trade": True, "can_transfer": False},
            )
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(
            200,
            json={
                "success": True,
                "success_response": {
                    "order_id": f"order-{len(bodies)}",
                    "client_order_id": body["client_order_id"],
                },
            },
        )

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.coinbase.com"
        ) as client:
            adapter = CoinbaseAdapter(signer(), StateStore(tmp_path / "state.db"), client=client)
            await adapter.verify_permissions()
            await adapter.submit_order(approved("cfm-entry", symbol="BIT-30DEC30-CDE"))
            await adapter.submit_order(approved("cfm-exit", symbol="BIT-30DEC30-CDE", is_exit=True))

    asyncio.run(exercise())
    assert bodies[0]["product_id"] == "BIT-30DEC30-CDE"
    assert bodies[0]["order_configuration"] == {
        "limit_limit_gtc": {
            "base_size": "0.001",
            "limit_price": "50000.00",
            "post_only": True,
        }
    }
    assert bodies[1]["order_configuration"] == {"market_market_ioc": {"base_size": "0.001"}}


def test_cancel_balances_orders_positions_specs_and_reconciliation(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/key_permissions"):
            payload: object = {
                "can_view": True,
                "can_trade": True,
                "can_transfer": False,
            }
        elif path.endswith("/orders/batch_cancel"):
            payload = {"results": [{"success": True, "order_id": "venue-order"}]}
        elif path.endswith("/orders/historical/batch"):
            payload = {
                "orders": [
                    {
                        "client_order_id": "client",
                        "order_id": "venue-order",
                        "product_id": "BTC-USD",
                        "status": "OPEN",
                        "base_size": "0.01",
                        "filled_size": "0.002",
                    }
                ],
                "has_next": False,
            }
        elif path.endswith("/accounts"):
            payload = {
                "accounts": [
                    {
                        "currency": "USD",
                        "available_balance": {"value": "100.25"},
                        "hold": {"value": "2.50"},
                    }
                ],
                "has_next": False,
            }
        elif path.endswith("/cfm/positions"):
            payload = {
                "positions": [
                    {
                        "product_id": "BIT-30DEC30-CDE",
                        "side": "SHORT",
                        "number_of_contracts": "2",
                        "avg_entry_price": "50000",
                        "unrealized_pnl": "-12.50",
                        "daily_realized_pnl": "3.25",
                    }
                ]
            }
        elif path.endswith("/products"):
            payload = {
                "products": [
                    {
                        "product_id": "BTC-USD",
                        "product_type": "SPOT",
                        "price_increment": "0.01",
                        "base_increment": "0.00000001",
                        "base_min_size": "0.00000001",
                        "quote_min_size": "1",
                        "status": "online",
                        "trading_disabled": False,
                    }
                ],
                "has_next": False,
            }
        else:
            raise AssertionError(path)
        return httpx.Response(200, json=payload)

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.coinbase.com"
        ) as client:
            adapter = CoinbaseAdapter(
                signer(),
                StateStore(tmp_path / "state.db"),
                client=client,
                clock=lambda: datetime(2026, 7, 5, tzinfo=UTC),
            )
            await adapter.verify_permissions()
            assert await adapter.cancel_order("venue-order", approved("cancel", is_exit=True))
            orders = await adapter.get_open_orders()
            balances = await adapter.get_balances()
            positions = await adapter.get_positions()
            specs = await adapter.get_product_specs()
            reconciled = await adapter.reconcile()
            assert orders[0].filled_quantity == Decimal("0.002")
            assert balances[0].available == Decimal("100.25")
            assert positions[0].quantity == Decimal("-2")
            assert specs[0].minimum_notional == Decimal("1")
            assert set(reconciled) == {"orders", "balances", "positions"}

    asyncio.run(exercise())
