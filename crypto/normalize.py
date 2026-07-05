"""Metadata-derived order normalization and daily API fee caching."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, ROUND_UP, Decimal, InvalidOperation
from enum import StrEnum
from typing import Protocol

from crypto.symbols import SymbolRegistry, Venue


class SnapMode(StrEnum):
    DOWN = "down"
    UP = "up"
    NEAREST = "nearest"


@dataclass(frozen=True, slots=True)
class InstrumentRules:
    canonical_symbol: str
    tick_size: Decimal
    step_size: Decimal
    minimum_size: Decimal
    minimum_notional: Decimal


@dataclass(frozen=True, slots=True)
class NormalizedOrder:
    price: Decimal
    size: Decimal
    notional: Decimal


class BelowMinimumNotional(ValueError):
    pass


def snap_to_increment(
    value: Decimal, increment: Decimal, mode: SnapMode = SnapMode.DOWN
) -> Decimal:
    if not value.is_finite() or value < 0:
        raise ValueError("value must be a non-negative finite Decimal")
    if not increment.is_finite() or increment <= 0:
        raise ValueError("increment must be a positive finite Decimal")
    rounding = {
        SnapMode.DOWN: ROUND_DOWN,
        SnapMode.UP: ROUND_UP,
        SnapMode.NEAREST: ROUND_HALF_EVEN,
    }[mode]
    try:
        units = (value / increment).to_integral_value(rounding=rounding)
        return units * increment
    except InvalidOperation as error:
        raise ValueError("cannot snap Decimal") from error


def normalize_order(*, price: Decimal, size: Decimal, rules: InstrumentRules) -> NormalizedOrder:
    snapped_price = snap_to_increment(price, rules.tick_size, SnapMode.DOWN)
    snapped_size = snap_to_increment(size, rules.step_size, SnapMode.DOWN)
    if snapped_price <= 0 or snapped_size < rules.minimum_size:
        raise BelowMinimumNotional("order is below minimum price or size")
    notional = snapped_price * snapped_size
    if notional < rules.minimum_notional:
        raise BelowMinimumNotional(f"notional {notional} is below minimum {rules.minimum_notional}")
    return NormalizedOrder(snapped_price, snapped_size, notional)


def rules_from_coinbase(payload: Mapping[str, object]) -> dict[str, InstrumentRules]:
    products = payload.get("products")
    if not isinstance(products, list):
        raise ValueError("Coinbase metadata has no products list")
    result: dict[str, InstrumentRules] = {}
    for product in products:
        if not isinstance(product, Mapping) or product.get("product_type") != "SPOT":
            continue
        symbol = str(product["product_id"])
        result[symbol] = InstrumentRules(
            canonical_symbol=symbol,
            tick_size=_decimal(product["price_increment"]),
            step_size=_decimal(product["base_increment"]),
            minimum_size=_decimal(product["base_min_size"]),
            minimum_notional=_decimal(product["quote_min_size"]),
        )
    return result


def rules_from_kraken(
    payload: Mapping[str, object], registry: SymbolRegistry
) -> dict[str, InstrumentRules]:
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise ValueError("Kraken metadata has no result mapping")
    rules: dict[str, InstrumentRules] = {}
    for native, details in result.items():
        if not isinstance(details, Mapping):
            continue
        canonical = registry.to_canonical(str(native), Venue.KRAKEN)
        decimals = int(details["lot_decimals"])
        rules[canonical] = InstrumentRules(
            canonical_symbol=canonical,
            tick_size=_decimal(details["tick_size"]),
            step_size=Decimal("1").scaleb(-decimals),
            minimum_size=_decimal(details["ordermin"]),
            minimum_notional=_decimal(details["costmin"]),
        )
    return rules


@dataclass(frozen=True, slots=True)
class FeeQuote:
    maker_rate: Decimal
    taker_rate: Decimal
    retrieved_at: datetime


class FeeApi(Protocol):
    async def fetch(self, venue: Venue, symbols: Sequence[str]) -> Mapping[str, FeeQuote]: ...


class PrivateVenueApi(Protocol):
    """Signed transport supplied by an adapter; credentials never enter this module."""

    async def fetch_permissions(self, venue: Venue) -> Mapping[str, object]: ...

    async def fetch_fee_payload(
        self, venue: Venue, symbols: Sequence[str]
    ) -> Mapping[str, object]: ...


class CredentialPermissionError(PermissionError):
    pass


class PermissionVerifiedFeeApi:
    """Fetch account fees only after the venue proves withdrawal is unavailable."""

    def __init__(self, api: PrivateVenueApi, registry: SymbolRegistry) -> None:
        self._api = api
        self._registry = registry

    async def fetch(self, venue: Venue, symbols: Sequence[str]) -> Mapping[str, FeeQuote]:
        permissions = await self._api.fetch_permissions(venue)
        if venue is Venue.COINBASE:
            verify_coinbase_key_permissions(permissions)
        else:
            verify_kraken_key_permissions(permissions)
        payload = await self._api.fetch_fee_payload(venue, symbols)
        if venue is Venue.COINBASE:
            return parse_coinbase_fee(payload, symbols)
        return parse_kraken_fees(payload, self._registry)


class DailyFeeLookup:
    """Refresh account-specific fee rates from the API at least once per UTC day."""

    def __init__(
        self,
        api: FeeApi,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._api = api
        self._clock = clock
        self._cache: dict[tuple[Venue, str], FeeQuote] = {}
        self._refreshed_at: dict[Venue, datetime] = {}

    async def get(self, venue: Venue, symbols: Sequence[str]) -> Mapping[str, FeeQuote]:
        now = self._clock()
        last = self._refreshed_at.get(venue)
        missing = any((venue, symbol) not in self._cache for symbol in symbols)
        if last is None or now - last >= timedelta(days=1) or missing:
            fresh = await self._api.fetch(venue, symbols)
            if set(symbols) - set(fresh):
                raise ValueError("fee API omitted requested symbols")
            for symbol, quote in fresh.items():
                if quote.maker_rate < 0 or quote.taker_rate < 0:
                    raise ValueError("fee API returned a negative fee rate")
                self._cache[(venue, symbol)] = quote
            self._refreshed_at[venue] = now
        return {symbol: self._cache[(venue, symbol)] for symbol in symbols}


def parse_coinbase_fee(
    payload: Mapping[str, object], symbols: Sequence[str]
) -> dict[str, FeeQuote]:
    tier = payload.get("fee_tier")
    if not isinstance(tier, Mapping):
        raise ValueError("Coinbase fee response has no fee_tier")
    now = datetime.now(UTC)
    quote = FeeQuote(
        maker_rate=_decimal(tier["maker_fee_rate"]),
        taker_rate=_decimal(tier["taker_fee_rate"]),
        retrieved_at=now,
    )
    return {symbol: quote for symbol in symbols}


def parse_kraken_fees(
    payload: Mapping[str, object], registry: SymbolRegistry
) -> dict[str, FeeQuote]:
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise ValueError("Kraken fee response has no result")
    taker = result.get("fees")
    maker = result.get("fees_maker")
    if not isinstance(taker, Mapping) or not isinstance(maker, Mapping):
        raise ValueError("Kraken fee response omitted maker/taker fees")
    now = datetime.now(UTC)
    output: dict[str, FeeQuote] = {}
    for native, details in taker.items():
        maker_details = maker.get(native)
        if not isinstance(details, Mapping) or not isinstance(maker_details, Mapping):
            continue
        canonical = registry.to_canonical(str(native), Venue.KRAKEN)
        output[canonical] = FeeQuote(
            maker_rate=_decimal(maker_details["fee"]) / Decimal("100"),
            taker_rate=_decimal(details["fee"]) / Decimal("100"),
            retrieved_at=now,
        )
    return output


def verify_coinbase_key_permissions(payload: Mapping[str, object]) -> None:
    """Require view/trade and reject Coinbase's deposit/withdrawal permission."""

    if payload.get("can_view") is not True or payload.get("can_trade") is not True:
        raise CredentialPermissionError("Coinbase key must have view and trade permission")
    if payload.get("can_transfer") is not False:
        raise CredentialPermissionError(
            "Coinbase key transfer/withdrawal permission is present or unverifiable"
        )


def verify_kraken_key_permissions(payload: Mapping[str, object]) -> None:
    """Reject every Kraken permission capable of withdrawing or adding an address."""

    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise CredentialPermissionError("Kraken key permissions are unverifiable")
    permissions = result.get("permissions")
    if not isinstance(permissions, list) or not all(
        isinstance(permission, str) for permission in permissions
    ):
        raise CredentialPermissionError("Kraken key permissions are unverifiable")
    permission_set = set(permissions)
    if "modify-trades" not in permission_set:
        raise CredentialPermissionError("Kraken key must have modify-trades permission")
    forbidden = permission_set & {
        "withdraw-funds",
        "add-withdraw-address",
        "update-withdraw-address",
    }
    if forbidden:
        raise CredentialPermissionError(
            f"Kraken key has forbidden withdrawal permissions: {sorted(forbidden)}"
        )


def _decimal(value: object) -> Decimal:
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        raise ValueError("metadata Decimal must be finite")
    return parsed
