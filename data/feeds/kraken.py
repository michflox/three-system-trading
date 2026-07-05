"""Kraken Spot public REST and WebSocket market-data client.

Official docs verified 2026-07-04:
- https://docs.kraken.com/api-reference/market-data/get-ohlc-data
- https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/ticker
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import websockets

from data.quality import valid_price

KRAKEN_REST = "https://api.kraken.com/0/public"
KRAKEN_WS = "wss://ws.kraken.com/v2"
KRAKEN_SYMBOLS = ("BTC/USD", "ETH/USD", "SOL/USD")
CANONICAL = {"BTC/USD": "BTC-USD", "ETH/USD": "ETH-USD", "SOL/USD": "SOL-USD"}
RowCallback = Callable[[dict[str, object]], Awaitable[None]]


class KrakenRestClient:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._owned = client is None
        self._client = client or httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        if self._owned:
            await self._client.aclose()

    async def fetch_spot_candles(
        self, symbol: str, *, interval_minutes: int
    ) -> list[dict[str, object]]:
        if interval_minutes not in {60, 1440}:
            raise ValueError("recorder supports hourly or daily Kraken candles")
        response = await self._client.get(
            f"{KRAKEN_REST}/OHLC",
            params={"pair": symbol, "interval": str(interval_minutes), "assetVersion": "1"},
        )
        response.raise_for_status()
        return parse_spot_candles(
            response.json(),
            requested_symbol=symbol,
            interval="1h" if interval_minutes == 60 else "1d",
        )


class KrakenWebSocketClient:
    def __init__(self, symbols: Sequence[str] = KRAKEN_SYMBOLS) -> None:
        self.symbols = tuple(symbols)
        self._latest: dict[str, dict[str, object]] = {}
        self._last_prices: dict[str, Decimal] = {}

    async def record(self, callback: RowCallback, stop: asyncio.Event) -> None:
        async with websockets.connect(KRAKEN_WS, ping_interval=20, ping_timeout=20) as socket:
            await socket.send(
                json.dumps(
                    {
                        "method": "subscribe",
                        "params": {
                            "channel": "ticker",
                            "symbol": list(self.symbols),
                            "event_trigger": "bbo",
                            "snapshot": True,
                        },
                    }
                )
            )
            async with asyncio.TaskGroup() as group:
                group.create_task(self._listen(socket, stop))
                group.create_task(self._sample(callback, stop))

    async def _listen(self, socket: Any, stop: asyncio.Event) -> None:
        while not stop.is_set():
            message = json.loads(await socket.recv())
            if message.get("channel") != "ticker":
                continue
            for update in message.get("data", []):
                if update.get("symbol") in self.symbols:
                    self._latest[str(update["symbol"])] = update

    async def _sample(self, callback: RowCallback, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await _sleep_to_next_minute()
            timestamp = datetime.now(UTC).replace(second=0, microsecond=0)
            for native_symbol, update in self._latest.items():
                bid = Decimal(str(update["bid"]))
                ask = Decimal(str(update["ask"]))
                midpoint = (bid + ask) / Decimal("2")
                previous = self._last_prices.get(native_symbol)
                if bid >= ask or not valid_price(midpoint, previous):
                    continue
                self._last_prices[native_symbol] = midpoint
                await callback(
                    {
                        "venue": "kraken",
                        "symbol": CANONICAL[native_symbol],
                        "timestamp": timestamp,
                        "bid_price": bid,
                        "bid_size": Decimal(str(update["bid_qty"])),
                        "ask_price": ask,
                        "ask_size": Decimal(str(update["ask_qty"])),
                    }
                )


def parse_spot_candles(
    payload: object, *, requested_symbol: str, interval: str
) -> list[dict[str, object]]:
    if not isinstance(payload, Mapping) or payload.get("error"):
        raise ValueError(f"Kraken OHLC error: {payload!r}")
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise ValueError("Kraken OHLC response has no result")
    series = next((value for key, value in result.items() if key != "last"), None)
    if not isinstance(series, list):
        raise ValueError("Kraken OHLC response has no candle series")
    rows: list[dict[str, object]] = []
    previous: Decimal | None = None
    # Kraken documents the final row as the current, uncommitted bucket.
    for candle in series[:-1]:
        if not isinstance(candle, list) or len(candle) < 7:
            raise ValueError("malformed Kraken candle")
        close = Decimal(str(candle[4]))
        if not valid_price(close, previous):
            continue
        previous = close
        rows.append(
            {
                "venue": "kraken",
                "symbol": CANONICAL[requested_symbol],
                "interval": interval,
                "timestamp": datetime.fromtimestamp(int(candle[0]), tz=UTC),
                "open": Decimal(str(candle[1])),
                "high": Decimal(str(candle[2])),
                "low": Decimal(str(candle[3])),
                "close": close,
                "volume": Decimal(str(candle[6])),
            }
        )
    return rows


async def _sleep_to_next_minute() -> None:
    now = datetime.now(UTC)
    await asyncio.sleep(max(0.01, 60 - now.second - now.microsecond / 1_000_000))
