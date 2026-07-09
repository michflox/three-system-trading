# TODO

Tracked follow-ups that are deliberately not being executed yet. Each entry
records why it was deferred and what it depends on.

## Wire real venue adapters through paper engine for startup credential verification

**Recorded:** 2026-07-09

**Current state:** `trading-crypto-paper.service` uses `PaperAdapter` for both venues, whose
`verify_permissions()` is a no-op (`crypto/adapters/paper.py:94-95`). The real
`CoinbaseAdapter`/`KrakenAdapter` credential guards (signing, trade/withdrawal permission
checks, persistent nonce) only run in integration tests
(`tests/integration/test_coinbase_adapter.py`, `tests/integration/test_kraken_adapter.py`,
and the opt-in `RUN_KRAKEN_INTEGRATION=1` / live-readonly probes) — never in the deployed
DRY_RUN/PAPER pipeline. This was discovered while trying to verify a newly onboarded
authenticated Kraken key against "startup" behavior that doesn't currently exist.

**Why it matters:** Before PAPER → LIVE promotion, the paper engine must instantiate the
real adapters at startup for credential verification (auth signing works, trade permission
present, withdrawal permission absent, nonce persists across restarts), even while continuing
to route fills through the simulated `PaperAdapter`. Right now nothing in the deployed system
would catch a credential regression until an actual LIVE cutover attempt.

**Scope:** `engines/crypto_paper_engine.py` startup path (construct real adapters alongside
`PaperAdapter`, call `verify_permissions()` before entering the main loop); corresponding
systemd unit env wiring for both `crypto-paper.env` (Kraken) and `data-recorder.env`
(Coinbase, already has real credentials for market data — the trading-permission guard is
the new part). Do not conflate with LIVE mode — this is a startup-gate wiring change only,
governed by the existing three-gate LIVE lock in `core/execution_mode.py`.

**Same follow-up applies to the IBKR paper engine** when Phase 2 reaches paper trading.

**Do not execute without an explicit scoped prompt.**
