"""Atomic, partitioned local Parquet storage for recorded market data."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.dataset as ds  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

DECIMAL_TYPE: Final = pa.decimal128(38, 18)
TIMESTAMP_TYPE: Final = pa.timestamp("us", tz="UTC")

OHLCV_SCHEMA: Final = pa.schema(
    [
        ("venue", pa.string()),
        ("symbol", pa.string()),
        ("interval", pa.string()),
        ("timestamp", TIMESTAMP_TYPE),
        ("open", DECIMAL_TYPE),
        ("high", DECIMAL_TYPE),
        ("low", DECIMAL_TYPE),
        ("close", DECIMAL_TYPE),
        ("volume", DECIMAL_TYPE),
    ]
)

TOP_OF_BOOK_SCHEMA: Final = pa.schema(
    [
        ("venue", pa.string()),
        ("symbol", pa.string()),
        ("timestamp", TIMESTAMP_TYPE),
        ("bid_price", DECIMAL_TYPE),
        ("bid_size", DECIMAL_TYPE),
        ("ask_price", DECIMAL_TYPE),
        ("ask_size", DECIMAL_TYPE),
    ]
)

FUNDING_SCHEMA: Final = pa.schema(
    [
        ("venue", pa.string()),
        ("symbol", pa.string()),
        ("timestamp", TIMESTAMP_TYPE),
        ("funding_rate", DECIMAL_TYPE),
        ("future_mark_price", DECIMAL_TYPE),
        ("spot_mark_price", DECIMAL_TYPE),
        ("fair_value_price", DECIMAL_TYPE),
        ("index_price", DECIMAL_TYPE),
    ]
)

SCHEMAS: Final = {
    "ohlcv": OHLCV_SCHEMA,
    "top_of_book": TOP_OF_BOOK_SCHEMA,
    "funding": FUNDING_SCHEMA,
}


class ParquetStore:
    """Write immutable Parquet fragments and atomically publish each fragment."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._counter = 0

    def append(self, dataset: str, rows: Iterable[Mapping[str, object]]) -> Path | None:
        schema = SCHEMAS.get(dataset)
        if schema is None:
            raise ValueError(f"unknown dataset {dataset!r}")
        normalized = [self._normalize(row, schema) for row in rows]
        if not normalized:
            return None
        groups: dict[Path, list[dict[str, object]]] = {}
        for row in normalized:
            timestamp = row["timestamp"]
            assert isinstance(timestamp, datetime)
            partition = self._partition_path(dataset, row, timestamp)
            groups.setdefault(partition, []).append(row)
        last_destination: Path | None = None
        with self._lock:
            for partition, partition_rows in groups.items():
                partition.mkdir(parents=True, exist_ok=True)
                table = pa.Table.from_pylist(partition_rows, schema=schema)
                self._counter += 1
                stem = f"part-{time.time_ns()}-{os.getpid()}-{self._counter}"
                temporary = partition / f".{stem}.parquet.tmp"
                destination = partition / f"{stem}.parquet"
                pq.write_table(table, temporary, compression="zstd", write_statistics=True)
                os.replace(temporary, destination)
                last_destination = destination
        return last_destination

    def read(self, dataset: str) -> pa.Table:
        schema = SCHEMAS.get(dataset)
        if schema is None:
            raise ValueError(f"unknown dataset {dataset!r}")
        path = self.root / dataset
        if not path.exists() or not any(path.rglob("*.parquet")):
            return pa.Table.from_pylist([], schema=schema)
        return ds.dataset(path, format="parquet").to_table()

    def append_ohlcv(self, rows: Iterable[Mapping[str, object]]) -> Path | None:
        return self.append("ohlcv", rows)

    def append_top_of_book(self, rows: Iterable[Mapping[str, object]]) -> Path | None:
        return self.append("top_of_book", rows)

    def append_funding(self, rows: Iterable[Mapping[str, object]]) -> Path | None:
        return self.append("funding", rows)

    def _partition_path(self, dataset: str, row: Mapping[str, object], timestamp: datetime) -> Path:
        fields = [f"venue={_safe_partition(str(row['venue']))}"]
        if "symbol" in row:
            fields.append(f"symbol={_safe_partition(str(row['symbol']))}")
        if "interval" in row:
            fields.append(f"interval={_safe_partition(str(row['interval']))}")
        fields.append(f"date={timestamp.astimezone(UTC).date().isoformat()}")
        return self.root.joinpath(dataset, *fields)

    @staticmethod
    def _normalize(row: Mapping[str, object], schema: pa.Schema) -> dict[str, object]:
        unknown = set(row) - set(schema.names)
        if unknown:
            raise ValueError(f"unknown fields for parquet schema: {sorted(unknown)}")
        normalized: dict[str, object] = {}
        for field in schema:
            value = row.get(field.name)
            if value is None:
                normalized[field.name] = None
            elif pa.types.is_decimal(field.type):
                if not isinstance(value, Decimal) or not value.is_finite():
                    raise ValueError(f"{field.name} must be a finite Decimal")
                normalized[field.name] = value
            elif pa.types.is_timestamp(field.type):
                if not isinstance(value, datetime) or value.tzinfo is None:
                    raise ValueError(f"{field.name} must be timezone-aware")
                normalized[field.name] = value.astimezone(UTC)
            else:
                normalized[field.name] = value
        return normalized


def _safe_partition(value: str) -> str:
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
    if not value or any(character not in allowed for character in value):
        return value.replace("/", "-").replace("\\", "-").replace("..", "-")
    return value
