"""Deterministic Decimal-only volatility-target position sizing."""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal, InvalidOperation
from itertools import pairwise
from typing import Final

TRADING_DAYS: Final = Decimal("252")
SQRT_TRADING_DAYS: Final = TRADING_DAYS.sqrt()
DEFAULT_EWMA_SPAN: Final = 32
DEFAULT_REBALANCE_BUFFER: Final = Decimal("0.25")
ZERO: Final = Decimal("0")


def ewma_volatility(
    prices: list[Decimal] | tuple[Decimal, ...], span: int = DEFAULT_EWMA_SPAN
) -> Decimal:
    """Estimate daily volatility as the EWMA root-mean-square simple return.

    The decay is expressed using the conventional span relationship
    ``alpha = 2 / (span + 1)``. At least two prices are required.
    """

    if span < 2:
        raise ValueError("EWMA span must be at least 2")
    if len(prices) < 2:
        raise ValueError("at least two prices are required")
    if any(not _positive_finite(price) for price in prices):
        raise ValueError("prices must be positive finite Decimals")

    returns = [(current / previous) - Decimal("1") for previous, current in pairwise(prices)]
    alpha = Decimal("2") / Decimal(span + 1)
    variance = returns[0] * returns[0]
    for daily_return in returns[1:]:
        variance = alpha * daily_return * daily_return + (Decimal("1") - alpha) * variance
    return variance.sqrt()


def vol_target_contracts(
    *,
    equity: Decimal,
    target_vol: Decimal,
    weight: Decimal,
    sigma: Decimal,
    point_value: Decimal,
    price: Decimal,
) -> Decimal:
    """Return whole contracts from the mandated annualized vol-target formula.

    Values with magnitude below one contract round toward zero, as do all other
    fractional results. Invalid or non-positive scale inputs fail closed to zero.
    """

    positive_inputs = (equity, target_vol, sigma, point_value, price)
    if any(not _positive_finite(value) for value in positive_inputs) or not _finite(weight):
        return ZERO
    try:
        raw_contracts = (equity * target_vol * weight) / (
            sigma * point_value * price * SQRT_TRADING_DAYS
        )
    except (InvalidOperation, ZeroDivisionError):
        return ZERO
    if not raw_contracts.is_finite():
        return ZERO
    return raw_contracts.to_integral_value(rounding=ROUND_DOWN)


def apply_sector_cap(
    *,
    target_contracts: Decimal,
    equity: Decimal,
    sector_cap_fraction: Decimal,
    current_sector_notional: Decimal,
    point_value: Decimal,
    price: Decimal,
    current_contracts: Decimal = ZERO,
) -> Decimal:
    """Clip a target so total absolute sector exposure remains within its cap."""

    if not _finite(target_contracts):
        return ZERO
    if (
        any(
            not _positive_finite(value)
            for value in (equity, sector_cap_fraction, point_value, price)
        )
        or not _nonnegative_finite(current_sector_notional)
        or not _finite(current_contracts)
    ):
        return ZERO
    instrument_notional = abs(current_contracts * point_value * price)
    other_sector_notional = max(ZERO, current_sector_notional - instrument_notional)
    remaining = equity * sector_cap_fraction - other_sector_notional
    if remaining <= ZERO:
        return ZERO
    maximum = (remaining / (point_value * price)).to_integral_value(rounding=ROUND_DOWN)
    clipped_magnitude = min(abs(target_contracts), maximum)
    return clipped_magnitude if target_contracts >= ZERO else -clipped_magnitude


def apply_rebalance_buffer(
    *,
    current_contracts: Decimal,
    target_contracts: Decimal,
    buffer: Decimal = DEFAULT_REBALANCE_BUFFER,
) -> Decimal:
    """Keep the current position unless the target changes it by at least 25%."""

    if not _finite(current_contracts) or not _finite(target_contracts):
        return ZERO
    if not _positive_finite(buffer):
        return ZERO
    if current_contracts == ZERO:
        return target_contracts
    relative_change = abs(target_contracts - current_contracts) / abs(current_contracts)
    return current_contracts if relative_change < buffer else target_contracts


def size_position(
    *,
    equity: Decimal,
    target_vol: Decimal,
    weight: Decimal,
    sigma: Decimal,
    point_value: Decimal,
    price: Decimal,
    sector_cap_fraction: Decimal,
    current_sector_notional: Decimal,
    current_contracts: Decimal,
) -> Decimal:
    """Apply vol targeting, sector clipping, then the rebalance buffer in order."""

    target = vol_target_contracts(
        equity=equity,
        target_vol=target_vol,
        weight=weight,
        sigma=sigma,
        point_value=point_value,
        price=price,
    )
    target = apply_sector_cap(
        target_contracts=target,
        equity=equity,
        sector_cap_fraction=sector_cap_fraction,
        current_sector_notional=current_sector_notional,
        point_value=point_value,
        price=price,
        current_contracts=current_contracts,
    )
    return apply_rebalance_buffer(
        current_contracts=current_contracts,
        target_contracts=target,
    )


def _finite(value: Decimal) -> bool:
    return isinstance(value, Decimal) and value.is_finite()


def _positive_finite(value: Decimal) -> bool:
    try:
        return _finite(value) and value > ZERO
    except InvalidOperation:
        return False


def _nonnegative_finite(value: Decimal) -> bool:
    try:
        return _finite(value) and value >= ZERO
    except InvalidOperation:
        return False
