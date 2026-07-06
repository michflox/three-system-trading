# Crypto paper deployment acceptance status

Implementation date: 2026-07-05

| Gate | Status | Required evidence |
|---|---|---|
| 48 continuous hours DRY_RUN | **PENDING UBUNTU DEPLOYMENT** | systemd active interval, heartbeat continuity, zero unhandled failures |
| Deliberate mid-cycle kill and recovery | **AUTOMATED TEST ONLY** | Ubuntu kill drill plus matching adapter/order/portfolio state |
| PAPER start | **PENDING** | Human changes only `TRADING_EXECUTION_MODE=DRY_RUN` to `PAPER` after DRY_RUN sign-off |
| Daily live-vs-backtest output | **IMPLEMENTED; SERVER EVIDENCE PENDING** | Daily JSON under `/var/lib/trading-bot/reports` |
| Eight-week PAPER minimum | **PENDING** | Eight continuous weeks after the actual PAPER start timestamp |

No LIVE discussion is allowed before 2026-09-01, the earliest possible date assuming the 48-hour
DRY_RUN begins on 2026-07-05 and PAPER begins on 2026-07-07. If PAPER begins later or is
interrupted, the allowed discussion date moves to 56 days after the actual uninterrupted PAPER
start. This is a discussion gate only; the agent must never enable LIVE.
