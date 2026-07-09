# Crypto paper deployment acceptance status

Implementation date: 2026-07-05
Last updated: 2026-07-09 (DRY_RUN clock restarted after two feed bugs fixed and Kraken
authenticated onboarding completed — supersedes all earlier restarts)

Deployed to `trading-bot-01` (DigitalOcean droplet, 165.22.196.24), previously hosting the retired
Hyperliquid bot (stopped, disabled, archived to
`/opt/archive/Hyperliquid-Whale-Tracker-20260708-033052`). `trading-data-recorder.service` and
`trading-crypto-paper.service` are both `active`, `TRADING_EXECUTION_MODE=DRY_RUN` confirmed.

## 2026-07-09 journal entry — clock restart

**This restart supersedes all earlier DRY_RUN restarts** (2026-07-08 05:13:21 UTC and
2026-07-08 07:54:49 UTC are both superseded; do not use either as the gate start).

**Two feed bugs fixed this session:**
1. Coinbase `level2` websocket subscription was missing JWT authentication — Coinbase silently
   accepted the subscription but never delivered `l2_data` (`bc9824bf`, `fix(data): authenticate
   Coinbase level2 websocket, quiet diagnostic logging`).
2. `_listen()` read `product_id` from individual update objects instead of the parent event —
   always `None`, silently discarding every book update even after JWT auth started working
   (`08cac02b`, `fix(coinbase): move product_id lookup to event level in l2_data parser`).

A third, related fix landed the same session but is not one of the above two: Coinbase's REST
candle parser didn't drop the still-forming last candle, asymmetric with Kraken's parser
(`1f8ac322`, `fix(coinbase): drop uncommitted last candle in REST parser`).

**Kraken authenticated onboarding completed:** trade-enabled, non-withdrawal key verified via a
standalone probe (not wired into any deployed service — see `TODO.md` for the deferred
integration work). All four checks passed: adapter instantiation, `verify_permissions()` guard
(trade permission present, withdrawal absent), persistent nonce continuity confirmed across
genuinely separate processes, authenticated `Balance` endpoint call succeeded.

**Row-count evidence (Step 2, pre-restart verification):** 63 Coinbase `top_of_book` rows landed
over a 20-minute observation window (21 rows each for BTC-USD/ETH-USD/SOL-USD, one per minute),
last row 13.7s old at query time. Coinbase health transitioned `FAILED → HEALTHY` and stayed there
for the full window. Kraken had zero health transitions during the same window (continuously
`HEALTHY`, unaffected).

**New clock start:** `trading-data-recorder.service` active since **2026-07-09 04:29:45 UTC**;
`trading-crypto-paper.service` active since **2026-07-09 04:29:50 UTC**. Use **04:29:50 UTC** (the
later, conservative timestamp) as the DRY_RUN gate start, consistent with prior convention.

| Gate | Status | Required evidence |
|---|---|---|
| 48 continuous hours DRY_RUN | **IN PROGRESS — started 2026-07-09 04:29:50 UTC** | systemd active interval, heartbeat continuity, zero unhandled failures — NOT YET COMPLETE, do not treat as passed before 2026-07-11 04:29:50 UTC with no interruption |
| Deliberate mid-cycle kill and recovery | **NOT YET PERFORMED ON SERVER** | Ubuntu kill drill (`systemctl kill --signal=KILL trading-crypto-paper`) plus matching adapter/order/portfolio state — automated test coverage only so far |
| PAPER start | **PENDING** | Human changes only `TRADING_EXECUTION_MODE=DRY_RUN` to `PAPER` after DRY_RUN sign-off |
| Daily live-vs-backtest output | **IMPLEMENTED; SERVER EVIDENCE PENDING** | Daily JSON under `/var/lib/trading-bot/reports` — no full day has elapsed yet |
| Eight-week PAPER minimum | **PENDING** | Eight continuous weeks after the actual PAPER start timestamp |

## CFM funding-carry GO/NO-GO date — recalculated

**Original estimate: 2026-09-04** (assumed clean funding collection starting 2026-07-06 + 60 days).

**Revised: 2026-09-06** (actual first funding row `2026-07-08T06:00:00 UTC` + 60 days).

**Why it moved:** Not because of the two websocket bugs above — CFM funding collection uses
`CoinbaseRestClient.fetch_funding()`'s REST `rest_token()` auth, a completely separate,
already-working code path from the `level2` websocket subscription. Queried the funding store
directly: 69 rows, perfectly continuous hourly from `2026-07-08T06:00:00 UTC` through the query
time, zero gaps anywhere in that range, including before either websocket fix was deployed. The
date moved because the recorder's first successful start didn't happen until 2026-07-08 05:13 UTC
(two days after the original 07-06 assumption) — blocked by an unrelated, earlier-fixed systemd
`EnvironmentFile=` credential-parsing issue (see the 2026-07-08 journal entry above), not by
anything fixed today.

The original 2026-09-01 LIVE-discussion estimate assumed PAPER starting 2026-07-07; since DRY_RUN
itself did not start until 2026-07-08 (and has now restarted again on 2026-07-09), that date no
longer holds. The earliest possible LIVE discussion date is 56 days after the actual uninterrupted
PAPER start, to be set once PAPER actually begins. This is a discussion gate only; the agent must
never enable LIVE.
