"""Opt-in probes: RUN_KRAKEN_INTEGRATION=1 with a non-withdrawal read key.

No order, cancel, deposit, transfer, or withdrawal endpoint is called.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from core.state import StateStore
from crypto.adapters.kraken import KrakenAdapter, KrakenSigner
from crypto.normalize import CredentialPermissionError
from crypto.symbols import SymbolRegistry, Venue, VenueSymbol

pytestmark = pytest.mark.live_kraken


@pytest.mark.skipif(
    os.getenv("RUN_KRAKEN_INTEGRATION") != "1",
    reason="set RUN_KRAKEN_INTEGRATION=1",
)
def test_live_read_key_balances_specs_stream_and_trade_guard(tmp_path) -> None:
    registry = SymbolRegistry(
        [
            VenueSymbol(
                "BTC-USD",
                Venue.KRAKEN,
                "BTC/USD",
                "BTC/USD",
                "XBTUSD",
                "XBT/USD",
            )
        ]
    )

    async def exercise() -> None:
        adapter = KrakenAdapter(
            KrakenSigner.from_env(), StateStore(tmp_path / "state.db"), registry
        )
        try:
            balances = await adapter.get_balances()
            specs = await adapter.get_product_specs()
            assert isinstance(balances, tuple)
            assert any(spec.symbol == "BTC-USD" for spec in specs)
            stream = adapter.stream_user_events()
            event = await asyncio.wait_for(anext(stream), timeout=20)
            assert event.source == "websocket"
            await stream.aclose()
            with pytest.raises(CredentialPermissionError):
                await adapter.verify_permissions()
        finally:
            await adapter.close()

    asyncio.run(exercise())
