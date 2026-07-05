from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal

from core.events import OrderRequest, OrderType, Side
from core.risk import Approved
from core.state import StateStore
from crypto.adapters.base import ProductSpec
from crypto.adapters.paper import LiveQuote, PaperAdapter
from crypto.capabilities import VenueCapabilities
from crypto.health import HealthState, VenueHealth
from crypto.order_manager import LifecycleState, MakerFirstPolicy, OrderManager, TouchQuote
from crypto.router import ExecutionRouter, InstrumentType
from ops.journal import AppendOnlyJournal

NOW = datetime(2026, 7, 5, tzinfo=UTC)


def approved(
    client_id: str,
    side: Side,
    quantity: str,
    *,
    order_type: OrderType,
    price: str | None = None,
    is_exit: bool = False,
) -> Approved:
    order = OrderRequest(
        client_order_id=client_id,
        symbol="BTC-USD",
        side=side,
        quantity=Decimal(quantity),
        order_type=order_type,
        created_at=NOW,
        limit_price=None if price is None else Decimal(price),
        expected_price=Decimal(price or "100"),
        strategy_id="chaos",
        is_exit=is_exit,
    )
    return Approved(order, order.quantity * Decimal(price or "100"))


def spec() -> ProductSpec:
    return ProductSpec(
        "BTC-USD",
        "SPOT",
        Decimal("0.01"),
        Decimal("0.0001"),
        Decimal("0.0001"),
        Decimal("1"),
        True,
    )


def capability() -> VenueCapabilities:
    return VenueCapabilities(
        spot=True,
        perpetual_futures=False,
        margin=False,
        market_orders=True,
        precision_metadata=True,
        min_notional_metadata=True,
        active_symbols=frozenset({"BTC-USD"}),
    )


def paper(venue: str, state: StateStore, *, crash: bool = False) -> PaperAdapter:
    adapter = PaperAdapter(
        venue,
        state,
        maker_fee=Decimal("0.001"),
        taker_fee=Decimal("0.002"),
        product_specs=[spec()],
        clock=lambda: NOW,
        crash_after_accept_once=crash,
    )
    adapter.set_quote(LiveQuote("BTC-USD", Decimal("99"), Decimal("101"), Decimal("100"), NOW))
    return adapter


def test_kill_ws_reconnect_reconciliation_catches_missed_fill(tmp_path) -> None:
    async def exercise() -> None:
        store = StateStore(tmp_path / "state.db")
        adapter = paper("coinbase", store)
        manager = OrderManager(store, clock=lambda: NOW)
        order = approved("ws-chaos", Side.BUY, "1", order_type=OrderType.LIMIT, price="99")
        await manager.submit(adapter, order)
        task = asyncio.create_task(manager.monitor(adapter))
        await asyncio.sleep(0)
        await adapter.disconnect_stream()
        await adapter.update_quote(
            LiveQuote("BTC-USD", Decimal("98"), Decimal("100"), Decimal("98.99"), NOW)
        )
        for _ in range(20):
            managed = manager.get("ws-chaos")
            if managed is not None and managed.state is LifecycleState.FILLED:
                break
            await asyncio.sleep(0)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        assert manager.get("ws-chaos").state is LifecycleState.FILLED  # type: ignore[union-attr]

    asyncio.run(exercise())


def test_failed_primary_reduces_quarantines_then_migrates_only_after_closed(tmp_path) -> None:
    async def exercise() -> None:
        primary = paper("coinbase", StateStore(tmp_path / "primary.db"))
        backup = paper("kraken", StateStore(tmp_path / "backup.db"))
        await primary.submit_order(approved("seed", Side.BUY, "1", order_type=OrderType.MARKET))
        coinbase_health = VenueHealth("coinbase", clock=lambda: NOW)
        kraken_health = VenueHealth("kraken", clock=lambda: NOW)
        coinbase_health.force_failed("chaos")
        router = ExecutionRouter(
            {"coinbase": primary, "kraken": backup},
            {"coinbase": capability(), "kraken": capability()},
            {"coinbase": coinbase_health, "kraken": kraken_health},
            AppendOnlyJournal(tmp_path / "router.jsonl"),
            clock=lambda: NOW,
        )
        flatten = approved(
            "flatten", Side.SELL, "1", order_type=OrderType.LIMIT, price="101", is_exit=True
        )
        migrate = approved("migrate", Side.BUY, "1", order_type=OrderType.LIMIT, price="99")
        await router.failover("coinbase", [flatten], [(InstrumentType.SPOT, migrate)])
        assert coinbase_health.state is HealthState.QUARANTINED
        assert not await router.reconcile_and_migrate("coinbase")
        assert await backup.get_open_orders() == ()

        await primary.update_quote(
            LiveQuote("BTC-USD", Decimal("100"), Decimal("102"), Decimal("101.01"), NOW)
        )
        assert await router.reconcile_and_migrate("coinbase")
        assert len(await backup.get_open_orders()) == 1
        assert router.dirty_positions == {}

    asyncio.run(exercise())


