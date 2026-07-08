# Crypto paper deployment acceptance status

Implementation date: 2026-07-05
Last updated: 2026-07-08 (Ubuntu deployment executed; DRY_RUN clock started)

Deployed to `trading-bot-01` (DigitalOcean droplet, 165.22.196.24), previously hosting the retired
Hyperliquid bot (stopped, disabled, archived to
`/opt/archive/Hyperliquid-Whale-Tracker-20260708-033052`). `trading-data-recorder.service` and
`trading-crypto-paper.service` are both `active`, `TRADING_EXECUTION_MODE=DRY_RUN` confirmed.

| Gate | Status | Required evidence |
|---|---|---|
| 48 continuous hours DRY_RUN | **IN PROGRESS — started 2026-07-08 05:13:21 UTC** | systemd active interval, heartbeat continuity, zero unhandled failures — NOT YET COMPLETE, do not treat as passed before 2026-07-10 05:13:21 UTC with no interruption |
| Deliberate mid-cycle kill and recovery | **NOT YET PERFORMED ON SERVER** | Ubuntu kill drill (`systemctl kill --signal=KILL trading-crypto-paper`) plus matching adapter/order/portfolio state — automated test coverage only so far |
| PAPER start | **PENDING** | Human changes only `TRADING_EXECUTION_MODE=DRY_RUN` to `PAPER` after DRY_RUN sign-off |
| Daily live-vs-backtest output | **IMPLEMENTED; SERVER EVIDENCE PENDING** | Daily JSON under `/var/lib/trading-bot/reports` — no full day has elapsed yet |
| Eight-week PAPER minimum | **PENDING** | Eight continuous weeks after the actual PAPER start timestamp |

The recorder's `ActiveEnterTimestamp` (05:13:21 UTC) is used as the conservative DRY_RUN start
rather than the paper engine's own timestamp (05:10:58 UTC), since the recorder failed and
restarted 14 times during initial deployment (systemd `EnvironmentFile=` credential-parsing issue,
fixed — see `ops/systemd/README.md`) before both services were stably active together.

The original 2026-09-01 LIVE-discussion estimate assumed PAPER starting 2026-07-07; since DRY_RUN
itself did not start until 2026-07-08, that date no longer holds. The earliest possible LIVE
discussion date is 56 days after the actual uninterrupted PAPER start, to be set once PAPER
actually begins. This is a discussion gate only; the agent must never enable LIVE.
