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
