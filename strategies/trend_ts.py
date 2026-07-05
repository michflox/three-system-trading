"""Exact time-series trend specification; pure and deterministic."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from core.events import Signal, SignalAction
from strategies.types import MarketState

HALF = Decimal("0.5")
ONE = Decimal("1")
ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class TrendParams:
    fast_1: int = 8
    slow_1: int = 32
    fast_2: int = 16
    slow_2: int = 64
    fast_3: int = 32
    slow_3: int = 128
    breakout_days: int = 100
    breakout_decay: Decimal = Decimal("0.9")

    def __post_init__(self) -> None:
        pairs = ((self.fast_1, self.slow_1), (self.fast_2, self.slow_2), (self.fast_3, self.slow_3))
        if any(fast < 1 or slow <= fast for fast, slow in pairs):
            raise ValueError("each EWMA pair requires 1 <= fast < slow")
        if self.breakout_days < 2:
            raise ValueError("breakout_days must be at least two")
        if not self.breakout_decay.is_finite() or self.breakout_decay <= ZERO:
            raise ValueError("breakout_decay must be positive and finite")


DEFAULT_TREND_PARAMS = TrendParams()


def trend_ts(market_state: MarketState, params: TrendParams) -> tuple[Signal, ...]:
    """Return the mandated score as a signed target quantity in [-1, 1]."""

    bars = market_state.history or (market_state.bar,)
    if bars[-1] != market_state.bar:
        raise ValueError("history must end at the current bar")
    closes = tuple(bar.close for bar in bars)
    score = trend_score(closes, params)
    if score > ZERO:
        action = SignalAction.BUY
        quantity: Decimal | None = score
    elif score < ZERO:
        action = SignalAction.SELL
        quantity = -score
    else:
        action = SignalAction.HOLD
        quantity = None
    return (
        Signal(
            strategy_id="trend_ts",
            symbol=market_state.bar.symbol,
            action=action,
            generated_at=market_state.bar.timestamp,
            quantity=quantity,
            reference_price=market_state.bar.close,
        ),
    )


def trend_score(closes: tuple[Decimal, ...], params: TrendParams) -> Decimal:
    if not closes or any(not price.is_finite() or price <= ZERO for price in closes):
        raise ValueError("closes must be positive finite Decimals")
    warmup = max(params.slow_1, params.slow_2, params.slow_3, params.breakout_days + 1)
    if len(closes) < warmup:
        return ZERO
    crossover = (
        _sign(_ewma(closes, params.fast_1) - _ewma(closes, params.slow_1))
        + _sign(_ewma(closes, params.fast_2) - _ewma(closes, params.slow_2))
        + _sign(_ewma(closes, params.fast_3) - _ewma(closes, params.slow_3))
    ) / Decimal("3")
    breakout = _breakout(closes, params.breakout_days, params.breakout_decay)
    return max(-ONE, min(ONE, HALF * crossover + HALF * breakout))


def _ewma(values: tuple[Decimal, ...], span: int) -> Decimal:
    alpha = Decimal("2") / Decimal(span + 1)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (ONE - alpha) * result
    return result


def _breakout(values: tuple[Decimal, ...], window: int, decay: Decimal) -> Decimal:
    direction = ZERO
    age = 0
    for index in range(window, len(values)):
        previous = values[index - window : index]
        current = values[index]
        if current > max(previous):
            direction = ONE
            age = 0
        elif current < min(previous):
            direction = -ONE
            age = 0
        elif direction != ZERO:
            age += 1
    return direction * (decay**age)


def _sign(value: Decimal) -> Decimal:
    if value > ZERO:
        return ONE
    if value < ZERO:
        return -ONE
    return ZERO
