"""Canonical crypto symbols and metadata-derived venue aliases."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class Venue(StrEnum):
    COINBASE = "coinbase"
    KRAKEN = "kraken"


class SymbolDialect(StrEnum):
    DEFAULT = "default"
    REST = "rest"
    WEBSOCKET = "websocket"
    LEGACY_REST = "legacy_rest"
    LEGACY_WEBSOCKET = "legacy_websocket"


@dataclass(frozen=True, slots=True)
class VenueSymbol:
    canonical: str
    venue: Venue
    rest: str
    websocket: str
    legacy_rest: str | None = None
    legacy_websocket: str | None = None

    def native(self, dialect: SymbolDialect = SymbolDialect.DEFAULT) -> str:
        if dialect in {SymbolDialect.DEFAULT, SymbolDialect.REST}:
            return self.rest
        if dialect is SymbolDialect.WEBSOCKET:
            return self.websocket
        if dialect is SymbolDialect.LEGACY_REST and self.legacy_rest is not None:
            return self.legacy_rest
        if dialect is SymbolDialect.LEGACY_WEBSOCKET and self.legacy_websocket is not None:
            return self.legacy_websocket
        raise KeyError(f"{self.venue} has no {dialect} alias for {self.canonical}")


class SymbolRegistry:
    def __init__(self, symbols: list[VenueSymbol] | tuple[VenueSymbol, ...]) -> None:
        self._forward = {(item.venue, item.canonical): item for item in symbols}
        self._reverse: dict[tuple[Venue, str], str] = {}
        for item in symbols:
            aliases = {
                item.rest,
                item.websocket,
                item.legacy_rest,
                item.legacy_websocket,
            }
            for alias in aliases - {None}:
                assert alias is not None
                key = (item.venue, alias.upper())
                existing = self._reverse.get(key)
                if existing is not None and existing != item.canonical:
                    raise ValueError(f"ambiguous venue alias {item.venue}:{alias}")
                self._reverse[key] = item.canonical

    def to_venue(
        self,
        canonical: str,
        venue: Venue,
        dialect: SymbolDialect = SymbolDialect.DEFAULT,
    ) -> str:
        return self._forward[(venue, _canonicalize(canonical))].native(dialect)

    def to_canonical(self, native: str, venue: Venue) -> str:
        return self._reverse[(venue, native.upper())]

    @property
    def canonical_symbols(self) -> frozenset[str]:
        return frozenset(canonical for _, canonical in self._forward)

    @classmethod
    def from_metadata(
        cls,
        coinbase_payload: Mapping[str, object],
        kraken_payload: Mapping[str, object],
    ) -> SymbolRegistry:
        symbols: list[VenueSymbol] = []
        products = coinbase_payload.get("products")
        if not isinstance(products, list):
            raise ValueError("Coinbase metadata has no products list")
        for product in products:
            if not isinstance(product, Mapping) or product.get("product_type") != "SPOT":
                continue
            native = str(product["product_id"])
            canonical = _canonicalize(
                f"{product['base_currency_id']}-{product['quote_currency_id']}"
            )
            symbols.append(VenueSymbol(canonical, Venue.COINBASE, native, native))

        result = kraken_payload.get("result")
        if not isinstance(result, Mapping):
            raise ValueError("Kraken metadata has no result mapping")
        for display_name, details in result.items():
            if not isinstance(details, Mapping):
                continue
            canonical = _canonicalize(f"{details['base']}-{details['quote']}")
            websocket = _replace_xbt(str(display_name))
            symbols.append(
                VenueSymbol(
                    canonical=canonical,
                    venue=Venue.KRAKEN,
                    rest=websocket,
                    websocket=websocket,
                    legacy_rest=str(details.get("altname")) or None,
                    legacy_websocket=str(details.get("wsname")) or None,
                )
            )
        return cls(symbols)


def _canonicalize(value: str) -> str:
    normalized = _replace_xbt(value.strip().upper().replace("/", "-"))
    parts = normalized.split("-")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"invalid canonical symbol {value!r}")
    return f"{parts[0]}-{parts[1]}"


def _replace_xbt(value: str) -> str:
    return value.replace("XBT", "BTC")
