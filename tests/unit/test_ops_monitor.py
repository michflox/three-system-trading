import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from core.events import OrderRequest, OrderType, Side
from core.risk import RiskReason, Vetoed
from core.state import StateStore
from crypto.health import HealthSnapshot, HealthState
from data.quality import DailyQualityReport, DatasetQuality
from ops.journal import AppendOnlyJournal
from ops.monitor import OpsMonitor, TelegramAlertTransport


class CaptureAlerts:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> None:
        self.messages.append(text)


def test_monitor_heartbeats_and_required_alert_classes(tmp_path) -> None:
    async def exercise() -> None:
        now = datetime(2026, 7, 5, tzinfo=UTC)
        capture = CaptureAlerts()
        monitor = OpsMonitor(
            StateStore(tmp_path / "state.db"),
            AppendOnlyJournal(tmp_path / "journal.jsonl"),
            capture,
            heartbeat_path=tmp_path / "heartbeat.json",
        )
        monitor.heartbeat("paper", now=now)
        await monitor.position_changes("coinbase", {"BTC-USD": Decimal("1")}, now=now)
        order = OrderRequest(
            "client-1",
            "BTC-USD",
            Side.BUY,
            Decimal("1"),
            OrderType.LIMIT,
            now,
            limit_price=Decimal("100"),
            strategy_id="trend_ts",
            expected_price=Decimal("100"),
        )
        await monitor.risk_veto(Vetoed(RiskReason.POSITION_CAP), order, now=now)
        await monitor.health_transition(
            HealthSnapshot(
                "coinbase",
                HealthState.DEGRADED,
                ("book_stale",),
                now,
                0.0,
            )
        )
        report = DailyQualityReport(
            now,
            (DatasetQuality("ohlcv:1d", 2, 0, 1, 1, False, 0, 0),),
        )
        await monitor.data_quality(report)
        await monitor.divergence({"alert": True, "absolute_divergence": "0.01"}, now=now)

        heartbeat = json.loads((tmp_path / "heartbeat.json").read_text(encoding="utf-8"))
        assert heartbeat["component"] == "paper"
        assert len(capture.messages) == 5
        lines = (tmp_path / "journal.jsonl").read_text(encoding="utf-8").splitlines()
        events = [json.loads(line)["event"] for line in lines]
        assert events == [
            "position_change",
            "risk_veto",
            "health_transition",
            "data_quality_failure",
            "live_backtest_divergence",
        ]

    asyncio.run(exercise())


def test_health_transition_is_alerted_only_when_state_changes(tmp_path) -> None:
    async def exercise() -> None:
        now = datetime(2026, 7, 5, tzinfo=UTC)
        capture = CaptureAlerts()
        monitor = OpsMonitor(
            StateStore(tmp_path / "state.db"),
            AppendOnlyJournal(tmp_path / "journal.jsonl"),
            capture,
            heartbeat_path=tmp_path / "heartbeat.json",
        )
        snapshot = HealthSnapshot("coinbase", HealthState.HEALTHY, (), now, 0.0)
        await monitor.health_transition(snapshot)
        await monitor.health_transition(snapshot)
        assert len(capture.messages) == 1

    asyncio.run(exercise())


def test_telegram_uses_documented_send_message_json() -> None:
    async def exercise() -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"ok": True, "result": {}})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        transport = TelegramAlertTransport("dummy-token", "123", client=client)
        await transport.send("hello")
        await client.aclose()
        assert requests[0].method == "POST"
        assert requests[0].url.path == "/botdummy-token/sendMessage"
        assert json.loads(requests[0].content) == {"chat_id": "123", "text": "hello"}

    asyncio.run(exercise())
