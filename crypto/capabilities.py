"""Metadata-observable venue capabilities and conservative startup verification."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from crypto.symbols import Venue

# Official sources verified 2026-07-04:
# Coinbase Exchange REST: 10 requests/s, burst 15.
# https://docs.cdp.coinbase.com/exchange/rest-api/rate-limits
# Kraken Starter REST: counter max 15, decay 0.33/s.
# https://docs.kraken.com/exchange/guides/rest/ratelimits
SAFETY_FACTOR = Decimal("0.70")


@dataclass(frozen=True, slots=True)
class RateBudget:
    capacity: Decimal
    refill_per_second: Decimal


COINBASE_PUBLIC_BUDGET = RateBudget(
    capacity=Decimal("15") * SAFETY_FACTOR,
    refill_per_second=Decimal("10") * SAFETY_FACTOR,
)
KRAKEN_STARTER_BUDGET = RateBudget(
    capacity=Decimal("15") * SAFETY_FACTOR,
    refill_per_second=Decimal("0.33") * SAFETY_FACTOR,
)


class TokenBucket:
    def __init__(
        self,
        budget: RateBudget,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if budget.capacity <= 0 or budget.refill_per_second <= 0:
            raise ValueError("token-bucket budget must be positive")
        self.budget = budget
        self._clock = clock
        self._tokens = budget.capacity
        self._updated_at = clock()
        self._lock = asyncio.Lock()

    async def acquire(self, cost: Decimal = Decimal("1")) -> None:
        if cost <= 0 or cost > self.budget.capacity:
            raise ValueError("token cost must be positive and no greater than capacity")
        while True:
            async with self._lock:
                now = self._clock()
                elapsed = Decimal(str(max(0.0, now - self._updated_at)))
                self._tokens = min(
                    self.budget.capacity,
                    self._tokens + elapsed * self.budget.refill_per_second,
                )
                self._updated_at = now
                if self._tokens >= cost:
                    self._tokens -= cost
                    return
                wait_seconds = float((cost - self._tokens) / self.budget.refill_per_second)
            await asyncio.sleep(wait_seconds)


@dataclass(frozen=True, slots=True)
class VenueCapabilities:
    spot: bool
    perpetual_futures: bool
    margin: bool
    market_orders: bool
    precision_metadata: bool
    min_notional_metadata: bool
    active_symbols: frozenset[str]


@dataclass(frozen=True, slots=True)
class VenueVerification:
    venue: Venue
    declared: VenueCapabilities
    observed: VenueCapabilities
    mismatches: tuple[str, ...]

    @property
    def degraded(self) -> bool:
        return bool(self.mismatches)


def observe_capabilities(venue: Venue, payload: Mapping[str, object]) -> VenueCapabilities:
    if venue is Venue.COINBASE:
        return _observe_coinbase(payload)
    return _observe_kraken(payload)


def verify_capabilities(
    venue: Venue,
    declared: VenueCapabilities,
    payload: Mapping[str, object],
) -> VenueVerification:
    observed = observe_capabilities(venue, payload)
    mismatches: list[str] = []
    for field in (
        "spot",
        "perpetual_futures",
        "margin",
        "market_orders",
        "precision_metadata",
        "min_notional_metadata",
    ):
        if getattr(declared, field) != getattr(observed, field):
            mismatches.append(
                f"{field}: declared={getattr(declared, field)} observed={getattr(observed, field)}"
            )
    missing = declared.active_symbols - observed.active_symbols
    if missing:
        mismatches.append(f"active_symbols missing from metadata: {sorted(missing)}")
    return VenueVerification(venue, declared, observed, tuple(mismatches))


async def fetch_live_metadata(
    venue: Venue,
    *,
    client: httpx.AsyncClient | None = None,
    limiter: TokenBucket | None = None,
) -> dict[str, object]:
    owned = client is None
    http = client or httpx.AsyncClient(timeout=30.0)
    bucket = limiter or TokenBucket(
        COINBASE_PUBLIC_BUDGET if venue is Venue.COINBASE else KRAKEN_STARTER_BUDGET
    )
    try:
        await bucket.acquire()
        if venue is Venue.COINBASE:
            response = await http.get(
                "https://api.coinbase.com/api/v3/brokerage/market/products",
                params={"limit": "250"},
            )
        else:
            response = await http.get(
                "https://api.kraken.com/0/public/AssetPairs",
                params={"assetVersion": "1"},
            )
        response.raise_for_status()
        value: Any = response.json()
        if not isinstance(value, dict):
            raise ValueError("venue metadata response must be an object")
        return value
    finally:
        if owned:
            await http.aclose()


async def verify_live_at_startup(
    venue: Venue,
    declared: VenueCapabilities,
    *,
    client: httpx.AsyncClient | None = None,
    limiter: TokenBucket | None = None,
) -> VenueVerification:
    payload = await fetch_live_metadata(venue, client=client, limiter=limiter)
    return verify_capabilities(venue, declared, payload)


def _observe_coinbase(payload: Mapping[str, object]) -> VenueCapabilities:
    products = payload.get("products")
    if not isinstance(products, list):
        raise ValueError("Coinbase metadata has no products list")
    active = [
        product
        for product in products
        if isinstance(product, Mapping)
        and product.get("status") == "online"
        and product.get("trading_disabled") is False
    ]
    symbols = frozenset(str(product["product_id"]) for product in active)
    futures = [product for product in active if product.get("product_type") == "FUTURE"]
    perpetual = any(
        isinstance(product.get("future_product_details"), Mapping)
        and (
            product["future_product_details"].get("contract_expiry_type") == "PERPETUAL"
            or isinstance(product["future_product_details"].get("perpetual_details"), Mapping)
        )
        for product in futures
    )
    spot = [product for product in active if product.get("product_type") == "SPOT"]
    return VenueCapabilities(
        spot=bool(spot),
        perpetual_futures=perpetual,
        margin=bool(futures),
        market_orders=bool(active)
        and any(product.get("limit_only") is False for product in active),
        precision_metadata=bool(active)
        and all(
            product.get("price_increment") and product.get("base_increment") for product in active
        ),
        min_notional_metadata=bool(active)
        and all(product.get("quote_min_size") for product in active),
        active_symbols=symbols,
    )


def _observe_kraken(payload: Mapping[str, object]) -> VenueCapabilities:
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise ValueError("Kraken metadata has no result mapping")
    active = {
        str(name): details
        for name, details in result.items()
        if isinstance(details, Mapping) and details.get("status") == "online"
    }
    symbols = frozenset(name.replace("/", "-").replace("XBT", "BTC") for name in active)
    return VenueCapabilities(
        spot=bool(active),
        perpetual_futures=False,
        margin=any(bool(details.get("leverage_buy")) for details in active.values()),
        market_orders=bool(active),
        precision_metadata=bool(active)
        and all(
            details.get("tick_size") and details.get("lot_decimals") is not None
            for details in active.values()
        ),
        min_notional_metadata=bool(active)
        and all(details.get("costmin") for details in active.values()),
        active_symbols=symbols,
    )
