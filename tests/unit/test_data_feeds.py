import asyncio
import contextlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from data.feeds.coinbase import (
    CdePermissionVerificationUnavailable,
    CoinbaseRestClient,
    CoinbaseWebSocketClient,
    FundingPermissionError,
    parse_product_funding,
)
from data.feeds.coinbase import parse_spot_candles as parse_coinbase
from data.feeds.kraken import parse_spot_candles as parse_kraken

D = Decimal


def test_coinbase_candle_schema_and_chronological_sort() -> None:
    payload = [
        [3600, "99", "111", "100", "110", "2.5"],
        [0, "90", "101", "95", "100", "1.5"],
    ]
    rows = parse_coinbase(payload, symbol="BTC-USD", interval="1h")
    assert [row["timestamp"] for row in rows] == [
        datetime(1970, 1, 1, tzinfo=UTC),
        datetime(1970, 1, 1, 1, tzinfo=UTC),
    ]
    assert rows[1]["close"] == D("110")
    assert rows[1]["volume"] == D("2.5")


def test_coinbase_cfm_funding_preserves_official_mark_fields() -> None:
    rows = parse_product_funding(
        {
            "price": "62850.25",
            "future_product_details": {
                "funding_rate": "0.000100",
                "funding_time": "2025-08-13T12:00:00Z",
                "index_price": "62840.10",
                "settlement_price": "61000.00",
            },
        },
        product_id="BIP-20DEC30-CDE",
    )
    assert rows == [
        {
            "venue": "coinbase_cfm",
            "symbol": "BIP-20DEC30-CDE",
            "timestamp": datetime(2025, 8, 13, 12, tzinfo=UTC),
            "funding_rate": D("0.000100"),
            "future_mark_price": D("62850.25"),
            "spot_mark_price": D("62840.10"),
            "fair_value_price": D("62840.10"),
            "index_price": D("62840.10"),
        }
    ]


def test_cde_funding_requires_auth() -> None:
    async def exercise() -> None:
        client = CoinbaseRestClient()
        try:
            try:
                await client.fetch_funding("BIP-20DEC30-CDE")
            except CdePermissionVerificationUnavailable as error:
                assert "verify_permissions" in str(error)
            else:
                raise AssertionError(
                    "fetch_funding without prior verify_permissions did not fail closed"
                )
        finally:
            await client.close()

    asyncio.run(exercise())


# ---------------------------------------------------------------------------
# Permission gate tests — mock the HTTP layer; no network required
# ---------------------------------------------------------------------------

class _MockAuth:
    def rest_token(self, method: str, path: str) -> str:
        return "mock-jwt-token"


def _mock_response(payload: object) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=payload)
    return resp


def _client_with_permissions(permissions: dict[str, object]) -> MagicMock:
    mock = MagicMock(spec=httpx.AsyncClient)
    mock.get = AsyncMock(return_value=_mock_response(permissions))
    return mock


def _client_with_permissions_then_funding(
    permissions: dict[str, object],
    funding_payload: list[object],
) -> MagicMock:
    mock = MagicMock(spec=httpx.AsyncClient)
    mock.get = AsyncMock(
        side_effect=[_mock_response(permissions), _mock_response(funding_payload)]
    )
    return mock


def test_funding_permission_gate_refuses_if_can_transfer_is_true() -> None:
    # verify_permissions() must refuse when can_transfer=true
    http = _client_with_permissions(
        {"can_view": True, "can_trade": False, "can_transfer": True, "can_receive": True}
    )
    client = CoinbaseRestClient(client=http, auth=_MockAuth())
    try:
        asyncio.run(client.verify_permissions())
    except FundingPermissionError as error:
        assert "transfer" in str(error).lower()
    else:
        raise AssertionError("expected FundingPermissionError for can_transfer=true")


def test_funding_permission_gate_refuses_if_can_view_is_false() -> None:
    # verify_permissions() must refuse when can_view=false
    http = _client_with_permissions(
        {"can_view": False, "can_trade": False, "can_transfer": False, "can_receive": False}
    )
    client = CoinbaseRestClient(client=http, auth=_MockAuth())
    try:
        asyncio.run(client.verify_permissions())
    except FundingPermissionError as error:
        assert "can_view" in str(error)
    else:
        raise AssertionError("expected FundingPermissionError for can_view=false")


