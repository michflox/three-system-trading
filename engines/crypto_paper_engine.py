"""DRY_RUN/PAPER crypto orchestration over the recorded market-data store."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from core.events import Bar, OrderRequest, OrderType, Side, SignalAction
from core.execution_mode import ExecutionMode, LiveGateConfig
from core.risk import (
    AssetClass,
    PortfolioSnapshot,
    Quote,
    RiskConfig,
    RiskManager,
    Venue,
    Vetoed,
)
from core.sizing import ewma_volatility
from core.state import StateStore
from crypto.adapters.paper import LiveQuote, PaperAdapter
from crypto.capabilities import VenueCapabilities
from crypto.health import VenueHealth
from crypto.order_manager import MakerFirstPolicy, OrderManager, TouchQuote
from crypto.router import ExecutionRouter, InstrumentType
from data.quality import build_daily_report
from data.store import ParquetStore
from engines.trend_backtest import (
    TrendBacktestConfig,
    TrendBacktestEngine,
    trend_target_quantity,
)
from ops.journal import AppendOnlyJournal
from ops.monitor import AlertTransport, NullAlertTransport, OpsMonitor, TelegramAlertTransport
from strategies.trend_ts import DEFAULT_TREND_PARAMS, trend_ts
from strategies.types import MarketState

LOGGER = logging.getLogger("crypto-paper-engine")
SYMBOLS = ("BTC-USD", "ETH-USD")
ZERO = Decimal("0")
ENGINE_STATE_KEY = "crypto:paper_engine:ledger"
CYCLE_KEY_PREFIX = "crypto:paper_engine:cycle:"


@dataclass(frozen=True, slots=True)
class CryptoPaperConfig:
    mode: ExecutionMode
    data_root: Path
    report_root: Path
    state_path: Path
    journal_path: Path
    risk_config_path: Path
    heartbeat_path: Path
    initial_equity: Decimal
    coinbase_maker_fee: Decimal
    coinbase_taker_fee: Decimal
    kraken_maker_fee: Decimal
    kraken_taker_fee: Decimal
    divergence_tolerance: Decimal
    quote_poll_interval: timedelta = timedelta(seconds=30)
    daily_run_time: time = time(0, 5, tzinfo=UTC)

    def __post_init__(self) -> None:
        if self.mode is ExecutionMode.LIVE:
            raise PermissionError("crypto paper deployment refuses LIVE mode")
        values = (
            self.initial_equity,
            self.coinbase_maker_fee,
            self.coinbase_taker_fee,
            self.kraken_maker_fee,
            self.kraken_taker_fee,
            self.divergence_tolerance,
        )
        if any(not value.is_finite() or value < ZERO for value in values):
            raise ValueError("paper configuration requires non-negative finite Decimals")
        if self.initial_equity <= ZERO or self.quote_poll_interval <= timedelta(0):
            raise ValueError("initial equity and quote poll interval must be positive")

    @classmethod
    def from_environment(cls) -> CryptoPaperConfig:
        mode = ExecutionMode(os.environ.get("TRADING_EXECUTION_MODE", "DRY_RUN"))
        return cls(
            mode=mode,
            data_root=Path(os.environ.get("TRADING_DATA_DIR", "var/data")),
            report_root=Path(os.environ.get("TRADING_REPORT_DIR", "var/reports")),
            state_path=Path(os.environ.get("TRADING_STATE_DB", "var/state/crypto-paper.db")),
            journal_path=Path(
                os.environ.get("TRADING_JOURNAL", "var/journal/crypto-paper.jsonl")
            ),
            risk_config_path=Path(
                os.environ.get("TRADING_RISK_CONFIG", "config/crypto_paper.json")
            ),
            heartbeat_path=Path(
                os.environ.get("TRADING_HEARTBEAT", "var/run/crypto-paper-heartbeat.json")
            ),
            initial_equity=_required_decimal("PAPER_INITIAL_EQUITY", default="100000"),
            coinbase_maker_fee=_required_decimal("COINBASE_MAKER_FEE"),
            coinbase_taker_fee=_required_decimal("COINBASE_TAKER_FEE"),
            kraken_maker_fee=_required_decimal("KRAKEN_MAKER_FEE"),
            kraken_taker_fee=_required_decimal("KRAKEN_TAKER_FEE"),
            divergence_tolerance=_required_decimal(
                "LIVE_BACKTEST_COST_TOLERANCE", default="0.005"
            ),
        )


@dataclass(frozen=True, slots=True)
class PortfolioPoint:
    timestamp: datetime
    equity: Decimal


@dataclass(frozen=True, slots=True)
class EngineLedger:
    day_start_equity: Decimal
    week_start_equity: Decimal
    high_water_mark: Decimal
    last_cycle_date: date | None
    paper_started_at: datetime | None
    history: tuple[PortfolioPoint, ...]


@dataclass(frozen=True, slots=True)
class CycleResult:
    cycle_date: date
    mode: ExecutionMode
    approved: int
    vetoed: int
    submitted: int
    equity: Decimal


class RecordedCryptoFeed:
    """Read deduplicated canonical daily bars and minute BBO snapshots from Parquet."""

    def __init__(self, store: ParquetStore) -> None:
        self._store = store

    def daily_bars(
        self,
        symbols: Sequence[str] = SYMBOLS,
        *,
        venue: str = "coinbase",
    ) -> dict[str, tuple[Bar, ...]]:
        wanted = set(symbols)
        rows = [
            row
            for row in self._store.read("ohlcv").to_pylist()
            if row["venue"] == venue
            and row["interval"] == "1d"
            and row["symbol"] in wanted
        ]
        grouped: dict[str, dict[datetime, Bar]] = {symbol: {} for symbol in symbols}
        for row in rows:
            symbol = str(row["symbol"])
            timestamp = _as_aware(row["timestamp"])
            grouped[symbol][timestamp] = Bar(
                symbol,
                timestamp,
                Decimal(row["open"]),
                Decimal(row["high"]),
                Decimal(row["low"]),
                Decimal(row["close"]),
                Decimal(row["volume"]),
            )
        if any(not grouped[symbol] for symbol in symbols):
            raise RuntimeError("recorded daily feed is missing a required symbol")
        common = set.intersection(*(set(grouped[symbol]) for symbol in symbols))
        if not common:
            raise RuntimeError("recorded daily bars have no aligned timestamps")
        ordered = sorted(common)
        return {
            symbol: tuple(grouped[symbol][timestamp] for timestamp in ordered)
            for symbol in symbols
        }

    def latest_quotes(
        self,
        symbols: Sequence[str] = SYMBOLS,
    ) -> dict[str, dict[str, LiveQuote]]:
        wanted = set(symbols)
        latest: dict[tuple[str, str], Mapping[str, object]] = {}
        for row in self._store.read("top_of_book").to_pylist():
            venue = str(row["venue"])
            symbol = str(row["symbol"])
            if symbol not in wanted or venue not in {"coinbase", "kraken"}:
                continue
            key = (venue, symbol)
            current = latest.get(key)
            timestamp = row["timestamp"]
            if not isinstance(timestamp, datetime):
                raise ValueError("top-of-book timestamp must be a datetime")
            current_timestamp = None if current is None else current["timestamp"]
            if current_timestamp is not None and not isinstance(current_timestamp, datetime):
                raise ValueError("top-of-book timestamp must be a datetime")
            if current_timestamp is None or timestamp > current_timestamp:
                latest[key] = row
        result: dict[str, dict[str, LiveQuote]] = {"coinbase": {}, "kraken": {}}
        for (venue, symbol), row in latest.items():
            bid = Decimal(str(row["bid_price"]))
            ask = Decimal(str(row["ask_price"]))
            timestamp = row["timestamp"]
            if not isinstance(timestamp, datetime):
                raise ValueError("top-of-book timestamp must be a datetime")
            result[venue][symbol] = LiveQuote(
                symbol,
                bid,
                ask,
                (bid + ask) / Decimal("2"),
                _as_aware(timestamp),
            )
        return result


class CryptoPaperEngine:
    def __init__(
        self,
        config: CryptoPaperConfig,
        state: StateStore,
        feed: RecordedCryptoFeed,
        risk: RiskManager,
        router: ExecutionRouter,
        adapters: Mapping[str, PaperAdapter],
        health: Mapping[str, VenueHealth],
        order_manager: OrderManager,
        monitor: OpsMonitor,
        journal: AppendOnlyJournal,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.config = config
        self._state = state
        self._feed = feed
        self._risk = risk
        self._router = router
        self._adapters = dict(adapters)
        self._health = dict(health)
        self._orders = order_manager
        self._monitor = monitor
        self._journal = journal
        self._clock = clock

    async def recover(self) -> None:
        """Reconcile durable paper/order state after an abrupt process death."""

        await self._orders.reconcile_many(tuple(self._adapters.values()))
        await self._record_portfolio(self._clock(), append_history=False)

    async def refresh_quotes(self) -> dict[str, dict[str, LiveQuote]]:
        now = self._clock()
        quotes = await asyncio.to_thread(self._feed.latest_quotes)
        for venue, venue_quotes in quotes.items():
            monitor = self._health[venue]
            monitor.record_rest(True, now)
            for quote in venue_quotes.values():
                monitor.record_ws_heartbeat(quote.timestamp)
                monitor.record_book(quote.timestamp)
                if self.config.mode is ExecutionMode.PAPER:
                    await self._adapters[venue].update_quote(quote)
            snapshot = monitor.check(now, force=True)
            await self._monitor.health_transition(snapshot)
        if self.config.mode is ExecutionMode.PAPER:
            await self._record_portfolio(now, append_history=False)
        return quotes

    async def run_daily_cycle(self, now: datetime | None = None) -> CycleResult:
        observed = _as_aware(now or self._clock()).astimezone(UTC)
        cycle_date = observed.date()
        cycle_key = CYCLE_KEY_PREFIX + cycle_date.isoformat()
        prior = self._state.get(cycle_key)
        if prior is not None and json.loads(prior).get("status") == "COMPLETED":
            ledger = self._load_ledger()
            equity = ledger.history[-1].equity if ledger.history else self.config.initial_equity
            return CycleResult(cycle_date, self.config.mode, 0, 0, 0, equity)

        self._state.set(
            cycle_key,
            json.dumps(
                {"status": "PREPARED", "prepared_at": observed.isoformat()},
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
        )
        bars = await asyncio.to_thread(self._feed.daily_bars)
        quotes = await self.refresh_quotes()
        coinbase_quotes = quotes.get("coinbase", {})
        if any(symbol not in coinbase_quotes for symbol in SYMBOLS):
            raise RuntimeError("Coinbase recorded BBO is missing a required symbol")
        if any(
            observed - coinbase_quotes[symbol].timestamp >= timedelta(minutes=2)
            for symbol in SYMBOLS
        ):
            raise RuntimeError("Coinbase recorded BBO is stale")

        current_positions = await self._positions("coinbase")
        cash = await self._cash("coinbase")
        equity = cash + sum(
            quantity * _mid(coinbase_quotes[symbol])
            for symbol, quantity in current_positions.items()
            if symbol in coinbase_quotes
        )
        ledger = self._roll_ledger(self._load_ledger(), observed, equity)
        approved_count = vetoed_count = submitted_count = 0
        for symbol in SYMBOLS:
            history = bars[symbol]
            current = current_positions.get(symbol, ZERO)
            market_state = MarketState(history[-1], current, history)
            signal = trend_ts(market_state, DEFAULT_TREND_PARAMS)[0]
            score = ZERO
            if signal.action is SignalAction.BUY:
                score = signal.quantity or ZERO
            elif signal.action is SignalAction.SELL:
                score = -(signal.quantity or ZERO)
            sigma = ewma_volatility(tuple(bar.close for bar in history[-33:]), span=32)
            target = trend_target_quantity(
                equity=equity,
                score=score,
                sigma=sigma,
                price=history[-1].close,
                config=TrendBacktestConfig(
                    self.config.initial_equity,
                    self.config.coinbase_maker_fee,
                ),
            )
            if current != ZERO and abs(target - current) / abs(current) < Decimal("0.25"):
                continue
            base_order = self._target_order(
                symbol,
                current,
                target,
                observed,
                coinbase_quotes[symbol],
            )
            if base_order is None:
                continue
            planned = MakerFirstPolicy.plan(
                base_order,
                TouchQuote(coinbase_quotes[symbol].bid, coinbase_quotes[symbol].ask),
                reprices=0,
                urgent=False,
            )
            assert planned is not None
            portfolio = self._portfolio(
                equity,
                ledger,
                current_positions,
                coinbase_quotes,
                observed,
            )
            decision = self._risk.approve(planned, portfolio, Venue("coinbase"))
            if isinstance(decision, Vetoed):
                vetoed_count += 1
                await self._monitor.risk_veto(decision, planned, now=observed)
                continue
            approved_count += 1
            route = self._router.route(
                decision,
                InstrumentType.SPOT,
                current_position=current,
            )
            routed = decision
            if route.fitted_order is not None:
                fitted = self._risk.approve(
                    route.fitted_order,
                    portfolio,
                    Venue("coinbase"),
                )
                if isinstance(fitted, Vetoed):
                    vetoed_count += 1
                    await self._monitor.risk_veto(fitted, route.fitted_order, now=observed)
                    continue
                routed = fitted
                route = self._router.route(
                    routed,
                    InstrumentType.SPOT,
                    current_position=current,
                )
            if route.adapter is None or route.venue is None:
                continue
            if self.config.mode is ExecutionMode.DRY_RUN:
                self._journal.append(
                    {
                        "event": "dry_run_order",
                        "timestamp": observed.isoformat(),
                        "client_order_id": routed.order.client_order_id,
                        "symbol": symbol,
                        "venue": route.venue,
                        "quantity": str(routed.order.quantity),
                    }
                )
                continue
            await self._orders.submit(route.adapter, routed)
            submitted_count += 1

        if self.config.mode is ExecutionMode.PAPER:
            await self._orders.reconcile_many(tuple(self._adapters.values()))
        point = await self._record_portfolio(observed, append_history=True)
        completed = {
            "status": "COMPLETED",
            "completed_at": observed.isoformat(),
            "mode": self.config.mode.value,
            "approved": approved_count,
            "vetoed": vetoed_count,
            "submitted": submitted_count,
            "equity": str(point.equity),
        }
        self._state.set(
            cycle_key,
            json.dumps(completed, sort_keys=True, separators=(",", ":")).encode(),
        )
        self._monitor.heartbeat("crypto-paper-engine", now=observed, status="cycle-complete")
        return CycleResult(
            cycle_date,
            self.config.mode,
            approved_count,
            vetoed_count,
            submitted_count,
            point.equity,
        )

    async def run_forever(self) -> None:
        await self.recover()
        async with asyncio.TaskGroup() as group:
            group.create_task(self._quote_loop())
            group.create_task(self._daily_loop())
            group.create_task(self._heartbeat_loop())

    async def monitor_data_quality(self, now: datetime) -> None:
        report = await asyncio.to_thread(
            build_daily_report,
            ParquetStore(self.config.data_root),
            now=now,
        )
        await self._monitor.data_quality(report)

    async def _quote_loop(self) -> None:
        while True:
            await self.refresh_quotes()
            await asyncio.sleep(self.config.quote_poll_interval.total_seconds())

    async def _daily_loop(self) -> None:
        while True:
            await asyncio.sleep(_seconds_until(self._clock(), self.config.daily_run_time))
            await self.run_daily_cycle()

    async def _heartbeat_loop(self) -> None:
        while True:
            self._monitor.heartbeat("crypto-paper-engine", now=self._clock())
            await asyncio.sleep(30)

    def _target_order(
        self,
        symbol: str,
        current: Decimal,
        target: Decimal,
        now: datetime,
        quote: LiveQuote,
    ) -> OrderRequest | None:
        delta = target - current
        if delta == ZERO:
            return None
        projected_same_side = target == ZERO or (current > ZERO and target > ZERO) or (
            current < ZERO and target < ZERO
        )
        is_exit = current != ZERO and abs(target) < abs(current) and projected_same_side
        side = Side.BUY if delta > ZERO else Side.SELL
        expected = quote.bid if side is Side.BUY else quote.ask
        return OrderRequest(
            client_order_id=f"trend_ts:{symbol}:{now.date().isoformat()}",
            symbol=symbol,
            side=side,
            quantity=abs(delta),
            order_type=OrderType.LIMIT,
            created_at=now,
            limit_price=expected,
            expected_price=expected,
            strategy_id="trend_ts",
            is_exit=is_exit,
        )

    def _portfolio(
        self,
        equity: Decimal,
        ledger: EngineLedger,
        positions: Mapping[str, Decimal],
        quotes: Mapping[str, LiveQuote],
        now: datetime,
    ) -> PortfolioSnapshot:
        allocation = sum(
            (
                abs(quantity * _mid(quotes[symbol]))
                for symbol, quantity in positions.items()
                if symbol in quotes
            ),
            ZERO,
        )
        return PortfolioSnapshot(
            equity,
            ledger.day_start_equity,
            ledger.week_start_equity,
            ledger.high_water_mark,
            dict(positions),
            {"trend_ts": allocation},
            {"coinbase": allocation, "kraken": ZERO},
            {
                symbol: Quote(
                    _mid(quote),
                    Decimal("1"),
                    quote.timestamp,
                    AssetClass.CRYPTO,
                )
                for symbol, quote in quotes.items()
            },
        )

    async def _record_portfolio(
        self,
        now: datetime,
        *,
        append_history: bool,
    ) -> PortfolioPoint:
        quotes = await asyncio.to_thread(self._feed.latest_quotes)
        coinbase_quotes = quotes.get("coinbase", {})
        positions = await self._positions("coinbase")
        missing_quotes = set(positions) - set(coinbase_quotes)
        if missing_quotes:
            raise RuntimeError(
                f"cannot mark paper positions without quotes: {sorted(missing_quotes)}"
            )
        cash = await self._cash("coinbase")
        equity = cash + sum(
            quantity * _mid(coinbase_quotes[symbol])
            for symbol, quantity in positions.items()
            if symbol in coinbase_quotes
        )
        ledger = self._roll_ledger(self._load_ledger(), now, equity)
        point = PortfolioPoint(now.astimezone(UTC), equity)
        history = ledger.history
        if append_history and (
            not history or history[-1].timestamp.date() != point.timestamp.date()
        ):
            history = (*history, point)
        ledger = EngineLedger(
            ledger.day_start_equity,
            ledger.week_start_equity,
            max(ledger.high_water_mark, equity),
            now.date() if append_history else ledger.last_cycle_date,
            ledger.paper_started_at
            or (now if self.config.mode is ExecutionMode.PAPER else None),
            history,
        )
        self._save_ledger(ledger)
        await self._monitor.position_changes("coinbase", positions, now=now)
        return point

    async def _positions(self, venue: str) -> dict[str, Decimal]:
        positions = await self._adapters[venue].get_positions()
        return {position.symbol: position.quantity for position in positions}

    async def _cash(self, venue: str) -> Decimal:
        for balance in await self._adapters[venue].get_balances():
            if balance.currency == "USD":
                return balance.available
        raise RuntimeError(f"paper adapter {venue} has no USD balance")

    def _load_ledger(self) -> EngineLedger:
        raw = self._state.get(ENGINE_STATE_KEY)
        if raw is None:
            return EngineLedger(
                self.config.initial_equity,
                self.config.initial_equity,
                self.config.initial_equity,
                None,
                None,
                (),
            )
        value = json.loads(raw)
        return EngineLedger(
            Decimal(value["day_start_equity"]),
            Decimal(value["week_start_equity"]),
            Decimal(value["high_water_mark"]),
            date.fromisoformat(value["last_cycle_date"])
            if value["last_cycle_date"]
            else None,
            datetime.fromisoformat(value["paper_started_at"])
            if value["paper_started_at"]
            else None,
            tuple(
                PortfolioPoint(datetime.fromisoformat(item["timestamp"]), Decimal(item["equity"]))
                for item in value["history"]
            ),
        )

    def _save_ledger(self, ledger: EngineLedger) -> None:
        value = {
            "day_start_equity": str(ledger.day_start_equity),
            "week_start_equity": str(ledger.week_start_equity),
            "high_water_mark": str(ledger.high_water_mark),
            "last_cycle_date": (
                ledger.last_cycle_date.isoformat() if ledger.last_cycle_date else None
            ),
            "paper_started_at": ledger.paper_started_at.isoformat()
            if ledger.paper_started_at
            else None,
            "history": [
                {"timestamp": point.timestamp.isoformat(), "equity": str(point.equity)}
                for point in ledger.history
            ],
        }
        self._state.set(
            ENGINE_STATE_KEY,
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode(),
        )

    @staticmethod
    def _roll_ledger(ledger: EngineLedger, now: datetime, equity: Decimal) -> EngineLedger:
        if ledger.last_cycle_date is None:
            return EngineLedger(
                equity,
                equity,
                max(ledger.high_water_mark, equity),
                None,
                ledger.paper_started_at,
                ledger.history,
            )
        new_day = now.date() > ledger.last_cycle_date
        last_equity = ledger.history[-1].equity if ledger.history else equity
        day_start = last_equity if new_day else ledger.day_start_equity
        last_iso = ledger.last_cycle_date.isocalendar()
        now_iso = now.date().isocalendar()
        week_start = (
            last_equity
            if new_day and (last_iso.year, last_iso.week) != (now_iso.year, now_iso.week)
            else ledger.week_start_equity
        )
        return EngineLedger(
            day_start,
            week_start,
            max(ledger.high_water_mark, equity),
            ledger.last_cycle_date,
            ledger.paper_started_at,
            ledger.history,
        )


class DailyLiveBacktestDiff:
    def __init__(
        self,
        config: CryptoPaperConfig,
        state: StateStore,
        feed: RecordedCryptoFeed,
        monitor: OpsMonitor,
    ) -> None:
        self._config = config
        self._state = state
        self._feed = feed
        self._monitor = monitor

    async def run(self, now: datetime) -> dict[str, object]:
        raw = self._state.get(ENGINE_STATE_KEY)
        if raw is None:
            report: dict[str, object] = {"status": "INSUFFICIENT_HISTORY", "alert": False}
            return self._write(now, report)
        ledger = json.loads(raw)
        history = ledger["history"]
        if len(history) < 2:
            return self._write(now, {"status": "INSUFFICIENT_HISTORY", "alert": False})
        points = [
            PortfolioPoint(datetime.fromisoformat(item["timestamp"]), Decimal(item["equity"]))
            for item in history
        ]
        if ledger["paper_started_at"] is not None:
            paper_started = datetime.fromisoformat(ledger["paper_started_at"])
            points = [point for point in points if point.timestamp >= paper_started]
            if len(points) < 2:
                return self._write(
                    now,
                    {"status": "INSUFFICIENT_HISTORY", "alert": False},
                )
        bars = await asyncio.to_thread(self._feed.daily_bars)
        start_date = points[0].timestamp.date()
        end_date = points[-1].timestamp.date()
        bars = {
            symbol: tuple(bar for bar in values if bar.timestamp.date() <= end_date)
            for symbol, values in bars.items()
        }
        indices = [
            index
            for index, bar in enumerate(bars[SYMBOLS[0]])
            if bar.timestamp.date() >= start_date
        ]
        if not indices:
            return self._write(now, {"status": "INSUFFICIENT_HISTORY", "alert": False})
        engine = TrendBacktestEngine(
            TrendBacktestConfig(points[0].equity, self._config.coinbase_maker_fee)
        )
        result = engine.run(bars, DEFAULT_TREND_PARAMS, trade_start_index=indices[0])
        live_return = points[-1].equity / points[0].equity - Decimal("1")
        backtest_return = result.ending_equity / points[0].equity - Decimal("1")
        divergence = abs(live_return - backtest_return)
        report = {
            "status": "ALERT" if divergence > self._config.divergence_tolerance else "OK",
            "start": points[0].timestamp.date().isoformat(),
            "end": points[-1].timestamp.date().isoformat(),
            "live_return": str(live_return),
            "backtest_return": str(backtest_return),
            "absolute_divergence": str(divergence),
            "known_cost_model_difference": str(self._config.divergence_tolerance),
            "alert": divergence > self._config.divergence_tolerance,
        }
        written = self._write(now, report)
        await self._monitor.divergence(written, now=now)
        return written

    def _write(self, now: datetime, report: dict[str, object]) -> dict[str, object]:
        document = {"generated_at": now.astimezone(UTC).isoformat(), **report}
        path = self._config.report_root / f"live-backtest-diff-{now.date().isoformat()}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(document, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
        return document


def build_engine(config: CryptoPaperConfig) -> tuple[CryptoPaperEngine, DailyLiveBacktestDiff]:
    state = StateStore(config.state_path)
    journal = AppendOnlyJournal(config.journal_path)
    store = ParquetStore(config.data_root)
    feed = RecordedCryptoFeed(store)
    risk_document = json.loads(config.risk_config_path.read_text(encoding="utf-8"))
    live_gate = LiveGateConfig(
        bool(risk_document.get("live_enabled", False)),
        Path(str(risk_document["arm_file"])),
        str(risk_document["arm_sha256"]),
    )
    risk_config = RiskConfig(
        live_gate,
        {key: Decimal(value) for key, value in risk_document["instrument_position_caps"].items()},
        {key: Decimal(value) for key, value in risk_document["strategy_allocation_caps"].items()},
        {key: Decimal(value) for key, value in risk_document["venue_capital_caps"].items()},
        {AssetClass.CRYPTO: timedelta(seconds=int(risk_document["crypto_stale_seconds"]))},
        config.risk_config_path,
        Decimal(risk_document["max_order_notional_fraction"]),
    )
    risk = RiskManager(config.mode, risk_config, journal)
    adapters = {
        "coinbase": PaperAdapter(
            "coinbase",
            state,
            maker_fee=config.coinbase_maker_fee,
            taker_fee=config.coinbase_taker_fee,
            initial_cash=config.initial_equity,
        ),
        "kraken": PaperAdapter(
            "kraken",
            state,
            maker_fee=config.kraken_maker_fee,
            taker_fee=config.kraken_taker_fee,
            initial_cash=config.initial_equity,
        ),
    }
    health = {venue: VenueHealth(venue) for venue in adapters}
    capabilities = {
        venue: VenueCapabilities(True, False, False, True, True, True, frozenset(SYMBOLS))
        for venue in adapters
    }
    router = ExecutionRouter(adapters, capabilities, health, journal)
    transport: AlertTransport
    if os.environ.get("TRADING_ALERTS_DISABLED") == "1":
        if config.mode is ExecutionMode.PAPER:
            raise RuntimeError("Telegram alerts cannot be disabled in PAPER mode")
        transport = NullAlertTransport()
    else:
        transport = TelegramAlertTransport.from_environment()
    monitor = OpsMonitor(state, journal, transport, heartbeat_path=config.heartbeat_path)
    engine = CryptoPaperEngine(
        config,
        state,
        feed,
        risk,
        router,
        adapters,
        health,
        OrderManager(state),
        monitor,
        journal,
    )
    return engine, DailyLiveBacktestDiff(config, state, feed, monitor)


async def _main(command: str) -> None:
    config = CryptoPaperConfig.from_environment()
    engine, diff = build_engine(config)
    if command == "run":
        await engine.run_forever()
    elif command == "cycle":
        await engine.recover()
        await engine.run_daily_cycle()
    elif command == "recover":
        await engine.recover()
    elif command == "diff":
        await diff.run(datetime.now(UTC))
    elif command == "quality":
        await engine.monitor_data_quality(datetime.now(UTC))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the crypto DRY_RUN/PAPER deployment")
    parser.add_argument(
        "command",
        choices=("run", "cycle", "recover", "diff", "quality"),
        nargs="?",
        default="run",
    )
    arguments = parser.parse_args()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(_main(arguments.command))


def _required_decimal(name: str, *, default: str | None = None) -> Decimal:
    raw = os.environ.get(name, default)
    if raw is None:
        raise RuntimeError(f"missing required environment variable {name}")
    value = Decimal(raw)
    if not value.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return value


def _mid(quote: LiveQuote) -> Decimal:
    return (quote.bid + quote.ask) / Decimal("2")


def _as_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("market timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _seconds_until(now: datetime, target_time: time) -> float:
    observed = _as_aware(now)
    target = datetime.combine(observed.date(), target_time).astimezone(UTC)
    if target <= observed:
        target += timedelta(days=1)
    return (target - observed).total_seconds()


if __name__ == "__main__":
    main()
