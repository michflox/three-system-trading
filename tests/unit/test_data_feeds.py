import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from data.feeds.coinbase import (
    CdePermissionVerificationUnavailable,
    CoinbaseRestClient,
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


def test_cde_funding_refuses_unverifiable_api_key_path() -> None:
    async def exercise() -> None:
        client = CoinbaseRestClient()
        try:
            try:
                await client.fetch_funding("BIPZ30", datetime(2026, 7, 4).date())
            except CdePermissionVerificationUnavailable as error:
                assert "permission-inspection" in str(error)
            else:
                raise AssertionError("funding path did not fail closed")
        finally:
            await client.close()

    asyncio.run(exercise())


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
