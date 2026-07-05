"""A deliberately trivial pure buy-and-hold strategy for engine validation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from core.events import Signal, SignalAction
from strategies.types import MarketState


@dataclass(frozen=True, slots=True)
class BuyAndHoldParams:
    symbol: str
    quantity: Decimal
    strategy_id: str = "buy_and_hold"


def buy_and_hold(state: MarketState, params: BuyAndHoldParams) -> tuple[Signal, ...]:
    """Buy once when flat, then emit no further signals."""

    if state.bar.symbol != params.symbol or state.position_quantity != 0:
        return ()
    return (
        Signal(
            strategy_id=params.strategy_id,
            symbol=params.symbol,
            action=SignalAction.BUY,
            generated_at=state.bar.timestamp,
            quantity=params.quantity,
            reference_price=state.bar.close,
        ),
    )
