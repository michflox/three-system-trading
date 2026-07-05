"""Shared pure-strategy interface used by every engine."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TypeVar

from core.events import Bar, Signal


@dataclass(frozen=True, slots=True)
class MarketState:
    bar: Bar
    position_quantity: Decimal
    history: tuple[Bar, ...] = ()


ParamsT = TypeVar("ParamsT")
Strategy = Callable[[MarketState, ParamsT], Sequence[Signal]]
