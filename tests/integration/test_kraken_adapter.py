from __future__ import annotations

import asyncio
import base64
import urllib.parse
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from core.events import OrderRequest, OrderType, Side
from core.risk import Approved
from core.state import StateStore
from crypto.adapters.base import CapabilityUnsupported
from crypto.adapters.kraken import (
    AdapterNotReadyError,
    KrakenAdapter,
    KrakenSigner,
    PersistentNonce,
)
from crypto.normalize import CredentialPermissionError
from crypto.symbols import SymbolRegistry, Venue, VenueSymbol

ALL_PERMISSIONS = [
    "query-funds",
    "query-open-trades",
    "query-closed-trades",
    "modify-trades",
    "close-trades",
    "create-ws-token",
]


def signer() -> KrakenSigner:
    return KrakenSigner("test-api-key", base64.b64encode(b"secret").decode())


def symbols() -> SymbolRegistry:
    return SymbolRegistry(
        [
            VenueSymbol(
                "BTC-USD",
                Venue.KRAKEN,
                "BTC/USD",
                "BTC/USD",
                "XBTUSD",
                "XBT/USD",
            )
        ]
    )


def approved(client_id: str = "client-1", *, is_exit: bool = False) -> Approved:
    order = OrderRequest(
        client_order_id=client_id,
        symbol="BTC-USD",
        side=Side.SELL if is_exit else Side.BUY,
        quantity=Decimal("0.001"),
        order_type=OrderType.MARKET if is_exit else OrderType.LIMIT,
        created_at=datetime(2026, 7, 5, tzinfo=UTC),
        limit_price=None if is_exit else Decimal("50000.00"),
        strategy_id="test",
        is_exit=is_exit,
    )
    return Approved(order, Decimal("50"))


def decoded_form(request: httpx.Request) -> dict[str, str]:
    parsed = urllib.parse.parse_qs(request.content.decode(), strict_parsing=True)
    return {key: values[0] for key, values in parsed.items()}


def response(result: object) -> httpx.Response:
    return httpx.Response(200, json={"error": [], "result": result})


def test_hmac_sha512_matches_kraken_official_vector() -> None:
    secret = (
        "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
    )
    auth = KrakenSigner("key", secret)
    body = "nonce=1616492376594&ordertype=limit&pair=XBTUSD&price=37500&type=buy&volume=1.25"
    assert auth.signature("/0/private/AddOrder", 1616492376594, body) == (
        "4/dpxb3iT4tp/ZCVEwSnEsLxx0bqyhLpdfOpc6fn7OR8+UClSV5n9E6aSS8MPtnRfp32bAb0nmbRn6H8ndwLUQ=="
    )


def test_nonce_is_strictly_increasing_across_restart(tmp_path) -> None:
    path = tmp_path / "state.db"
    first = PersistentNonce(StateStore(path), "dedicated-key", clock_ms=lambda: 1000)
    assert first.next() == 1000
    assert first.next() == 1001

    restarted = PersistentNonce(StateStore(path), "dedicated-key", clock_ms=lambda: 500)
    assert restarted.next() == 1002
    assert restarted.next() == 1003


def test_permission_guard_rejects_withdrawal_and_blocks_orders(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response({"permissions": [*ALL_PERMISSIONS, "withdraw-funds"]})

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.kraken.com"
        ) as client:
            adapter = KrakenAdapter(
                signer(), StateStore(tmp_path / "state.db"), symbols(), client=client
            )
            with pytest.raises(CredentialPermissionError):
                await adapter.verify_permissions()
            with pytest.raises(AdapterNotReadyError):
                await adapter.submit_order(approved())

    asyncio.run(exercise())


