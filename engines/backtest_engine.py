"""Event-driven daily backtest using production strategy and risk interfaces."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Generic, TypeVar

from core.events import Bar, Fill, OrderRequest, OrderType, Side, Signal, SignalAction
from core.risk import Approved, AssetClass, PortfolioSnapshot, Quote, RiskManager, Venue
from engines.fees import FeeSchedule
from strategies.types import MarketState, Strategy

ParamsT = TypeVar("ParamsT")
ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class DailyMarketEvent:
    bar: Bar
    bid: Decimal
    ask: Decimal
    venue: Venue
    asset_class: AssetClass
    point_value: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        values = (self.bid, self.ask, self.point_value, self.bar.volume)
        if any(not value.is_finite() or value <= ZERO for value in values):
            raise ValueError("market event values must be positive finite Decimals")
        if self.bid > self.ask:
            raise ValueError("bid cannot exceed ask")


@dataclass(frozen=True, slots=True)
class SimulatedFill:
    fill: Fill
    commission: Decimal
    slippage: Decimal
    maker: bool


@dataclass(frozen=True, slots=True)
class EquityPoint:
    timestamp: datetime
    equity: Decimal


@dataclass(frozen=True, slots=True)
class BacktestResult:
    equity_curve: tuple[EquityPoint, ...]
    fills: tuple[SimulatedFill, ...]
    ending_cash: Decimal
    ending_positions: dict[str, Decimal]


class BacktestEngine(Generic[ParamsT]):
    """Run feed -> pure strategy -> shared risk -> fills -> portfolio each day."""

    def __init__(
        self,
        *,
        initial_equity: Decimal,
        strategy: Strategy[ParamsT],
        strategy_params: ParamsT,
        risk_manager: RiskManager,
        fee_schedules: dict[str, FeeSchedule],
        slippage_rate: Decimal,
        default_order_type: OrderType = OrderType.MARKET,
    ) -> None:
        if not initial_equity.is_finite() or initial_equity <= ZERO:
            raise ValueError("initial equity must be a positive finite Decimal")
        if not slippage_rate.is_finite() or slippage_rate <= ZERO:
            raise ValueError("slippage must always be a positive finite Decimal")
        if not fee_schedules:
            raise ValueError("at least one fee schedule is required")
        self._initial_equity = initial_equity
        self._strategy = strategy
        self._params = strategy_params
        self._risk = risk_manager
        self._fees = fee_schedules
        self._slippage_rate = slippage_rate
        self._order_type = default_order_type

    def run(self, feed: Iterable[DailyMarketEvent]) -> BacktestResult:
        cash = self._initial_equity
        positions: dict[str, Decimal] = {}
        strategy_allocations: dict[str, Decimal] = {}
        venue_allocations: dict[str, Decimal] = {}
        fills: list[SimulatedFill] = []
        curve: list[EquityPoint] = []
        previous_equity = self._initial_equity
        week_start_equity = self._initial_equity
        high_water_mark = self._initial_equity
        previous_date: date | None = None

        for event in feed:
            current_date = event.bar.timestamp.date()
            if previous_date is not None and current_date <= previous_date:
                raise ValueError("daily feed must be strictly chronological")
            current_iso = current_date.isocalendar()
            previous_iso = previous_date.isocalendar() if previous_date is not None else None
            if previous_iso is not None and (current_iso.year, current_iso.week) != (
                previous_iso.year,
                previous_iso.week,
            ):
                week_start_equity = previous_equity
            previous_date = current_date

            symbol = event.bar.symbol
            current_position = positions.get(symbol, ZERO)
            marked_equity = cash + current_position * event.bar.close * event.point_value
            high_water_mark = max(high_water_mark, marked_equity)
            state = MarketState(bar=event.bar, position_quantity=current_position)
            signals = self._strategy(state, self._params)
            self._validate_signals(signals, event)

            for index, signal in enumerate(signals):
                order = self._order_from_signal(signal, event, current_position, index)
                if order is None:
                    continue
                midpoint = (event.bid + event.ask) / Decimal("2")
                portfolio = PortfolioSnapshot(
                    equity=marked_equity,
                    day_start_equity=previous_equity,
                    week_start_equity=week_start_equity,
                    high_water_mark=high_water_mark,
                    positions=dict(positions),
                    strategy_allocations={
                        **strategy_allocations,
                        signal.strategy_id: strategy_allocations.get(signal.strategy_id, ZERO),
                    },
                    venue_allocations={
                        **venue_allocations,
                        event.venue.name: venue_allocations.get(event.venue.name, ZERO),
                    },
                    last_quotes={
                        symbol: Quote(
                            price=midpoint,
                            volume=event.bar.volume,
                            timestamp=event.bar.timestamp,
                            asset_class=event.asset_class,
                        )
                    },
                )
                decision = self._risk.approve(order, portfolio, event.venue)
                if not isinstance(decision, Approved):
                    continue
                simulated = self._simulate_fill(order, event)
                if simulated is None:
                    continue
                fills.append(simulated)
                signed_quantity = (
                    simulated.fill.quantity
                    if simulated.fill.side is Side.BUY
                    else -simulated.fill.quantity
                )
                trade_value = simulated.fill.quantity * simulated.fill.price * event.point_value
                costs = simulated.commission + (simulated.slippage if simulated.maker else ZERO)
                cash += (
                    -trade_value - costs if simulated.fill.side is Side.BUY else trade_value - costs
                )
                positions[symbol] = positions.get(symbol, ZERO) + signed_quantity
                current_position = positions[symbol]
                allocation = abs(current_position * event.bar.close * event.point_value)
                strategy_allocations[signal.strategy_id] = allocation
                venue_allocations[event.venue.name] = allocation
                marked_equity = cash + current_position * event.bar.close * event.point_value

            high_water_mark = max(high_water_mark, marked_equity)
            previous_equity = marked_equity
            curve.append(EquityPoint(timestamp=event.bar.timestamp, equity=marked_equity))

        return BacktestResult(
            equity_curve=tuple(curve),
            fills=tuple(fills),
            ending_cash=cash,
            ending_positions=dict(positions),
        )

    @staticmethod
    def _validate_signals(signals: Sequence[Signal], event: DailyMarketEvent) -> None:
        for signal in signals:
            if signal.symbol != event.bar.symbol or signal.generated_at != event.bar.timestamp:
                raise ValueError("strategy signals must match the current market event")

    def _order_from_signal(
        self,
        signal: Signal,
        event: DailyMarketEvent,
        current_position: Decimal,
        index: int,
    ) -> OrderRequest | None:
        if signal.action is SignalAction.HOLD:
            return None
        if signal.action is SignalAction.EXIT:
            if current_position == ZERO:
                return None
            side = Side.SELL if current_position > ZERO else Side.BUY
            quantity = abs(current_position)
            is_exit = True
        else:
            side = Side.BUY if signal.action is SignalAction.BUY else Side.SELL
            if signal.quantity is None:
                raise ValueError("BUY and SELL signals require a Decimal quantity")
            quantity = signal.quantity
            is_exit = False
        midpoint = (event.bid + event.ask) / Decimal("2")
        limit_price = signal.reference_price if self._order_type is OrderType.LIMIT else None
        expected_price = limit_price if limit_price is not None else midpoint
        return OrderRequest(
            client_order_id=(
                f"{signal.strategy_id}:{signal.symbol}:{event.bar.timestamp.isoformat()}:{index}"
            ),
            symbol=signal.symbol,
            side=side,
            quantity=quantity,
            order_type=self._order_type,
            created_at=event.bar.timestamp,
            limit_price=limit_price,
            strategy_id=signal.strategy_id,
            expected_price=expected_price,
            is_exit=is_exit,
        )

    def _simulate_fill(
        self,
        order: OrderRequest,
        event: DailyMarketEvent,
    ) -> SimulatedFill | None:
        schedule = self._fees.get(event.venue.name)
        if schedule is None:
            raise ValueError(f"missing fee schedule for venue {event.venue.name!r}")
        maker = order.order_type is OrderType.LIMIT
        if maker:
            if order.limit_price is None:
                raise ValueError("limit order has no limit price")
            traded_through = (
                event.bar.low < order.limit_price
                if order.side is Side.BUY
                else event.bar.high > order.limit_price
            )
            if not traded_through:
                return None
            fill_price = order.limit_price
        else:
            spread_cross = event.ask if order.side is Side.BUY else event.bid
            fill_price = spread_cross

        notional = order.quantity * fill_price * event.point_value
        slippage = notional * self._slippage_rate
        if not maker:
            adverse_multiplier = (
                Decimal("1") + self._slippage_rate
                if order.side is Side.BUY
                else Decimal("1") - self._slippage_rate
            )
            fill_price *= adverse_multiplier
            notional = order.quantity * fill_price * event.point_value
            slippage = abs(fill_price - spread_cross) * order.quantity * event.point_value
        commission = schedule.calculate(quantity=order.quantity, notional=notional, maker=maker)
        fill = Fill(
            order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=fill_price,
            fee=commission,
            timestamp=event.bar.timestamp,
        )
        return SimulatedFill(
            fill=fill,
            commission=commission,
            slippage=slippage,
            maker=maker,
        )
