from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from data.quality import build_daily_report, detect_gaps, is_stale, valid_price
from data.store import ParquetStore

D = Decimal
START = datetime(2026, 1, 5, tzinfo=UTC)
END = START + timedelta(hours=48)


def timestamps(step: timedelta) -> list[datetime]:
    values: list[datetime] = []
    cursor = START
    while cursor <= END:
        values.append(cursor)
        cursor += step
    return values


def test_parquet_store_round_trip_and_date_partitions(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    rows = [_ohlcv_row(timestamp, "1d") for timestamp in timestamps(timedelta(days=1))]
    store.append_ohlcv(rows)
    table = store.read("ohlcv")
    assert table.num_rows == 3
    assert len(list((tmp_path / "ohlcv").rglob("*.parquet"))) == 3
    assert table.schema.field("close").type.precision == 38


def test_synthetic_48_hours_reports_zero_gaps_for_every_dataset(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    store.append_ohlcv(_ohlcv_row(value, "1h") for value in timestamps(timedelta(hours=1)))
    store.append_ohlcv(_ohlcv_row(value, "1d") for value in timestamps(timedelta(days=1)))
    store.append_top_of_book(_book_row(value) for value in timestamps(timedelta(minutes=1)))
    store.append_funding(_funding_row(value) for value in timestamps(timedelta(hours=1)))
    expected = {
        "ohlcv:1h": {("coinbase", "BTC-USD")},
        "ohlcv:1d": {("coinbase", "BTC-USD")},
        "top_of_book": {("coinbase", "BTC-USD")},
        "funding": {("coinbase_cfm", "BIPZ30")},
    }
    report = build_daily_report(store, now=END, expected_groups=expected)
    assert report.zero_gaps
    assert all(not dataset.stale for dataset in report.datasets)
    assert {dataset.dataset: dataset.gaps for dataset in report.datasets} == {
        "ohlcv:1h": 0,
        "ohlcv:1d": 0,
        "top_of_book": 0,
        "funding": 0,
    }


def test_gap_stale_and_price_rejection() -> None:
    series = timestamps(timedelta(hours=1))
    del series[10]
    gaps = detect_gaps(series, timedelta(hours=1))
    assert len(gaps) == 1 and gaps[0].missing_points == 1
    assert is_stale(START, START + timedelta(hours=2), timedelta(hours=2))
    assert not valid_price(D("0"))
    assert not valid_price(D("NaN"))
    assert not valid_price(D("151"), D("100"), max_relative_move=D("0.50"))
    assert valid_price(D("150"), D("100"), max_relative_move=D("0.50"))


def test_missing_expected_series_cannot_report_zero_gaps(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    store.append_top_of_book(_book_row(value) for value in timestamps(timedelta(minutes=1)))
    report = build_daily_report(store, now=END)
    assert not report.zero_gaps
    top = next(item for item in report.datasets if item.dataset == "top_of_book")
    assert top.missing_series == 5


def _ohlcv_row(timestamp: datetime, interval: str) -> dict[str, object]:
    return {
        "venue": "coinbase",
        "symbol": "BTC-USD",
        "interval": interval,
        "timestamp": timestamp,
        "open": D("100"),
        "high": D("101"),
        "low": D("99"),
        "close": D("100"),
        "volume": D("10"),
    }


def _book_row(timestamp: datetime) -> dict[str, object]:
    return {
        "venue": "coinbase",
        "symbol": "BTC-USD",
        "timestamp": timestamp,
        "bid_price": D("99.9"),
        "bid_size": D("2"),
        "ask_price": D("100.1"),
        "ask_size": D("3"),
    }


def _funding_row(timestamp: datetime) -> dict[str, object]:
    return {
        "venue": "coinbase_cfm",
        "symbol": "BIPZ30",
        "timestamp": timestamp,
        "funding_rate": D("0.0001"),
        "future_mark_price": D("100"),
        "spot_mark_price": D("99.9"),
        "fair_value_price": D("99.95"),
        "index_price": None,
    }
