"""Persistent operational heartbeats and secret-safe Telegram alerts.

Telegram Bot API documentation checked 2026-07-05:
https://core.telegram.org/bots/api#sendmessage
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol

import httpx

from core.events import OrderRequest
from core.risk import Vetoed
from core.state import StateStore
from crypto.health import HealthSnapshot
from data.quality import DailyQualityReport
from ops.journal import AppendOnlyJournal


class AlertTransport(Protocol):
    async def send(self, text: str) -> None: ...


class NullAlertTransport:
    async def send(self, text: str) -> None:
        del text


class TelegramAlertTransport:
    """Send plain-text alerts without ever logging the bot token or response body."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not token or not chat_id:
            raise ValueError("Telegram token and chat ID are required")
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._client = client or httpx.AsyncClient(timeout=15.0)
        self._owns_client = client is None
        # httpx INFO logs include request URLs; Telegram embeds the secret token
        # in the path, so suppress those loggers before any request is attempted.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

    @classmethod
    def from_environment(cls) -> TelegramAlertTransport:
        try:
            return cls(os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"])
        except KeyError as error:
            raise RuntimeError(
                f"missing required environment variable {error.args[0]}"
            ) from None

    async def send(self, text: str) -> None:
        if not text or len(text) > 4096:
            raise ValueError("Telegram alert text must contain 1-4096 characters")
        try:
            response = await self._client.post(
                self._url,
                json={"chat_id": self._chat_id, "text": text},
            )
            payload = response.json()
            if (
                response.status_code >= 400
                or not isinstance(payload, dict)
                or not payload.get("ok")
            ):
                raise RuntimeError("Telegram rejected alert")
        except (httpx.HTTPError, ValueError, RuntimeError):
            raise RuntimeError("Telegram alert delivery failed") from None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


@dataclass(frozen=True, slots=True)
class Heartbeat:
    component: str
    timestamp: datetime
    status: str


class OpsMonitor:
    def __init__(
        self,
        state: StateStore,
        journal: AppendOnlyJournal,
        transport: AlertTransport,
        *,
        heartbeat_path: Path,
    ) -> None:
        self._state = state
        self._journal = journal
        self._transport = transport
        self._heartbeat_path = heartbeat_path

    def heartbeat(self, component: str, *, now: datetime, status: str = "ok") -> None:
        _require_aware(now)
        heartbeat = Heartbeat(component, now.astimezone(UTC), status)
        _atomic_json(self._heartbeat_path, asdict(heartbeat))

    async def position_changes(
        self,
        venue: str,
        positions: Mapping[str, Decimal],
        *,
        now: datetime,
    ) -> None:
        _require_aware(now)
        normalized = {symbol: str(quantity) for symbol, quantity in sorted(positions.items())}
        key = f"monitor:positions:{venue}"
        previous_raw = self._state.get(key)
        previous = {} if previous_raw is None else json.loads(previous_raw)
        if previous != normalized:
            await self._alert(
                "position_change",
                now,
                {"venue": venue, "previous": previous, "current": normalized},
            )
            self._state.set(
                key,
                json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode(),
            )

    async def risk_veto(self, veto: Vetoed, order: OrderRequest, *, now: datetime) -> None:
        await self._alert(
            "risk_veto",
            now,
            {
                "client_order_id": order.client_order_id,
                "symbol": order.symbol,
                "reason": veto.reason.value,
                "flatten_and_halt": veto.flatten_and_halt,
            },
        )

    async def health_transition(self, snapshot: HealthSnapshot) -> None:
        key = f"monitor:health:{snapshot.venue}"
        previous_raw = self._state.get(key)
        previous = None if previous_raw is None else previous_raw.decode()
        current = snapshot.state.value
        if previous != current:
            await self._alert(
                "health_transition",
                snapshot.checked_at,
                {
                    "venue": snapshot.venue,
                    "previous": previous,
                    "current": current,
                    "reasons": list(snapshot.reasons),
                },
            )
            self._state.set(key, current.encode())

    async def data_quality(self, report: DailyQualityReport) -> None:
        failures = [
            item.dataset
            for item in report.datasets
            if item.gaps or item.stale or item.rejected_prices or item.missing_series
        ]
        if failures:
            await self._alert(
                "data_quality_failure",
                report.generated_at,
                {"datasets": failures},
            )

    async def divergence(self, report: Mapping[str, object], *, now: datetime) -> None:
        if bool(report.get("alert")):
            await self._alert("live_backtest_divergence", now, report)

    async def _alert(
        self,
        event: str,
        timestamp: datetime,
        payload: Mapping[str, object],
    ) -> None:
        _require_aware(timestamp)
        record = {
            "event": event,
            "timestamp": timestamp.astimezone(UTC).isoformat(),
            **payload,
        }
        self._journal.append(record)
        text = f"[{event}] " + json.dumps(payload, sort_keys=True, default=str)
        try:
            await self._transport.send(text)
        except Exception as error:
            self._journal.append(
                {
                    "event": "alert_delivery_failed",
                    "timestamp": timestamp.astimezone(UTC).isoformat(),
                    "alert_event": event,
                    "error_type": type(error).__name__,
                }
            )


def _atomic_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, default=_json_default, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"cannot encode {type(value).__name__}")


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("monitor timestamps must be timezone-aware")
