import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import httpx

from data.feeds.coinbase import (
    CdePermissionVerificationUnavailable,
    CoinbaseRestClient,
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
