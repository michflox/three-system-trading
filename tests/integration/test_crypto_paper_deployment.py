import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.events import Bar
from core.execution_mode import ExecutionMode, LiveGateConfig
from core.risk import AssetClass, RiskConfig, RiskManager
from core.state import StateStore
from crypto.adapters.paper import LiveQuote, PaperAdapter
from crypto.capabilities import VenueCapabilities
from crypto.health import VenueHealth
from crypto.order_manager import LifecycleState, OrderManager
from crypto.router import ExecutionRouter
from engines.crypto_paper_engine import (
    ENGINE_STATE_KEY,
    CryptoPaperConfig,
    CryptoPaperEngine,
    DailyLiveBacktestDiff,
    PortfolioPoint,
)
from ops.journal import AppendOnlyJournal
from ops.monitor import NullAlertTransport, OpsMonitor

NOW = datetime(2026, 7, 5, 0, 5, tzinfo=UTC)
SYMBOLS = ("BTC-USD", "ETH-USD")


class MemoryFeed:
    def __init__(self, now: datetime) -> None:
        start = now - timedelta(days=140)
        self.bars: dict[str, tuple[Bar, ...]] = {}
        self.quotes: dict[str, dict[str, LiveQuote]] = {"coinbase": {}, "kraken": {}}
        for offset, symbol in enumerate(SYMBOLS):
            series: list[Bar] = []
            for index in range(140):
                close = Decimal(100 + offset * 50) + Decimal(index) / Decimal("10")
                timestamp = (start + timedelta(days=index)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                series.append(
                    Bar(
                        symbol,
                        timestamp,
                        close - Decimal("0.1"),
                        close + Decimal("0.2"),
                        close - Decimal("0.2"),
                        close,
                        Decimal("1000"),
                    )
                )
            self.bars[symbol] = tuple(series)
            close = series[-1].close
            for venue in self.quotes:
                self.quotes[venue][symbol] = LiveQuote(
                    symbol,
                    close - Decimal("0.01"),
                    close + Decimal("0.01"),
                    close,
                    now,
                )

    def daily_bars(self) -> dict[str, tuple[Bar, ...]]:
        return self.bars

    def latest_quotes(self) -> dict[str, dict[str, LiveQuote]]:
        return self.quotes


def make_engine(
    tmp_path,
    *,
    mode: ExecutionMode,
    crash_after_accept: bool = False,
) -> tuple[CryptoPaperEngine, StateStore, PaperAdapter, OrderManager, MemoryFeed, OpsMonitor]:
    state = StateStore(tmp_path / "state.db")
    journal = AppendOnlyJournal(tmp_path / "journal.jsonl")
    risk_path = tmp_path / "risk.json"
    risk_path.write_text('{"live_enabled":false}\n', encoding="utf-8")
    config = CryptoPaperConfig(
        mode,
        tmp_path / "data",
        tmp_path / "reports",
        tmp_path / "state.db",
        tmp_path / "journal.jsonl",
        risk_path,
        tmp_path / "heartbeat.json",
        Decimal("100000"),
        Decimal("0.006"),
        Decimal("0.012"),
        Decimal("0.0025"),
        Decimal("0.004"),
        Decimal("10"),
    )
    risk = RiskManager(
        mode,
        RiskConfig(
            LiveGateConfig(False, tmp_path / "arm.live", "0" * 64),
            {symbol: Decimal("1000") for symbol in SYMBOLS},
            {"trend_ts": Decimal("100000")},
            {"coinbase": Decimal("100000"), "kraken": Decimal("100000")},
            {AssetClass.CRYPTO: timedelta(minutes=2)},
            risk_path,
            Decimal("0.25"),
        ),
        journal,
    )
    coinbase = PaperAdapter(
        "coinbase",
        state,
        maker_fee=Decimal("0.006"),
        taker_fee=Decimal("0.012"),
        crash_after_accept_once=crash_after_accept,
        clock=lambda: NOW,
    )
    kraken = PaperAdapter(
        "kraken",
        state,
        maker_fee=Decimal("0.0025"),
        taker_fee=Decimal("0.004"),
        clock=lambda: NOW,
    )
    adapters = {"coinbase": coinbase, "kraken": kraken}
    health = {venue: VenueHealth(venue, clock=lambda: NOW) for venue in adapters}
    capabilities = {
        venue: VenueCapabilities(True, False, False, True, True, True, frozenset(SYMBOLS))
        for venue in adapters
    }
    router = ExecutionRouter(adapters, capabilities, health, journal, clock=lambda: NOW)
    monitor = OpsMonitor(
        state,
        journal,
        NullAlertTransport(),
        heartbeat_path=tmp_path / "heartbeat.json",
    )
    order_manager = OrderManager(state, clock=lambda: NOW)
    feed = MemoryFeed(NOW)
    engine = CryptoPaperEngine(
        config,
        state,
        feed,  # type: ignore[arg-type]
        risk,
        router,
        adapters,
        health,
        order_manager,
        monitor,
        journal,
        clock=lambda: NOW,
    )
    return engine, state, coinbase, order_manager, feed, monitor


def test_dry_run_reaches_risk_and_router_but_never_submits(tmp_path) -> None:
    async def exercise() -> None:
        engine, _, adapter, _, _, _ = make_engine(tmp_path, mode=ExecutionMode.DRY_RUN)
        result = await engine.run_daily_cycle(NOW)
        assert result.approved == 2
        assert result.submitted == 0
        assert not await adapter.get_open_orders()
        journal = (tmp_path / "journal.jsonl").read_text(encoding="utf-8")
        assert journal.count('"event":"dry_run_order"') == 2

    asyncio.run(exercise())


def test_kill_mid_cycle_recovers_order_and_adapter_state(tmp_path) -> None:
    async def exercise() -> None:
        first, state, _, _, _, _ = make_engine(
            tmp_path,
            mode=ExecutionMode.PAPER,
            crash_after_accept=True,
        )
        with pytest.raises(ConnectionError, match="simulated crash"):
            await first.run_daily_cycle(NOW)

        restarted, _, adapter, manager, _, _ = make_engine(
            tmp_path,
            mode=ExecutionMode.PAPER,
        )
        await restarted.recover()
        result = await restarted.run_daily_cycle(NOW)
        assert result.submitted == 2
        broker_orders = await adapter.get_open_orders()
        assert len(broker_orders) == 2
        for broker_order in broker_orders:
            managed = manager.get(broker_order.client_order_id)
            assert managed is not None
            assert managed.venue_order_id == broker_order.venue_order_id
            assert managed.state is LifecycleState.OPEN
        assert state.get("paper:coinbase:broker") is not None

    asyncio.run(exercise())


def test_daily_diff_runs_backtest_and_writes_output(tmp_path) -> None:
    async def exercise() -> None:
        engine, state, _, _, feed, monitor = make_engine(
            tmp_path,
            mode=ExecutionMode.DRY_RUN,
        )
        del engine
        points = (
            PortfolioPoint(NOW - timedelta(days=1), Decimal("100000")),
            PortfolioPoint(NOW, Decimal("100100")),
        )
        ledger = {
            "day_start_equity": "100000",
            "week_start_equity": "100000",
            "high_water_mark": "100100",
            "last_cycle_date": NOW.date().isoformat(),
            "paper_started_at": None,
            "history": [
                {"timestamp": point.timestamp.isoformat(), "equity": str(point.equity)}
                for point in points
            ],
        }
        state.set(ENGINE_STATE_KEY, json.dumps(ledger).encode())
        config = CryptoPaperConfig(
            ExecutionMode.DRY_RUN,
            tmp_path / "data",
            tmp_path / "reports",
            tmp_path / "state.db",
            tmp_path / "journal.jsonl",
            tmp_path / "risk.json",
            tmp_path / "heartbeat.json",
            Decimal("100000"),
            Decimal("0.006"),
            Decimal("0.012"),
            Decimal("0.0025"),
            Decimal("0.004"),
            Decimal("10"),
        )
        report = await DailyLiveBacktestDiff(
            config,
            state,
            feed,  # type: ignore[arg-type]
            monitor,
        ).run(NOW)
        assert report["status"] == "OK"
        assert "backtest_return" in report
        assert (tmp_path / "reports" / "live-backtest-diff-2026-07-05.json").exists()

    asyncio.run(exercise())


def test_live_mode_is_refused_before_dependencies_start(tmp_path) -> None:
    with pytest.raises(PermissionError, match="refuses LIVE"):
        CryptoPaperConfig(
            ExecutionMode.LIVE,
            tmp_path,
            tmp_path,
            tmp_path / "state.db",
            tmp_path / "journal.jsonl",
            tmp_path / "risk.json",
            tmp_path / "heartbeat.json",
            Decimal("100000"),
            Decimal("0.006"),
            Decimal("0.012"),
            Decimal("0.0025"),
            Decimal("0.004"),
            Decimal("0.005"),
        )
