from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from ib_async import Future, OrderStatus, Trade

from adapters.ibkr import (
    ContractQualificationError,
    IBKRAdapter,
    IBKRConfig,
    PaperAccountRequired,
    StopCoverageError,
)
from core.events import OrderRequest, OrderType, Side
from core.risk import Approved
from core.state import StateStore


class FakeIB:
    def __init__(self, account: str = "DU123") -> None:
        self.account = account
        self.connected = False
        self.placed: list[Trade] = []
        self.position_rows: list[Any] = []
        self.connect_failures = 0

    async def connectAsync(self, *_args: object, **_kwargs: object) -> None:
        if self.connect_failures:
            self.connect_failures -= 1
            raise ConnectionError("daily restart")
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def isConnected(self) -> bool:
        return self.connected

    def managedAccounts(self) -> list[str]:
        return [self.account]

    async def qualifyContractsAsync(self, candidate: Future) -> list[Future]:
        candidate.conId = 1000 + len(self.placed)
        candidate.localSymbol = f"{candidate.symbol}{candidate.lastTradeDateOrContractMonth}"
        return [candidate]

    async def reqOpenOrdersAsync(self) -> list[Trade]:
        return [trade for trade in self.placed if not trade.isDone()]

    async def reqCompletedOrdersAsync(self, _api_only: bool) -> list[Trade]:
        return [trade for trade in self.placed if trade.isDone()]

    def trades(self) -> list[Trade]:
        return list(self.placed)

    def openTrades(self) -> list[Trade]:
        return [trade for trade in self.placed if not trade.isDone()]

    def placeOrder(self, contract: Future, order: Any) -> Trade:
        order.orderId = len(self.placed) + 1
        trade = Trade(contract, order, OrderStatus(order.orderId, "Submitted"))
        self.placed.append(trade)
        return trade

    def cancelOrder(self, order: Any) -> None:
        for trade in self.placed:
            if trade.order is order:
                trade.orderStatus.status = "Cancelled"

    async def reqPositionsAsync(self) -> list[Any]:
        return self.position_rows

    async def accountSummaryAsync(self, account: str) -> list[Any]:
        return [
            SimpleNamespace(
                account=account,
                tag="NetLiquidation",
                value="100000",
                currency="USD",
            )
        ]


def _approved(client_id: str, order_type: OrderType, price: str | None = None) -> Approved:
    request = OrderRequest(
        client_id,
        "MES",
        Side.BUY,
        Decimal("1"),
        order_type,
        datetime(2026, 7, 6, 14, 31, tzinfo=UTC),
        limit_price=Decimal(price) if order_type is OrderType.LIMIT and price else None,
        strategy_id="futures_trend",
        expected_price=Decimal(price or "6000"),
    )
    return Approved(request, request.quantity * Decimal(price or "6000"))


def test_submit_is_idempotent_across_adapter_restart(tmp_path: Path) -> None:
    async def exercise() -> None:
        state = StateStore(tmp_path / "state.db")
        fake = FakeIB()
        first = IBKRAdapter(IBKRConfig(account="DU123"), state, ib=fake)
        await first.connect()
        approval = _approved("mes-entry-1", OrderType.LIMIT, "6000")
        first_ack = await first.submit_order(approval, "202609")

        restarted = IBKRAdapter(IBKRConfig(account="DU123"), state, ib=fake)
        second_ack = await restarted.submit_order(approval, "202609")

        assert len(fake.placed) == 1
        assert first_ack.venue_order_id == second_ack.venue_order_id == "1"

    asyncio.run(exercise())


def test_daily_gateway_restart_reconnects(tmp_path: Path) -> None:
    async def exercise() -> None:
        fake = FakeIB()
        fake.connect_failures = 1
        sleeps: list[float] = []

        async def no_wait(seconds: float) -> None:
            sleeps.append(seconds)

        adapter = IBKRAdapter(
            IBKRConfig(account="DU123", restart_retries=2),
            StateStore(tmp_path / "state.db"),
            ib=fake,
            sleep=no_wait,
        )
        await adapter.ensure_connected()

        assert fake.connected
        assert sleeps == [1.0]

    asyncio.run(exercise())