def test_funding_permission_gate_allows_view_only_key() -> None:
    # verify_permissions() passes, then fetch_funding() calls the Advanced Trade product endpoint
    product_payload = {
        "price": "62850.25",
        "future_product_details": {
            "funding_rate": "0.000100",
            "funding_time": "2025-08-13T12:00:00Z",
            "index_price": "62840.10",
            "settlement_price": "61000.00",
        },
    }
    http = _client_with_permissions_then_funding(
        {"can_view": True, "can_trade": False, "can_transfer": False, "can_receive": False},
        product_payload,
    )
    client = CoinbaseRestClient(client=http, auth=_MockAuth())

    async def exercise() -> list[dict[str, object]]:
        await client.verify_permissions()
        return await client.fetch_funding("BIP-20DEC30-CDE")

    rows = asyncio.run(exercise())
    assert len(rows) == 1
    assert rows[0]["funding_rate"] == Decimal("0.000100")
    assert rows[0]["venue"] == "coinbase_cfm"
    # Confirm fetch_funding used api.coinbase.com (Advanced Trade product endpoint)
    second_call_url: str = http.get.call_args_list[1].args[0]
    assert "api.coinbase.com" in second_call_url
    assert "BIP-20DEC30-CDE" in second_call_url


# ---------------------------------------------------------------------------
# CoinbaseWebSocketClient — level2 JWT auth and low-noise message handling
# ---------------------------------------------------------------------------

class _FakeWsSocket:
    def __init__(self, messages: list[dict[str, object]]) -> None:
        self.sent: list[dict[str, object]] = []
        self._messages = list(messages)

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def recv(self) -> str:
        if self._messages:
            return json.dumps(self._messages.pop(0))
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def __aenter__(self) -> "_FakeWsSocket":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _MockWsAuth:
    def websocket_token(self) -> str:
        return "mock-ws-jwt-token"


def _run_record_briefly(
    client: CoinbaseWebSocketClient,
    fake_socket: "_FakeWsSocket",
    monkeypatch: pytest.MonkeyPatch,
    *,
    timeout: float = 0.2,
) -> list[dict[str, object]]:
    monkeypatch.setattr(
        "data.feeds.coinbase.websockets.connect", lambda *args, **kwargs: fake_socket
    )
    stop = asyncio.Event()
    received: list[dict[str, object]] = []

    async def callback(row: dict[str, object]) -> None:
        received.append(row)

    async def exercise() -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(client.record(callback, stop), timeout=timeout)

    asyncio.run(exercise())
    return received


def test_level2_subscribe_includes_jwt_and_applies_l2_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = [
        {"channel": "subscriptions", "events": [{"subscriptions": {"level2": ["BTC-USD"]}}]},
        {"channel": "heartbeats", "events": [{"heartbeat_counter": 1}]},
        {
            "channel": "l2_data",
            "events": [
                {
                    "type": "snapshot",
                    "product_id": "BTC-USD",
                    "updates": [
                        {"side": "bid", "price_level": "100", "new_quantity": "1"},
                        {"side": "offer", "price_level": "101", "new_quantity": "2"},
                    ],
                }
            ],
        },
    ]
    fake_socket = _FakeWsSocket(messages)
    client = CoinbaseWebSocketClient(symbols=("BTC-USD",), auth=_MockWsAuth())
    _run_record_briefly(client, fake_socket, monkeypatch)

    level2_payload = fake_socket.sent[0]
    assert level2_payload["channel"] == "level2"
    assert level2_payload["jwt"] == "mock-ws-jwt-token"
    heartbeats_payload = fake_socket.sent[1]
    assert heartbeats_payload["channel"] == "heartbeats"
    assert "jwt" not in heartbeats_payload
    assert client._l2_data_confirmed is True
    assert client._bids["BTC-USD"][Decimal("100")] == Decimal("1")
    assert client._asks["BTC-USD"][Decimal("101")] == Decimal("2")


def test_level2_subscribe_omits_jwt_when_no_auth_provided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_socket = _FakeWsSocket([])
    client = CoinbaseWebSocketClient(symbols=("BTC-USD",), auth=None)
    _run_record_briefly(client, fake_socket, monkeypatch, timeout=0.1)

    assert "jwt" not in fake_socket.sent[0]


def test_heartbeats_are_not_logged_and_subscription_confirmed_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    messages = [
        {"channel": "subscriptions", "events": [{"subscriptions": {"level2": ["BTC-USD"]}}]},
        {"channel": "heartbeats", "events": [{"heartbeat_counter": 1}]},
        {"channel": "heartbeats", "events": [{"heartbeat_counter": 2}]},
        {"channel": "heartbeats", "events": [{"heartbeat_counter": 3}]},
    ]
    fake_socket = _FakeWsSocket(messages)
    client = CoinbaseWebSocketClient(symbols=("BTC-USD",), auth=None)

    with caplog.at_level("INFO", logger="market-data-recorder"):
        _run_record_briefly(client, fake_socket, monkeypatch)

    heartbeat_logs = [r for r in caplog.records if "heartbeat" in r.getMessage().lower()]
    assert heartbeat_logs == []
    subscription_logs = [
        r for r in caplog.records if "subscription confirmed" in r.getMessage()
    ]
    assert len(subscription_logs) == 1


