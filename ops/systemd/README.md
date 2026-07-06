# Market-data recorder deployment

Spot recording uses public market-data endpoints. Deployment is currently blocked for the CFM
funding stream: the live CDE endpoint requires a DCC API key although its endpoint example omits
authentication, and Coinbase documents no permission-inspection endpoint for DCC keys. The
recorder refuses to accept that unverifiable credential, as required by the repository security
policy. Do not enable this unit until Coinbase provides a way to verify that transfer/withdrawal
permission is absent (or clarifies that DCC keys categorically cannot possess it).

1. Install the repository and virtual environment at `/opt/trading-bot`.
2. Create the `tradingbot` system user and `/var/lib/trading-bot` owned by that user.
3. Copy `data-recorder.env.example` to `/etc/trading-bot/data-recorder.env` and verify the
   current Coinbase CFM contract symbols from the official contract/product metadata.
4. Copy `trading-data-recorder.service` to `/etc/systemd/system/`, run
   `systemctl daemon-reload`, then `systemctl enable --now trading-data-recorder`.
5. Confirm `status/funding-recorder.json` advances hourly and inspect the daily quality report.

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
