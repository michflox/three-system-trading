import asyncio
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import httpx

from crypto.capabilities import (
    COINBASE_PUBLIC_BUDGET,
    KRAKEN_STARTER_BUDGET,
    observe_capabilities,
    verify_capabilities,
    verify_live_at_startup,
)
from crypto.normalize import rules_from_coinbase, rules_from_kraken
from crypto.symbols import SymbolDialect, SymbolRegistry, Venue

ROOT = Path(__file__).parents[2]
METADATA = ROOT / "tests" / "fixtures" / "metadata"


def snapshots() -> tuple[dict[str, object], dict[str, object]]:
    coinbase = json.loads(
        (METADATA / "coinbase-products-2026-07-04.json").read_text(encoding="utf-8")
    )
    kraken = json.loads(
        (METADATA / "kraken-asset-pairs-2026-07-04.json").read_text(encoding="utf-8")
    )
    return coinbase, kraken


def test_symbol_round_trips_against_live_metadata_snapshots() -> None:
    coinbase, kraken = snapshots()
    registry = SymbolRegistry.from_metadata(coinbase, kraken)
    for canonical in ("BTC-USD", "ETH-USD", "SOL-USD"):
        for venue in Venue:
            native = registry.to_venue(canonical, venue)
            assert registry.to_canonical(native, venue) == canonical

    assert registry.to_venue("BTC-USD", Venue.KRAKEN) == "BTC/USD"
    assert registry.to_venue("BTC-USD", Venue.KRAKEN, SymbolDialect.LEGACY_REST) == "XBTUSD"
    assert registry.to_venue("BTC-USD", Venue.KRAKEN, SymbolDialect.LEGACY_WEBSOCKET) == "XBT/USD"
    assert registry.to_canonical("XBTUSD", Venue.KRAKEN) == "BTC-USD"
    assert registry.to_canonical("XBT/USD", Venue.KRAKEN) == "BTC-USD"


def test_normalization_rules_are_built_from_metadata_not_constants() -> None:
    coinbase, kraken = snapshots()
    registry = SymbolRegistry.from_metadata(coinbase, kraken)
    coinbase_rules = rules_from_coinbase(coinbase)
    kraken_rules = rules_from_kraken(kraken, registry)
    assert coinbase_rules["BTC-USD"].tick_size == Decimal("0.01")
    assert coinbase_rules["BTC-USD"].minimum_notional == Decimal("1")
    assert kraken_rules["BTC-USD"].tick_size == Decimal("0.1")
    assert kraken_rules["BTC-USD"].step_size == Decimal("0.00000001")
    assert kraken_rules["SOL-USD"].minimum_size == Decimal("0.06")


def test_deliberately_misdeclared_capability_degrades_venue() -> None:
    coinbase, _ = snapshots()
    observed = observe_capabilities(Venue.COINBASE, coinbase)
    declared = replace(observed, perpetual_futures=True)
    verification = verify_capabilities(Venue.COINBASE, declared, coinbase)
    assert verification.degraded
    assert any("perpetual_futures" in mismatch for mismatch in verification.mismatches)


def test_startup_verifier_consumes_live_endpoint_shape() -> None:
    coinbase, _ = snapshots()
    observed = observe_capabilities(Venue.COINBASE, coinbase)

    async def exercise() -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json=coinbase))
        async with httpx.AsyncClient(transport=transport) as client:
            result = await verify_live_at_startup(
                Venue.COINBASE,
                observed,
                client=client,
            )
        assert not result.degraded

    asyncio.run(exercise())


def test_rate_budgets_are_exactly_seventy_percent_of_documented_limits() -> None:
    assert COINBASE_PUBLIC_BUDGET.capacity == Decimal("10.50")
    assert COINBASE_PUBLIC_BUDGET.refill_per_second == Decimal("7.00")
    assert KRAKEN_STARTER_BUDGET.capacity == Decimal("10.50")
    assert KRAKEN_STARTER_BUDGET.refill_per_second == Decimal("0.2310")
