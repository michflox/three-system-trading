"""The single, ordered risk-approval path for every order."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import cast

from core.events import OrderRequest, Side
from core.execution_mode import ExecutionMode, LiveGateConfig, is_live_permitted
from ops.journal import AppendOnlyJournal

DAILY_LOSS_LIMIT = Decimal("-0.02")
WEEKLY_LOSS_LIMIT = Decimal("-0.05")
HWM_DRAWDOWN_LIMIT = Decimal("-0.15")
MAX_QUOTE_DEVIATION = Decimal("0.02")


class AssetClass(StrEnum):
    EQUITY = "equity"
    FUTURE = "future"
    OPTION = "option"
    CRYPTO = "crypto"


class RiskReason(StrEnum):
    EXECUTION_MODE_GATE = "execution_mode_gate"
    INVALID_NUMERIC_INPUT = "invalid_numeric_input"
    INVALID_EXIT = "invalid_exit"
    MISSING_RISK_LIMIT = "missing_risk_limit"
    POSITION_CAP = "position_cap"
    STRATEGY_ALLOCATION_CAP = "strategy_allocation_cap"
    VENUE_CAPITAL_CAP = "venue_capital_cap"
    DAILY_LOSS_HALT = "daily_loss_halt"
    WEEKLY_LOSS_HALT = "weekly_loss_halt"
    HWM_KILL_SWITCH = "hwm_kill_switch"
    ORDER_NOTIONAL = "order_notional_vs_equity"
    PRICE_DEVIATION = "price_deviation"
    DUPLICATE_ORDER = "duplicate_order"
    STALE_DATA = "stale_data"


@dataclass(frozen=True, slots=True)
class Quote:
    price: Decimal
    volume: Decimal
    timestamp: datetime
    asset_class: AssetClass


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    equity: Decimal
    day_start_equity: Decimal
    week_start_equity: Decimal
    high_water_mark: Decimal
    positions: Mapping[str, Decimal]
    strategy_allocations: Mapping[str, Decimal]
    venue_allocations: Mapping[str, Decimal]
    last_quotes: Mapping[str, Quote]


@dataclass(frozen=True, slots=True)
class Venue:
    name: str


@dataclass(frozen=True, slots=True)
class RiskConfig:
    live_gate: LiveGateConfig
    instrument_position_caps: Mapping[str, Decimal]
    strategy_allocation_caps: Mapping[str, Decimal]
    venue_capital_caps: Mapping[str, Decimal]
    staleness_thresholds: Mapping[AssetClass, timedelta]
    config_path: Path
    max_order_notional_fraction: Decimal = Decimal("0.10")


@dataclass(frozen=True, slots=True)
class Approved:
    order: OrderRequest
    notional: Decimal


@dataclass(frozen=True, slots=True)
class Vetoed:
    reason: RiskReason
    flatten_and_halt: bool = False
    kill_switch: bool = False


RiskDecision = Approved | Vetoed


class RiskManager:
    """Apply every risk rule in one ordered, fail-closed entry point."""

    def __init__(
        self,
        mode: ExecutionMode,
        config: RiskConfig,
        journal: AppendOnlyJournal,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._mode = mode
        self._config = config
        self._journal = journal
        self._environ = environ
        self._approved_client_ids: set[str] = set()
        self._live_killed = False
        self._lock = threading.RLock()

    def approve(
        self,
        order: OrderRequest,
        portfolio: PortfolioSnapshot,
        venue: Venue,
    ) -> RiskDecision:
        """Approve or veto an order after applying the complete ordered policy."""

        with self._lock:
            return self._approve_locked(order, portfolio, venue)

    def _approve_locked(
        self,
        order: OrderRequest,
        portfolio: PortfolioSnapshot,
        venue: Venue,
    ) -> RiskDecision:
        # 1. Execution-mode gate.
        if self._mode is ExecutionMode.LIVE and (
            (self._live_killed and not order.is_exit)
            or (
                not self._live_killed
                and not is_live_permitted(self._config.live_gate, self._environ)
            )
        ):
            return self._veto(RiskReason.EXECUTION_MODE_GATE, order, venue)

        quote = portfolio.last_quotes.get(order.symbol)
        if quote is None or not self._numeric_inputs_valid(order, portfolio, quote):
            return self._veto(RiskReason.INVALID_NUMERIC_INPUT, order, venue)

        order_price = self._order_price(order, quote)
        if order_price is None:
            return self._veto(RiskReason.INVALID_NUMERIC_INPUT, order, venue)
        notional = order.quantity * order_price
        current_quantity = portfolio.positions.get(order.symbol, Decimal("0"))
        signed_quantity = order.quantity if order.side is Side.BUY else -order.quantity
        projected_quantity = current_quantity + signed_quantity
        does_not_cross_zero = projected_quantity == 0 or (
            (current_quantity > 0 and projected_quantity > 0)
            or (current_quantity < 0 and projected_quantity < 0)
        )
        risk_reducing = (
            current_quantity != 0
            and abs(projected_quantity) < abs(current_quantity)
            and does_not_cross_zero
        )
        if order.is_exit and not risk_reducing:
            return self._veto(RiskReason.INVALID_EXIT, order, venue)

        # Risk-reducing exits bypass exposure caps, but never data-integrity checks.
        if not risk_reducing:
            # 2. Per-instrument position cap. Equality is permitted; exceeding it is not.
            position_cap = self._config.instrument_position_caps.get(order.symbol)
            if not self._positive_finite(position_cap):
                return self._veto(RiskReason.MISSING_RISK_LIMIT, order, venue)
            assert position_cap is not None
            if abs(projected_quantity) > position_cap:
                return self._veto(RiskReason.POSITION_CAP, order, venue)

            # 3. Per-strategy allocated-notional cap.
            strategy_cap = self._config.strategy_allocation_caps.get(order.strategy_id)
            strategy_allocated = portfolio.strategy_allocations.get(order.strategy_id)
            if not self._positive_finite(strategy_cap) or not self._nonnegative_finite(
                strategy_allocated
            ):
                return self._veto(RiskReason.MISSING_RISK_LIMIT, order, venue)
            assert strategy_cap is not None and strategy_allocated is not None
            if strategy_allocated + notional > strategy_cap:
                return self._veto(RiskReason.STRATEGY_ALLOCATION_CAP, order, venue)

            # 4. Per-venue allocated-capital cap from configuration.
            venue_cap = self._config.venue_capital_caps.get(venue.name)
            venue_allocated = portfolio.venue_allocations.get(venue.name)
            if not self._positive_finite(venue_cap) or not self._nonnegative_finite(
                venue_allocated
            ):
                return self._veto(RiskReason.MISSING_RISK_LIMIT, order, venue)
            assert venue_cap is not None and venue_allocated is not None
            if venue_allocated + notional > venue_cap:
                return self._veto(RiskReason.VENUE_CAPITAL_CAP, order, venue)

        # 5. Daily/weekly loss halts. A valid risk-reducing exit remains admissible.
        daily_return = (portfolio.equity - portfolio.day_start_equity) / portfolio.day_start_equity
        weekly_return = (
            portfolio.equity - portfolio.week_start_equity
        ) / portfolio.week_start_equity
        daily_halt = daily_return <= DAILY_LOSS_LIMIT
        weekly_halt = weekly_return <= WEEKLY_LOSS_LIMIT

        # 6. HWM kill switch is evaluated even while a lower-level halt is active so
        # later snapshots can escalate. Rewriting config is part of tripping the switch.
        drawdown = (portfolio.equity - portfolio.high_water_mark) / portfolio.high_water_mark
        kill_switch = drawdown <= HWM_DRAWDOWN_LIMIT
        if kill_switch:
            self._disable_live()

        if not risk_reducing:
            if daily_halt:
                return self._veto(RiskReason.DAILY_LOSS_HALT, order, venue, flatten_and_halt=True)
            if weekly_halt:
                return self._veto(RiskReason.WEEKLY_LOSS_HALT, order, venue, flatten_and_halt=True)
            if kill_switch:
                return self._veto(
                    RiskReason.HWM_KILL_SWITCH,
                    order,
                    venue,
                    flatten_and_halt=True,
                    kill_switch=True,
                )

        # 7. Order sanity and duplicate suppression.
        if (
            not risk_reducing
            and notional > portfolio.equity * self._config.max_order_notional_fraction
        ):
            return self._veto(RiskReason.ORDER_NOTIONAL, order, venue)
        deviation = abs(order_price - quote.price) / quote.price
        if deviation > MAX_QUOTE_DEVIATION:
            return self._veto(RiskReason.PRICE_DEVIATION, order, venue)
        if order.client_order_id in self._approved_client_ids:
            return self._veto(RiskReason.DUPLICATE_ORDER, order, venue)

        # 8. Per-asset-class stale-data veto; exits explicitly remain allowed.
        threshold = self._config.staleness_thresholds.get(quote.asset_class)
        if threshold is None or threshold <= timedelta(0):
            return self._veto(RiskReason.MISSING_RISK_LIMIT, order, venue)
        quote_age = order.created_at - quote.timestamp
        if quote_age < timedelta(0):
            return self._veto(RiskReason.INVALID_NUMERIC_INPUT, order, venue)
        if not order.is_exit and quote_age >= threshold:
            return self._veto(RiskReason.STALE_DATA, order, venue)

        self._approved_client_ids.add(order.client_order_id)
        return Approved(order=order, notional=notional)

    def _numeric_inputs_valid(
        self,
        order: OrderRequest,
        portfolio: PortfolioSnapshot,
        quote: Quote,
    ) -> bool:
        if (
            order.created_at.tzinfo is None
            or order.created_at.utcoffset() is None
            or quote.timestamp.tzinfo is None
            or quote.timestamp.utcoffset() is None
        ):
            return False
        positive = (
            order.quantity,
            portfolio.equity,
            portfolio.day_start_equity,
            portfolio.week_start_equity,
            portfolio.high_water_mark,
            quote.price,
            quote.volume,
            self._config.max_order_notional_fraction,
        )
        if not all(self._positive_finite(value) for value in positive):
            return False
        candidate_price = self._order_price(order, quote)
        if not self._positive_finite(candidate_price):
            return False
        return all(self._finite(value) for value in portfolio.positions.values())

    @staticmethod
    def _order_price(order: OrderRequest, quote: Quote) -> Decimal | None:
        for candidate in (order.expected_price, order.limit_price, order.stop_price):
            if candidate is not None:
                return candidate
        return quote.price

    @staticmethod
    def _finite(value: Decimal | None) -> bool:
        return isinstance(value, Decimal) and value.is_finite()

    @classmethod
    def _positive_finite(cls, value: Decimal | None) -> bool:
        try:
            return cls._finite(value) and cast(Decimal, value) > 0
        except InvalidOperation:
            return False

    @classmethod
    def _nonnegative_finite(cls, value: Decimal | None) -> bool:
        try:
            return cls._finite(value) and cast(Decimal, value) >= 0
        except InvalidOperation:
            return False

    def _disable_live(self) -> None:
        if self._live_killed:
            return
        path = self._config.config_path
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("risk config root must be a JSON object")
        document = cast(dict[str, object], raw)
        document["live_enabled"] = False
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(document, stream, sort_keys=True, separators=(",", ":"), allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
        self._live_killed = True

    def _veto(
        self,
        reason: RiskReason,
        order: OrderRequest,
        venue: Venue,
        *,
        flatten_and_halt: bool = False,
        kill_switch: bool = False,
    ) -> Vetoed:
        decision = Vetoed(
            reason=reason,
            flatten_and_halt=flatten_and_halt,
            kill_switch=kill_switch,
        )
        self._journal.append(
            {
                "event": "risk_veto",
                "timestamp": order.created_at.isoformat(),
                "client_order_id": order.client_order_id,
                "symbol": order.symbol,
                "strategy_id": order.strategy_id,
                "venue": venue.name,
                "reason": reason.value,
                "flatten_and_halt": flatten_and_halt,
                "kill_switch": kill_switch,
            }
        )
        return decision
