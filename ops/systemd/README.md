# Market-data recorder deployment

Spot recording uses public market-data endpoints. The CFM funding stream uses the public
`api.exchange.fairx.net/rest/funding-rate` endpoint (no credentials required for the data fetch).
The permission gate calls `api.coinbase.com/api/v3/brokerage/key_permissions` at recorder startup
using a **read-only CDP API key** (requires `can_view=true`, refuses if `can_transfer` is true).
Recorder startup fails closed if the key is absent, misconfigured, or has transfer permissions.

Before enabling the recorder:

1. Create a CDP API key with **view permission only** (no trade, no transfer).
   If the key uses Ed25519 (the current Coinbase default), `CdpJwtAuth` supports it natively.
2. Install the repository and virtual environment at `/opt/trading-bot`.
3. Create the `tradingbot` system user and `/var/lib/trading-bot` owned by that user.
4. Copy `data-recorder.env.example` to `/etc/trading-bot/data-recorder.env` (owner `root`, mode
   `0600`). Set `COINBASE_API_KEY` and `COINBASE_API_SECRET`. Verify CFM contract symbols.
5. Test the permission gate manually before enabling the unit:
   ```
   COINBASE_API_KEY=... COINBASE_API_SECRET=... python -m data.recorder backfill
   ```
   Confirm it completes and `status/backfill-complete.json` is written.
6. Copy `trading-data-recorder.service` to `/etc/systemd/system/`, run
   `systemctl daemon-reload`, then `systemctl enable --now trading-data-recorder`.
7. Confirm `status/funding-recorder.json` advances hourly and inspect the daily quality report.

Disk free space is checked every five minutes. Crossing the configured threshold writes an
append-only alert, logs a critical systemd-journal event, and exits nonzero so systemd records and
restarts the failure. The initial REST backfill is guarded by an atomic completion marker.

## Crypto DRY_RUN/PAPER engine

The unit files follow the systemd service/timer behavior documented at
https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html and
https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html (checked 2026-07-05).

1. Copy `crypto-paper.env.example` to `/etc/trading-bot/crypto-paper.env`, owner `root`, mode
   `0600`. Populate fee rates from authenticated venue APIs and add Telegram credentials. Never
   put secrets in a unit file or repository file.
2. Leave `TRADING_EXECUTION_MODE=DRY_RUN`. The engine rejects `LIVE` unconditionally.
3. Copy `trading-crypto-paper.service`, both `trading-crypto-*.timer` files, and their matching
   oneshot services to `/etc/systemd/system/`.
4. Run `systemctl daemon-reload`, then enable the paper engine and timers. The engine recovers and
   reconciles persistent paper/order state before every start. `Restart=on-failure` handles an
   abrupt process death.
5. Observe 48 continuous hours with advancing heartbeats, daily cycle records, no uncaught
   failures, and a daily diff JSON. Perform a deliberate `systemctl kill --signal=KILL
   trading-crypto-paper`, allow systemd to restart it, and verify positions/open orders against
   the SQLite paper state.
6. Only after the 48-hour evidence is signed off may a human change the mode to `PAPER` and restart
   the service. PAPER must then run uninterrupted for at least eight weeks.

The daily strategy cycle is scheduled internally for 00:05 UTC. The quality timer runs at 00:12
UTC and the live-vs-backtest diff timer at 00:15 UTC. Timer `Persistent=true` causes a missed
oneshot to run after the server returns. Telegram alerts cover position changes, risk vetoes,
venue health transitions, data-quality failures, and excess model divergence.
