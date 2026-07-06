"""Coinbase public REST and WebSocket market-data client.

Official docs verified 2026-07-05/06:
- https://docs.cdp.coinbase.com/exchange/rest-api/products/get-product-candles
- https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/websocket/websocket-overview
- https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/data-api/get-api-key-permissions
- https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/products/get-best-bid-ask

Note: api.exchange.fairx.net/rest/funding-rate requires Coinbase Exchange HMAC auth
(CB-ACCESS-KEY/SIGN), not CDP JWT.  Funding rate is sourced from the Advanced Trade
product endpoint (future_product_details.funding_rate) instead.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

import httpx
import websockets

from data.quality import valid_price

COINBASE_EXCHANGE_REST = "https://api.exchange.coinbase.com"
COINBASE_ADVANCED_REST = "https://api.coinbase.com"
COINBASE_ADVANCED_WS = "wss://advanced-trade-ws.coinbase.com"
_PERMISSIONS_PATH = "/api/v3/brokerage/key_permissions"
SPOT_SYMBOLS = ("BTC-USD", "ETH-USD", "SOL-USD")
# Nano perpetual futures on the Coinbase Derivatives Exchange (CDE).
# Product IDs expire Dec 2030; update at contract rollover.
CFM_SYMBOLS = ("BIP-20DEC30-CDE", "ETP-20DEC30-CDE", "SLP-20DEC30-CDE")

RowCallback = Callable[[dict[str, object]], Awaitable[None]]


class CdePermissionVerificationUnavailable(RuntimeError):
    """Raised when funding is requested without providing CDP authentication."""


class FundingPermissionError(PermissionError):
    """Raised when the CDP key fails the can_view / can_transfer permission gate."""


class _RestAuth(Protocol):
    """Structural type accepted by CoinbaseRestClient for authenticated requests.

    CdpJwtAuth from crypto.adapters.coinbase satisfies this protocol; the data
    module does not import from crypto to avoid a layering dependency.
    """

    def rest_token(self, method: str, path: str) -> str: ...


class CoinbaseRestClient:
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        auth: _RestAuth | None = None,
    ) -> None:
        self._owned = client is None
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._auth = auth
        self._permissions_verified = False

    async def close(self) -> None:
        if self._owned:
            await self._client.aclose()

    async def fetch_spot_candles(
        self,
        symbol: str,
        *,
        granularity_seconds: int,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, object]]:
        if granularity_seconds not in {3600, 86400}:
            raise ValueError("recorder supports hourly or daily Coinbase candles")
        response = await self._client.get(
            f"{COINBASE_EXCHANGE_REST}/products/{symbol}/candles",
            params={
                "granularity": str(granularity_seconds),
                "start": start.astimezone(UTC).isoformat(),
                "end": end.astimezone(UTC).isoformat(),
            },
        )
        response.raise_for_status()
        return parse_spot_candles(
            response.json(),
            symbol=symbol,
            interval="1h" if granularity_seconds == 3600 else "1d",
        )

    async def backfill_spot(
        self,
        symbol: str,
        *,
        granularity_seconds: int,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        cursor = start
        window = timedelta(seconds=granularity_seconds * 300)
        while cursor < end:
            window_end = min(end, cursor + window)
            rows.extend(
                await self.fetch_spot_candles(
                    symbol,
                    granularity_seconds=granularity_seconds,
                    start=cursor,
                    end=window_end,
                )
            )
            cursor = window_end
            await asyncio.sleep(0.35)
        return _deduplicate(rows)

    async def verify_permissions(self) -> None:
        """Verify the CDP key before any data collection.

        Must be called once at recorder startup. Fails closed:
        - raises CdePermissionVerificationUnavailable if no auth was provided
        - raises FundingPermissionError if can_view is not true
        - raises FundingPermissionError if can_transfer is not false
        - propagates HTTP errors as-is (network failure → fail closed)
        """
        auth = self._auth
        if auth is None:
            raise CdePermissionVerificationUnavailable(
                "CoinbaseRestClient requires auth= to verify key permissions; "
                "set COINBASE_API_KEY and COINBASE_API_SECRET"
            )
        response = await self._client.get(
            f"{COINBASE_ADVANCED_REST}{_PERMISSIONS_PATH}",
            headers={"Authorization": f"Bearer {auth.rest_token('GET', _PERMISSIONS_PATH)}"},
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("can_view") is not True:
            raise FundingPermissionError(
                "Coinbase key must have can_view=true for data collection"
            )
        if payload.get("can_transfer") is not False:
            raise FundingPermissionError(
                "Coinbase key has transfer/withdrawal permission; a data-only key is required"
            )
        self._permissions_verified = True

    async def fetch_funding(self, symbol: str) -> list[dict[str, object]]:
        # Gate: verify_permissions() must succeed before any fetch.
        if not self._permissions_verified:
            raise CdePermissionVerificationUnavailable(
                "call verify_permissions() before fetch_funding()"
            )
        # Funding rate is embedded in the product response (future_product_details).
        # api.exchange.fairx.net/rest/funding-rate requires Exchange HMAC auth and
        # provides no historical data via CDP JWT; use the Advanced Trade product
        # endpoint instead.
        assert self._auth is not None  # invariant: always true after verify_permissions()
        path = f"/api/v3/brokerage/products/{symbol}"
        response = await self._client.get(
            f"{COINBASE_ADVANCED_REST}{path}",
            headers={"Authorization": f"Bearer {self._auth.rest_token('GET', path)}"},
        )
        response.raise_for_status()
        return parse_product_funding(response.json(), product_id=symbol)


class CoinbaseWebSocketClient:
    """Maintain Coinbase level2 books and emit one top-of-book row per minute."""

    def __init__(self, symbols: Sequence[str] = SPOT_SYMBOLS) -> None:
        self.symbols = tuple(symbols)
        self._bids: dict[str, dict[Decimal, Decimal]] = {symbol: {} for symbol in symbols}
        self._asks: dict[str, dict[Decimal, Decimal]] = {symbol: {} for symbol in symbols}
        self._last_prices: dict[str, Decimal] = {}

    async def record(self, callback: RowCallback, stop: asyncio.Event) -> None:
        async with websockets.connect(
            COINBASE_ADVANCED_WS, ping_interval=20, ping_timeout=20, max_queue=4096
        ) as socket:
            await socket.send(
                json.dumps(
                    {"type": "subscribe", "product_ids": list(self.symbols), "channel": "level2"}
                )
            )
            await socket.send(json.dumps({"type": "subscribe", "channel": "heartbeats"}))
            async with asyncio.TaskGroup() as group:
                group.create_task(self._listen(socket, stop))
                group.create_task(self._sample(callback, stop))

    async def _listen(self, socket: Any, stop: asyncio.Event) -> None:
        while not stop.is_set():
            message = json.loads(await socket.recv())
            if message.get("channel") != "l2_data":
                continue
            for event in message.get("events", []):
                for update in event.get("updates", []):
                    symbol = update.get("product_id")
                    if symbol not in self._bids:
                        continue
                    price = Decimal(str(update["price_level"]))
                    quantity = Decimal(str(update["new_quantity"]))
                    side = str(update["side"]).lower()
                    book = self._bids[symbol] if side in {"bid", "buy"} else self._asks[symbol]
                    if quantity == 0:
                        book.pop(price, None)
                    else:
                        book[price] = quantity

    async def _sample(self, callback: RowCallback, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await _sleep_to_next_minute()
            timestamp = datetime.now(UTC).replace(second=0, microsecond=0)
            for symbol in self.symbols:
                if not self._bids[symbol] or not self._asks[symbol]:
                    continue
                bid = max(self._bids[symbol])
                ask = min(self._asks[symbol])
                previous = self._last_prices.get(symbol)
                if bid >= ask or not valid_price((bid + ask) / Decimal("2"), previous):
                    continue
                self._last_prices[symbol] = (bid + ask) / Decimal("2")
                await callback(
                    {
                        "venue": "coinbase",
                        "symbol": symbol,
                        "timestamp": timestamp,
                        "bid_price": bid,
                        "bid_size": self._bids[symbol][bid],
                        "ask_price": ask,
                        "ask_size": self._asks[symbol][ask],
                    }
                )


def parse_spot_candles(payload: object, *, symbol: str, interval: str) -> list[dict[str, object]]:
    if not isinstance(payload, list):
        raise ValueError("Coinbase candles response must be a list")
    rows: list[dict[str, object]] = []
    previous: Decimal | None = None
    for candle in sorted(payload, key=lambda item: item[0] if isinstance(item, list) else 0):
        if not isinstance(candle, list) or len(candle) < 6:
            raise ValueError("malformed Coinbase candle")
        close = Decimal(str(candle[4]))
        if not valid_price(close, previous):
            continue
        previous = close
        rows.append(
            {
                "venue": "coinbase",
                "symbol": symbol,
                "interval": interval,
                "timestamp": datetime.fromtimestamp(int(candle[0]), tz=UTC),
                "low": Decimal(str(candle[1])),
                "high": Decimal(str(candle[2])),
                "open": Decimal(str(candle[3])),
                "close": close,
                "volume": Decimal(str(candle[5])),
            }
        )
    return rows


def parse_product_funding(payload: object, *, product_id: str) -> list[dict[str, object]]:
    """Parse funding rate from GET /api/v3/brokerage/products/{product_id} response."""
    if not isinstance(payload, dict):
        raise ValueError("Coinbase product response must be a dict")
    fpd = payload.get("future_product_details")
    if not isinstance(fpd, dict):
        return []
    funding_rate_str = fpd.get("funding_rate")
    funding_time_str = fpd.get("funding_time")
    if not funding_rate_str or not funding_time_str:
        return []
    mark_price_str = payload.get("price")
    index_price_str = fpd.get("index_price")
    if not mark_price_str or not valid_price(Decimal(str(mark_price_str))):
        return []
    index_price = Decimal(str(index_price_str)) if index_price_str else None
    return [
        {
            "venue": "coinbase_cfm",
            "symbol": product_id,
            "timestamp": _parse_timestamp(str(funding_time_str)),
            "funding_rate": Decimal(str(funding_rate_str)),
            "future_mark_price": Decimal(str(mark_price_str)),
            "spot_mark_price": index_price,
            "fair_value_price": index_price,
            "index_price": index_price,
        }
    ]


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _deduplicate(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    unique = {(row["venue"], row["symbol"], row["timestamp"]): row for row in rows}
    return sorted(unique.values(), key=lambda row: (str(row["symbol"]), row["timestamp"]))


async def _sleep_to_next_minute() -> None:
    now = datetime.now(UTC)
    delay = 60 - now.second - now.microsecond / 1_000_000
    await asyncio.sleep(max(0.01, delay))
