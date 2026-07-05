"""Backfill Coinbase daily BTC/ETH bars for the trend research dataset.

Official endpoint checked 2026-07-05:
https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles
"""

from __future__ import annotations

import argparse
import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from data.feeds.coinbase import CoinbaseRestClient
from data.store import OHLCV_SCHEMA


async def backfill(destination: Path) -> None:
    client = CoinbaseRestClient()
    rows: list[dict[str, object]] = []
    completed_end = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        for symbol, start in (
            ("BTC-USD", datetime(2015, 1, 1, tzinfo=UTC)),
            ("ETH-USD", datetime(2016, 1, 1, tzinfo=UTC)),
        ):
            rows.extend(
                await client.backfill_spot(
                    symbol,
                    granularity_seconds=86400,
                    start=start,
                    end=completed_end,
                )
            )
    finally:
        await client.close()
    rows = [row for row in rows if row["timestamp"] < completed_end]
    if not rows:
        raise RuntimeError("Coinbase daily backfill returned no rows")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".parquet.tmp")
    pq.write_table(
        pa.Table.from_pylist(rows, schema=OHLCV_SCHEMA),
        temporary,
        compression="zstd",
        write_statistics=True,
    )
    os.replace(temporary, destination)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path("var/data/ohlcv/coinbase-trend-daily.parquet"),
    )
    args = parser.parse_args()
    asyncio.run(backfill(args.destination))


if __name__ == "__main__":
    main()
