"""Long-running Coinbase/Kraken recorder and backfill command."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from crypto.adapters.coinbase import CdpJwtAuth
from data.feeds.coinbase import (
    SPOT_SYMBOLS,
    CoinbaseRestClient,
    CoinbaseWebSocketClient,
)
from data.feeds.kraken import KRAKEN_SYMBOLS, KrakenRestClient, KrakenWebSocketClient
from data.quality import build_daily_report, write_report
from data.store import ParquetStore

LOGGER = logging.getLogger("market-data-recorder")


@dataclass(frozen=True, slots=True)
class RecorderConfig:
    data_root: Path
    report_root: Path
    cfm_symbols: tuple[str, ...]
    spot_backfill_start: datetime
    funding_backfill_start: date
    minimum_free_percent: float

    @classmethod
    def from_environment(cls) -> RecorderConfig:
        return cls(
            data_root=Path(os.environ.get("TRADING_DATA_DIR", "var/data")),
            report_root=Path(os.environ.get("TRADING_REPORT_DIR", "var/reports")),
            cfm_symbols=tuple(
                item.strip()
                for item in os.environ.get("COINBASE_CFM_SYMBOLS", "BIPZ30,ETPZ30,SLPZ30").split(
                    ","
                )
                if item.strip()
            ),
            spot_backfill_start=datetime.fromisoformat(
                os.environ.get("SPOT_BACKFILL_START", "2015-01-01T00:00:00+00:00")
            ).astimezone(UTC),
            funding_backfill_start=date.fromisoformat(
                os.environ.get("FUNDING_BACKFILL_START", "2025-07-21")
            ),
            minimum_free_percent=float(os.environ.get("MINIMUM_DISK_FREE_PERCENT", "15")),
        )


class Recorder:
    def __init__(self, config: RecorderConfig) -> None:
        self.config = config
        self.store = ParquetStore(config.data_root)
        auth: CdpJwtAuth | None
        try:
            auth = CdpJwtAuth.from_env()
        except RuntimeError:
            auth = None
        self.coinbase = CoinbaseRestClient(auth=auth)
        self.kraken = KrakenRestClient()
        self.stop = asyncio.Event()
        self.queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=10_000)

    async def close(self) -> None:
        await self.coinbase.close()
        await self.kraken.close()

    async def backfill(self) -> None:
        LOGGER.info("verifying Coinbase key permissions before backfill")
        await self.coinbase.verify_permissions()
        marker = self.config.data_root / "status" / "backfill-complete.json"
        if marker.exists():
            LOGGER.info("backfill marker exists; skipping completed backfill")
            return
        now = datetime.now(UTC)
        for symbol in SPOT_SYMBOLS:
            for seconds in (3600, 86400):
                LOGGER.info("backfilling Coinbase %s %ss", symbol, seconds)
                rows = await self.coinbase.backfill_spot(
                    symbol,
                    granularity_seconds=seconds,
                    start=self.config.spot_backfill_start,
                    end=now,
                )
                self.store.append_ohlcv(rows)
        for symbol in KRAKEN_SYMBOLS:
            for minutes in (60, 1440):
                LOGGER.info(
                    "backfilling Kraken %s %sm (official maximum 720 rows)", symbol, minutes
                )
                self.store.append_ohlcv(
                    await self.kraken.fetch_spot_candles(symbol, interval_minutes=minutes)
                )
        LOGGER.info("backfilling Coinbase CFM hourly funding")
        funding = await self.coinbase.backfill_funding(
            self.config.cfm_symbols,
            start=self.config.funding_backfill_start,
            end=now.date(),
        )
        if not funding:
            raise RuntimeError("Coinbase CFM funding backfill returned no rows")
        self.store.append_funding(funding)
        _atomic_json(
            marker,
            {"completed_at": now.isoformat(), "funding_rows": len(funding)},
        )

    async def run_forever(self) -> None:
        LOGGER.info("verifying Coinbase key permissions before starting recorder")
        await self.coinbase.verify_permissions()
        async with asyncio.TaskGroup() as group:
            group.create_task(self._buffer_writer())
            group.create_task(
                _reconnecting(
                    "coinbase websocket",
                    lambda: CoinbaseWebSocketClient().record(self.queue.put, self.stop),
                    self.stop,
                )
            )
            group.create_task(
                _reconnecting(
                    "kraken websocket",
                    lambda: KrakenWebSocketClient().record(self.queue.put, self.stop),
                    self.stop,
                )
            )
            group.create_task(self._hourly_poll())
            group.create_task(self._disk_monitor())
            group.create_task(self._quality_reporter())

    async def _buffer_writer(self) -> None:
        pending: list[dict[str, object]] = []
        while not self.stop.is_set():
            try:
                row = await asyncio.wait_for(self.queue.get(), timeout=15)
                pending.append(row)
                self.queue.task_done()
            except TimeoutError:
                pass
            if pending and (len(pending) >= 100 or self.queue.empty()):
                batch, pending = pending, []
                await asyncio.to_thread(self.store.append_top_of_book, batch)

    async def _hourly_poll(self) -> None:
        while not self.stop.is_set():
            await _sleep_until_minute(5)
            now = datetime.now(UTC)
            for symbol in SPOT_SYMBOLS:
                hourly = await self.coinbase.fetch_spot_candles(
                    symbol,
                    granularity_seconds=3600,
                    start=now - timedelta(hours=3),
                    end=now,
                )
                self.store.append_ohlcv(
                    _latest_completed(hourly, now.replace(minute=0, second=0, microsecond=0))
                )
                if now.hour == 0:
                    daily = await self.coinbase.fetch_spot_candles(
                        symbol,
                        granularity_seconds=86400,
                        start=now - timedelta(days=3),
                        end=now,
                    )
                    self.store.append_ohlcv(
                        _latest_completed(
                            daily, now.replace(hour=0, minute=0, second=0, microsecond=0)
                        )
                    )
            for symbol in KRAKEN_SYMBOLS:
                hourly = await self.kraken.fetch_spot_candles(symbol, interval_minutes=60)
                self.store.append_ohlcv(
                    _latest_completed(hourly, now.replace(minute=0, second=0, microsecond=0))
                )
                if now.hour == 0:
                    daily = await self.kraken.fetch_spot_candles(symbol, interval_minutes=1440)
                    self.store.append_ohlcv(
                        _latest_completed(
                            daily, now.replace(hour=0, minute=0, second=0, microsecond=0)
                        )
                    )
            funding_rows: list[dict[str, object]] = []
            for symbol in self.config.cfm_symbols:
                funding_rows.extend(await self.coinbase.fetch_funding(symbol, now.date()))
            latest_funding = _latest_per_symbol(funding_rows)
            if not latest_funding:
                raise RuntimeError("Coinbase CFM hourly funding poll returned no rows")
            self.store.append_funding(latest_funding)
            _atomic_json(
                self.config.data_root / "status" / "funding-recorder.json",
                {
                    "last_success": now.isoformat(),
                    "symbols": sorted(str(row["symbol"]) for row in latest_funding),
                    "rows": len(latest_funding),
                },
            )

    async def _disk_monitor(self) -> None:
        while not self.stop.is_set():
            usage = shutil.disk_usage(self.config.data_root)
            free_percent = usage.free / usage.total * 100
            if free_percent < self.config.minimum_free_percent:
                alert = {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "event": "disk_space_low",
                    "free_percent": round(free_percent, 2),
                    "threshold_percent": self.config.minimum_free_percent,
                }
                alert_path = self.config.report_root / "disk-alerts.jsonl"
                alert_path.parent.mkdir(parents=True, exist_ok=True)
                with alert_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(alert, sort_keys=True) + "\n")
                LOGGER.critical("disk space below threshold: %.2f%% free", free_percent)
                raise RuntimeError("disk space alert threshold breached")
            await asyncio.sleep(300)

    async def _quality_reporter(self) -> None:
        while not self.stop.is_set():
            await _sleep_until_utc(hour=0, minute=10)
            now = datetime.now(UTC)
            report = await asyncio.to_thread(build_daily_report, self.store, now=now)
            await asyncio.to_thread(
                write_report,
                report,
                self.config.report_root / f"data-quality-{now.date().isoformat()}.json",
            )
            if not report.zero_gaps:
                LOGGER.error("data quality report contains gaps")


async def _reconnecting(
    name: str,
    operation: Callable[[], Awaitable[None]],
    stop: asyncio.Event,
) -> None:
    delay = 1
    while not stop.is_set():
        try:
            await operation()
            delay = 1
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("%s failed; reconnecting in %ss", name, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


def _latest_completed(
    rows: Sequence[dict[str, object]], current_bucket: datetime
) -> list[dict[str, object]]:
    completed = [row for row in rows if row["timestamp"] < current_bucket]
    return [max(completed, key=lambda row: row["timestamp"])] if completed else []


def _latest_per_symbol(rows: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    latest: dict[str, dict[str, object]] = {}
    for row in rows:
        symbol = str(row["symbol"])
        if symbol not in latest or row["timestamp"] > latest[symbol]["timestamp"]:
            latest[symbol] = row
    return list(latest.values())


async def _sleep_until_minute(minute: int) -> None:
    now = datetime.now(UTC)
    target = now.replace(minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(hours=1)
    await asyncio.sleep((target - now).total_seconds())


async def _sleep_until_utc(*, hour: int, minute: int) -> None:
    now = datetime.now(UTC)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    await asyncio.sleep((target - now).total_seconds())


def _atomic_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


async def _main(command: str) -> None:
    recorder = Recorder(RecorderConfig.from_environment())
    try:
        if command == "backfill":
            await recorder.backfill()
        elif command == "quality":
            now = datetime.now(UTC)
            report = build_daily_report(recorder.store, now=now)
            write_report(report, recorder.config.report_root / f"data-quality-{now.date()}.json")
            if not report.zero_gaps:
                raise SystemExit(2)
        else:
            await recorder.run_forever()
    finally:
        await recorder.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Record Coinbase and Kraken market data")
    parser.add_argument("command", choices=("run", "backfill", "quality"), nargs="?", default="run")
    arguments = parser.parse_args()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(_main(arguments.command))


if __name__ == "__main__":
    main()
