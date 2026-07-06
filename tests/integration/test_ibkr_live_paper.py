"""Opt-in read-only connectivity tests against the user's IB Gateway paper account."""

import asyncio
import json
import os
from pathlib import Path

import pytest

from adapters.ibkr import FUTURES_SPECS, IBKRAdapter, IBKRConfig
from core.state import StateStore


@pytest.mark.live_ibkr
def test_live_paper_account_qualifies_contracts_and_reads_account(tmp_path: Path) -> None:
    if os.environ.get("RUN_LIVE_IBKR_TESTS") != "1":
        pytest.skip("set RUN_LIVE_IBKR_TESTS=1 to test a running IB Gateway paper account")
    raw_months = os.environ.get("IBKR_TEST_CONTRACT_MONTHS")
    if raw_months is None:
        pytest.skip("IBKR_TEST_CONTRACT_MONTHS JSON is required to avoid stale contract guesses")
    months = json.loads(raw_months)
    assert set(months) == set(FUTURES_SPECS)

    async def exercise() -> None:
        adapter = IBKRAdapter(
            IBKRConfig.from_environment(),
            StateStore(tmp_path / "state.db"),
        )
        try:
            await adapter.connect()
            contracts = await adapter.qualify_contracts(months)
            values = await adapter.account_values()
            await adapter.positions()
        finally:
            await adapter.close()

        assert all(int(contract.conId) > 0 for contract in contracts.values())
        assert values

    asyncio.run(exercise())