def test_gateway_with_live_account_is_rejected(tmp_path: Path) -> None:
    async def exercise() -> None:
        fake = FakeIB(account="U999")
        adapter = IBKRAdapter(
            IBKRConfig(account="DU123"),
            StateStore(tmp_path / "state.db"),
            ib=fake,
        )
        with pytest.raises(PaperAccountRequired):
            await adapter.connect()

    asyncio.run(exercise())


def test_contract_qualification_rejects_mismatched_broker_metadata(tmp_path: Path) -> None:
    async def exercise() -> None:
        class MismatchedIB(FakeIB):
            async def qualifyContractsAsync(self, candidate: Future) -> list[Future]:
                qualified = await super().qualifyContractsAsync(candidate)
                qualified[0].currency = "EUR"
                return qualified

        fake = MismatchedIB()
        adapter = IBKRAdapter(
            IBKRConfig(account="DU123"),
            StateStore(tmp_path / "state.db"),
            ib=fake,
        )
        await adapter.connect()
        with pytest.raises(ContractQualificationError):
            await adapter.qualify_contract("MES", "202609")

    asyncio.run(exercise())


def test_stop_verification_writes_demonstrable_redacted_output(tmp_path: Path) -> None:
    async def exercise() -> None:
        fake = FakeIB()
        adapter = IBKRAdapter(
            IBKRConfig(account="DU123"),
            StateStore(tmp_path / "state.db"),
            ib=fake,
        )
        await adapter.connect()
        contract = await adapter.qualify_contract("MES", "202609")
        fake.position_rows = [
            SimpleNamespace(
                account="DU123",
                contract=contract,
                position=Decimal("2"),
                avgCost=Decimal("6000"),
            )
        ]
        stop_approval = Approved(
            OrderRequest(
                "catstop:mes",
                "MES",
                Side.SELL,
                Decimal("2"),
                OrderType.STOP,
                datetime(2026, 7, 6, tzinfo=UTC),
                stop_price=Decimal("5800"),
                strategy_id="futures_trend",
                expected_price=Decimal("6000"),
                is_exit=True,
            ),
            Decimal("12000"),
        )
        await adapter.submit_catastrophe_stop(stop_approval, "202609")
        output = tmp_path / "stop-report.json"
        report = await adapter.write_stop_verification(output)
        payload = json.loads(output.read_text(encoding="utf-8"))

        assert report.all_covered
        assert payload["account"] == "paper"
        assert "DU123" not in output.read_text(encoding="utf-8")
        assert payload["checks"][0]["reason"] == "ok"

    asyncio.run(exercise())


def test_missing_broker_stop_fails_verification_job(tmp_path: Path) -> None:
    async def exercise() -> None:
        fake = FakeIB()
        adapter = IBKRAdapter(
            IBKRConfig(account="DU123"),
            StateStore(tmp_path / "state.db"),
            ib=fake,
        )
        await adapter.connect()
        contract = await adapter.qualify_contract("MES", "202609")
        fake.position_rows = [
            SimpleNamespace(account="DU123", contract=contract, position=1, avgCost=6000)
        ]
        with pytest.raises(StopCoverageError):
            await adapter.write_stop_verification(tmp_path / "failed.json")

    asyncio.run(exercise())


def test_limit_fallback_requires_separate_approved_market_order(tmp_path: Path) -> None:
    async def exercise() -> None:
        fake = FakeIB()
        now = datetime(2026, 7, 6, 14, 31, tzinfo=UTC)

        def clock() -> datetime:
            return now + timedelta(seconds=len(sleeps))

        sleeps: list[float] = []

        async def advance(seconds: float) -> None:
            sleeps.append(seconds)

        adapter = IBKRAdapter(
            IBKRConfig(account="DU123", limit_timeout_seconds=1),
            StateStore(tmp_path / "state.db"),
            ib=fake,
            clock=clock,
            sleep=advance,
        )
        await adapter.connect()
        ack = await adapter.submit_limit_with_market_fallback(
            _approved("mes-limit", OrderType.LIMIT, "6000"),
            _approved("mes-market", OrderType.MARKET),
            "202609",
        )

        assert [trade.order.orderType for trade in fake.placed] == ["LMT", "MKT"]
        assert fake.placed[0].orderStatus.status == "Cancelled"
        assert ack.client_order_id == "mes-market"

    asyncio.run(exercise())
