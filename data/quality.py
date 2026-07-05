"""Deterministic market-data validation and daily quality reporting."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from pathlib import Path
from zoneinfo import ZoneInfo

from data.store import ParquetStore


@dataclass(frozen=True, slots=True)
class Gap:
    start: datetime
    end: datetime
    missing_points: int


@dataclass(frozen=True, slots=True)
class DatasetQuality:
    dataset: str
    rows: int
    duplicate_timestamps: int
    gaps: int
    missing_points: int
    stale: bool
    rejected_prices: int
    missing_series: int


@dataclass(frozen=True, slots=True)
class DailyQualityReport:
    generated_at: datetime
    datasets: tuple[DatasetQuality, ...]

    @property
    def zero_gaps(self) -> bool:
        return all(
            item.rows > 0
            and item.gaps == 0
            and item.missing_points == 0
            and item.missing_series == 0
            for item in self.datasets
        )


SPOT_SYMBOLS = ("BTC-USD", "ETH-USD", "SOL-USD")
DEFAULT_EXPECTED_GROUPS = {
    "ohlcv:1h": {(venue, symbol) for venue in ("coinbase", "kraken") for symbol in SPOT_SYMBOLS},
    "ohlcv:1d": {(venue, symbol) for venue in ("coinbase", "kraken") for symbol in SPOT_SYMBOLS},
    "top_of_book": {(venue, symbol) for venue in ("coinbase", "kraken") for symbol in SPOT_SYMBOLS},
    "funding": {("coinbase_cfm", symbol) for symbol in ("BIPZ30", "ETPZ30", "SLPZ30")},
}


def detect_gaps(
    timestamps: Iterable[datetime],
    expected_interval: timedelta,
    *,
    excluded: Callable[[datetime], bool] | None = None,
) -> tuple[Gap, ...]:
    if expected_interval <= timedelta(0):
        raise ValueError("expected interval must be positive")
    ordered = sorted(set(timestamps))
    gaps: list[Gap] = []
    for previous, current in pairwise(ordered):
        cursor = previous + expected_interval
        missing = 0
        first_missing: datetime | None = None
        last_missing: datetime | None = None
        while cursor < current:
            if excluded is None or not excluded(cursor):
                first_missing = first_missing or cursor
                last_missing = cursor
                missing += 1
            cursor += expected_interval
        if missing and first_missing is not None and last_missing is not None:
            gaps.append(Gap(first_missing, last_missing, missing))
    return tuple(gaps)


def is_stale(latest: datetime | None, now: datetime, threshold: timedelta) -> bool:
    if threshold <= timedelta(0):
        raise ValueError("stale threshold must be positive")
    if latest is None:
        return True
    return now - latest >= threshold


def valid_price(
    price: Decimal,
    previous: Decimal | None = None,
    *,
    max_relative_move: Decimal = Decimal("0.50"),
) -> bool:
    try:
        if not price.is_finite() or price <= 0:
            return False
        if previous is None:
            return True
        if not previous.is_finite() or previous <= 0:
            return False
        return abs(price - previous) / previous <= max_relative_move
    except (InvalidOperation, ZeroDivisionError):
        return False


def is_cfm_scheduled_maintenance(timestamp: datetime) -> bool:
    local = timestamp.astimezone(ZoneInfo("America/New_York"))
    return local.weekday() == 4 and local.hour == 17


def build_daily_report(
    store: ParquetStore,
    *,
    now: datetime,
    rejected_prices: Counter[str] | None = None,
    expected_groups: Mapping[str, set[tuple[str, str]]] | None = None,
) -> DailyQualityReport:
    rejected = rejected_prices or Counter()
    expected = expected_groups or DEFAULT_EXPECTED_GROUPS
    specifications = (
        ("ohlcv:1h", "ohlcv", timedelta(hours=1), timedelta(hours=2)),
        ("ohlcv:1d", "ohlcv", timedelta(days=1), timedelta(days=2)),
        ("top_of_book", "top_of_book", timedelta(minutes=1), timedelta(minutes=2)),
        ("funding", "funding", timedelta(hours=1), timedelta(hours=2)),
    )
    results: list[DatasetQuality] = []
    for label, dataset, interval, stale_after in specifications:
        table = store.read(dataset)
        rows = table.to_pylist()
        if dataset == "ohlcv":
            wanted = label.split(":", 1)[1]
            rows = [row for row in rows if row["interval"] == wanted]
        groups: dict[tuple[str, str], list[datetime]] = {}
        for row in rows:
            key = (str(row["venue"]), str(row["symbol"]))
            groups.setdefault(key, []).append(row["timestamp"])
        duplicate_count = 0
        all_gaps: list[Gap] = []
        latest: datetime | None = None
        exclusion = is_cfm_scheduled_maintenance if dataset == "funding" else None
        missing_series = len(expected.get(label, set()) - set(groups))
        stale_group = missing_series > 0
        for timestamps in groups.values():
            counts = Counter(timestamps)
            duplicate_count += sum(count - 1 for count in counts.values())
            all_gaps.extend(detect_gaps(timestamps, interval, excluded=exclusion))
            group_latest = max(timestamps, default=None)
            if group_latest is not None and (latest is None or group_latest > latest):
                latest = group_latest
            if is_stale(group_latest, now, stale_after):
                stale_group = True
        results.append(
            DatasetQuality(
                dataset=label,
                rows=len(rows),
                duplicate_timestamps=duplicate_count,
                gaps=len(all_gaps) + missing_series,
                missing_points=sum(gap.missing_points for gap in all_gaps) + missing_series,
                stale=stale_group or is_stale(latest, now, stale_after),
                rejected_prices=rejected[label],
                missing_series=missing_series,
            )
        )
    return DailyQualityReport(generated_at=now.astimezone(UTC), datasets=tuple(results))


def write_report(report: DailyQualityReport, path: Path | str) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    document = asdict(report)
    encoded = json.dumps(document, default=_json_default, sort_keys=True, indent=2)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(encoded + "\n", encoding="utf-8")
    temporary.replace(destination)


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"cannot encode {type(value).__name__}")
