"""Opt-in probes: RUN_COINBASE_INTEGRATION=1 with a read-only CDP key.

These tests never submit, edit, cancel, transfer, or withdraw. A read-only key is
expected to fail the adapter's trade permission guard; that failure is asserted.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from core.state import StateStore
from crypto.adapters.coinbase import CdpJwtAuth, CoinbaseAdapter
from crypto.normalize import CredentialPermissionError

pytestmark = pytest.mark.live_coinbase


def _enabled() -> bool:
    return os.getenv("RUN_COINBASE_INTEGRATION") == "1"


@pytest.mark.skipif(not _enabled(), reason="set RUN_COINBASE_INTEGRATION=1")
def test_live_read_only_balances_specs_stream_and_guard(tmp_path) -> None:
    async def exercise() -> None:
        adapter = CoinbaseAdapter(CdpJwtAuth.from_env(), StateStore(tmp_path / "state.db"))
        try:
            balances = await adapter.get_balances()
            specs = await adapter.get_product_specs()
            assert balances
            assert any(spec.symbol == "BTC-USD" for spec in specs)
            stream = adapter.stream_user_events(["BTC-USD"])
            event = await asyncio.wait_for(anext(stream), timeout=20)
            assert event.source == "websocket"
            await stream.aclose()
            with pytest.raises(CredentialPermissionError):
                await adapter.verify_permissions()
        finally:
            await adapter.close()

    asyncio.run(exercise())