def test_crash_restart_recovers_cl_ord_id_without_resubmit(tmp_path) -> None:
    add_attempts = 0
    open_order: dict[str, object] | None = None
    request_bodies: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal add_attempts, open_order
        path = request.url.path
        body = decoded_form(request)
        if path.endswith("/GetApiKeyInfo"):
            return response({"permissions": ALL_PERMISSIONS})
        if path.endswith("/OpenOrders"):
            orders = {} if open_order is None else {"ORDER-1": open_order}
            return response({"open": orders})
        if path.endswith("/ClosedOrders"):
            return response({"closed": {}})
        if path.endswith("/AddOrder"):
            add_attempts += 1
            request_bodies.append(body)
            open_order = {
                "cl_ord_id": body["cl_ord_id"],
                "status": "open",
                "vol": body["volume"],
                "vol_exec": "0",
                "descr": {"pair": "XBTUSD"},
            }
            raise httpx.ReadTimeout("crash after Kraken accepted the order")
        raise AssertionError(path)

    async def exercise() -> None:
        path = tmp_path / "state.db"
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.kraken.com"
        ) as client:
            first = KrakenAdapter(signer(), StateStore(path), symbols(), client=client)
            await first.verify_permissions()
            with pytest.raises(httpx.ReadTimeout):
                await first.submit_order(approved("stable-client"))

            restarted = KrakenAdapter(signer(), StateStore(path), symbols(), client=client)
            await restarted.verify_permissions()
            ack = await restarted.submit_order(approved("stable-client"))
            assert ack.venue_order_id == "ORDER-1"

    asyncio.run(exercise())
    assert add_attempts == 1
    assert request_bodies[0]["pair"] == "XBTUSD"
    assert request_bodies[0]["oflags"] == "post"


def test_order_lifecycle_balances_specs_and_spot_only_stub(tmp_path) -> None:
    submitted: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/AssetPairs"):
            return response(
                {
                    "BTC/USD": {
                        "base": "BTC",
                        "quote": "USD",
                        "tick_size": "0.1",
                        "lot_decimals": 8,
                        "ordermin": "0.00005",
                        "costmin": "0.5",
                        "status": "online",
                    }
                }
            )
        body = decoded_form(request)
        if path.endswith("/GetApiKeyInfo"):
            return response({"permissions": ALL_PERMISSIONS})
        if path.endswith("/AddOrder"):
            submitted.append(body)
            return response({"txid": ["ORDER-2"]})
        if path.endswith("/CancelOrder"):
            return response({"count": 1})
        if path.endswith("/OpenOrders"):
            return response(
                {
                    "open": {
                        "ORDER-2": {
                            "cl_ord_id": "entry-1",
                            "status": "open",
                            "vol": "0.001",
                            "vol_exec": "0.0002",
                            "descr": {"pair": "XBTUSD"},
                        }
                    }
                }
            )
        if path.endswith("/BalanceEx"):
            return response(
                {
                    "ZUSD": {
                        "balance": "100.25",
                        "hold_trade": "2.50",
                        "credit": "0",
                        "credit_used": "0",
                    },
                    "XXBT": {"balance": "1.0", "hold_trade": "0.1"},
                }
            )
        raise AssertionError(path)

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.kraken.com"
        ) as client:
            adapter = KrakenAdapter(
                signer(), StateStore(tmp_path / "state.db"), symbols(), client=client
            )
            await adapter.verify_permissions()
            ack = await adapter.submit_order(approved("entry-1"))
            assert ack.venue_order_id == "ORDER-2"
            assert await adapter.cancel_order("ORDER-2", approved("cancel-1", is_exit=True))
            orders = await adapter.get_open_orders()
            balances = await adapter.get_balances()
            specs = await adapter.get_product_specs()
            assert await adapter.get_positions() == ()
            assert orders[0].symbol == "BTC-USD"
            assert orders[0].filled_quantity == Decimal("0.0002")
            assert balances[0].available == Decimal("97.75")
            assert balances[1].currency == "BTC"
            assert specs[0].minimum_notional == Decimal("0.5")
            with pytest.raises(CapabilityUnsupported):
                await adapter.submit_us_perpetual(approved("perp-1"))

    asyncio.run(exercise())
    assert submitted[0]["ordertype"] == "limit"
    assert submitted[0]["oflags"] == "post"
