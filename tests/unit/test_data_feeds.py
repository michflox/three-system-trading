import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import httpx

from data.feeds.coinbase import (
    CdePermissionVerificationUnavailable,
    CoinbaseRestClient,
    FundingPermissionError,
    parse_funding,
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
    rows = parse_funding(
        [
            {
                "symbol": "BIPZ30",
                "funding_rate": "0.000100",
                "future_mark_price": "62850.25",
                "spot_mark_price": "62840.10",
                "fair_value_price": "62845.00",
                "event_time": "2025-08-13T12:00:00Z",
            }
        ],
        expected_symbol="BIPZ30",
    )
    assert rows == [
        {
            "venue": "coinbase_cfm",
            "symbol": "BIPZ30",
            "timestamp": datetime(2025, 8, 13, 12, tzinfo=UTC),
            "funding_rate": D("0.000100"),
            "future_mark_price": D("62850.25"),
            "spot_mark_price": D("62840.10"),
            "fair_value_price": D("62845.00"),
            "index_price": None,
        }
    ]


def test_cde_funding_requires_auth() -> None:
    async def exercise() -> None:
        client = CoinbaseRestClient()
        try:
            try:
                await client.fetch_funding("BIPZ30", datetime(2026, 7, 4).date())
            except CdePermissionVerificationUnavailable as error:
                assert "auth" in str(error).lower() or "cdp" in str(error).lower()
            else:
                raise AssertionError("funding without auth did not fail closed")
        finally:
            await client.close()

    asyncio.run(exercise())


# ---------------------------------------------------------------------------
# Permission gate tests — mock the HTTP layer; no network required
# ---------------------------------------------------------------------------

class _MockAuth:
    def rest_token(self, method: str, path: str) -> str:
        return "mock-jwt-token"


def _mock_http_client(
    permissions: dict[str, object],
    funding_payload: list[object] | None = None,
) -> MagicMock:
    perm_resp = MagicMock(spec=httpx.Response)
    perm_resp.raise_for_status = MagicMock()
    perm_resp.json = MagicMock(return_value=permissions)

    if funding_payload is not None:
        fund_resp = MagicMock(spec=httpx.Response)
        fund_resp.raise_for_status = MagicMock()
        fund_resp.json = MagicMock(return_value=funding_payload)
        side_effects: list[object] = [perm_resp, fund_resp]
    else:
        side_effects = [perm_resp]

    mock = MagicMock(spec=httpx.AsyncClient)
    mock.get = AsyncMock(side_effect=side_effects)
    return mock


def test_funding_permission_gate_refuses_if_can_transfer_is_true() -> None:
    http = _mock_http_client(
        {"can_view": True, "can_trade": False, "can_transfer": True, "can_receive": True}
    )
    recorder = CoinbaseRestClient(client=http, auth=_MockAuth())
    try:
        asyncio.run(recorder.fetch_funding("BIPZ30", date(2025, 8, 13)))
    except FundingPermissionError as error:
        assert "transfer" in str(error).lower()
    else:
        raise AssertionError("expected FundingPermissionError for can_transfer=true")


def test_funding_permission_gate_refuses_if_can_view_is_false() -> None:
    http = _mock_http_client(
        {"can_view": False, "can_trade": False, "can_transfer": False, "can_receive": False}
    )
    recorder = CoinbaseRestClient(client=http, auth=_MockAuth())
    try:
        asyncio.run(recorder.fetch_funding("BIPZ30", date(2025, 8, 13)))
    except FundingPermissionError as error:
        assert "can_view" in str(error)
    else:
        raise AssertionError("expected FundingPermissionError for can_view=false")


def test_funding_permission_gate_allows_view_only_key() -> None:
    funding_payload = [
        {
            "symbol": "BIPZ30",
            "funding_rate": "0.000100",
            "future_mark_price": "62850.25",
            "spot_mark_price": "62840.10",
            "fair_value_price": "62845.00",
            "event_time": "2025-08-13T12:00:00Z",
        }
    ]
    http = _mock_http_client(
        {"can_view": True, "can_trade": False, "can_transfer": False, "can_receive": False},
        funding_payload,
    )
    recorder = CoinbaseRestClient(client=http, auth=_MockAuth())
    rows = asyncio.run(recorder.fetch_funding("BIPZ30", date(2025, 8, 13)))
    assert len(rows) == 1
    assert rows[0]["funding_rate"] == Decimal("0.000100")
    assert rows[0]["venue"] == "coinbase_cfm"


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