def _run_listen_briefly(
    client: CoinbaseWebSocketClient,
    fake_socket: "_FakeWsSocket",
    *,
    timeout: float = 0.2,
) -> None:
    stop = asyncio.Event()

    async def exercise() -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(client._listen(fake_socket, stop), timeout=timeout)

    asyncio.run(exercise())


def test_level2_snapshot_populates_book() -> None:
    # Exact envelope shape captured by a live diagnostic probe against
    # wss://advanced-trade-ws.coinbase.com (2026-07-09T02:34 UTC): product_id
    # lives on the event, not on individual updates.
    messages = [
        {
            "channel": "l2_data",
            "events": [
                {
                    "type": "snapshot",
                    "product_id": "ETH-USD",  # untracked -- client only tracks BTC-USD
                    "updates": [
                        {"side": "bid", "price_level": "3000", "new_quantity": "1"},
                    ],
                },
                {
                    "type": "snapshot",
                    "product_id": "BTC-USD",
                    "updates": [
                        {"side": "bid", "price_level": "61840.25", "new_quantity": "0.0108082"},
                        {"side": "offer", "price_level": "61841.00", "new_quantity": "0.02"},
                    ],
                },
            ],
        },
    ]
    fake_socket = _FakeWsSocket(messages)
    client = CoinbaseWebSocketClient(symbols=("BTC-USD",))
    _run_listen_briefly(client, fake_socket)

    # Untracked product_id never gets a book key at all -- and does not
    # prevent the tracked event listed after it from being applied.
    assert "ETH-USD" not in client._bids
    assert "ETH-USD" not in client._asks
    assert client._bids["BTC-USD"][Decimal("61840.25")] == Decimal("0.0108082")
    assert client._asks["BTC-USD"][Decimal("61841.00")] == Decimal("0.02")


def test_level2_update_applies_delta_after_snapshot() -> None:
    messages = [
        {
            "channel": "l2_data",
            "events": [
                {
                    "type": "snapshot",
                    "product_id": "BTC-USD",
                    "updates": [
                        {"side": "bid", "price_level": "100", "new_quantity": "1"},
                        {"side": "offer", "price_level": "101", "new_quantity": "2"},
                    ],
                }
            ],
        },
        {
            "channel": "l2_data",
            "events": [
                {
                    "type": "update",
                    "product_id": "BTC-USD",
                    "updates": [
                        {"side": "bid", "price_level": "100", "new_quantity": "5"},
                        {"side": "bid", "price_level": "99.5", "new_quantity": "3"},
                        {"side": "offer", "price_level": "101", "new_quantity": "0"},
                    ],
                }
            ],
        },
    ]
    fake_socket = _FakeWsSocket(messages)
    client = CoinbaseWebSocketClient(symbols=("BTC-USD",))
    _run_listen_briefly(client, fake_socket)

    assert client._bids["BTC-USD"][Decimal("100")] == Decimal("5")
    assert client._bids["BTC-USD"][Decimal("99.5")] == Decimal("3")
    assert Decimal("101") not in client._asks["BTC-USD"]


def test_sample_produces_top_of_book_row_from_populated_book(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _no_sleep() -> None:
        return None

    monkeypatch.setattr("data.feeds.coinbase._sleep_to_next_minute", _no_sleep)

    client = CoinbaseWebSocketClient(symbols=("BTC-USD",))
    client._bids["BTC-USD"][Decimal("100")] = Decimal("1")
    client._asks["BTC-USD"][Decimal("102")] = Decimal("2")

    stop = asyncio.Event()
    received: list[dict[str, object]] = []

    async def callback(row: dict[str, object]) -> None:
        received.append(row)
        stop.set()

    asyncio.run(client._sample(callback, stop))

    assert len(received) == 1
    row = received[0]
    assert row["venue"] == "coinbase"
    assert row["symbol"] == "BTC-USD"
    assert row["bid_price"] == Decimal("100")
    assert row["bid_size"] == Decimal("1")
    assert row["ask_price"] == Decimal("102")
    assert row["ask_size"] == Decimal("2")
    assert isinstance(row["timestamp"], datetime)


def test_kraken_parser_drops_documented_uncommitted_last_candle() -> None:
    payload = {
        "error": [],
        "result": {
            "BTC/USD": [
                [0, "100", "101", "99", "100.5", "100.2", "2", 3],
                [3600, "100.5", "102", "100", "101", "101", "3", 4],
            ],
            "last": 3600,
        },
    }
    rows = parse_kraken(payload, requested_symbol="BTC/USD", interval="1h")
    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTC-USD"
    assert rows[0]["close"] == D("100.5")