def test_duplicate_suppression_survives_submit_ack_crash(tmp_path) -> None:
    async def exercise() -> None:
        path = tmp_path / "state.db"
        first_store = StateStore(path)
        first_adapter = paper("coinbase", first_store, crash=True)
        first_manager = OrderManager(first_store, clock=lambda: NOW)
        order = approved("stable", Side.BUY, "1", order_type=OrderType.LIMIT, price="99")
        with suppress(ConnectionError):
            await first_manager.submit(first_adapter, order)

        restarted_store = StateStore(path)
        restarted_adapter = paper("coinbase", restarted_store)
        restarted_manager = OrderManager(restarted_store, clock=lambda: NOW)
        ack = await restarted_manager.submit(restarted_adapter, order)
        assert ack.venue_order_id == "coinbase-paper-1"
        assert len(await restarted_adapter.get_order_history()) == 1

    asyncio.run(exercise())


def test_maker_first_three_reprices_and_urgent_cross_only() -> None:
    base = approved(
        "maker", Side.SELL, "1", order_type=OrderType.LIMIT, price="105", is_exit=True
    ).order
    quote = TouchQuote(Decimal("99"), Decimal("101"))
    for attempt in range(4):
        planned = MakerFirstPolicy.plan(base, quote, reprices=attempt, urgent=False)
        assert planned is not None
        assert planned.order_type is OrderType.LIMIT
        assert planned.limit_price == Decimal("101")
    assert MakerFirstPolicy.plan(base, quote, reprices=4, urgent=False) is None
    urgent = MakerFirstPolicy.plan(base, quote, reprices=4, urgent=True)
    assert urgent is not None
    assert urgent.order_type is OrderType.MARKET
    assert urgent.expected_price == Decimal("99")


def test_unroutable_spot_short_is_fit_to_flat_and_journaled(tmp_path) -> None:
    adapter = paper("coinbase", StateStore(tmp_path / "paper.db"))
    router = ExecutionRouter(
        {"coinbase": adapter},
        {"coinbase": capability()},
        {"coinbase": VenueHealth("coinbase", clock=lambda: NOW)},
        AppendOnlyJournal(tmp_path / "router.jsonl"),
        clock=lambda: NOW,
    )
    request = approved("short", Side.SELL, "1", order_type=OrderType.LIMIT, price="101")
    decision = router.route(request, InstrumentType.SPOT, current_position=Decimal("0.4"))
    assert decision.approved is None
    assert decision.fitted_order is not None
    assert decision.fitted_order.quantity == Decimal("0.4")
    assert decision.fitted_order.is_exit
    assert "capability_fit_flat" in (tmp_path / "router.jsonl").read_text()


def test_paper_maker_requires_trade_through_and_taker_pays_ask_plus_fee(tmp_path) -> None:
    async def exercise() -> None:
        adapter = paper("coinbase", StateStore(tmp_path / "paper.db"))
        await adapter.submit_order(
            approved("maker-fill", Side.BUY, "1", order_type=OrderType.LIMIT, price="99")
        )
        exact_touch = await adapter.update_quote(
            LiveQuote("BTC-USD", Decimal("99"), Decimal("101"), Decimal("99"), NOW)
        )
        assert exact_touch == ()
        traded_through = await adapter.update_quote(
            LiveQuote("BTC-USD", Decimal("98"), Decimal("100"), Decimal("98.99"), NOW)
        )
        assert traded_through[0].price == Decimal("99")
        assert traded_through[0].fee == Decimal("0.099")

        await adapter.submit_order(
            approved("taker-fill", Side.BUY, "1", order_type=OrderType.MARKET)
        )
        assert adapter.fills[-1].price == Decimal("100")
        assert adapter.fills[-1].fee == Decimal("0.200")

    asyncio.run(exercise())
