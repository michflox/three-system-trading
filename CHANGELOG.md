# Changelog

Concise technical summary of completed milestones, newest first. This file starts 2026-07-08;
earlier history is in `git log` and `AI_HANDOFF.md`.

## 2026-07-08 — Ubuntu deployment executed, DRY_RUN clock started

- Retired the Hyperliquid whale-tracker deployment on `trading-bot-01` (DigitalOcean,
  165.22.196.24): stopped and disabled `hyperliquid-bot.service`, archived
  `/opt/bots/Hyperliquid-Whale-Tracker` to
  `/opt/archive/Hyperliquid-Whale-Tracker-20260708-033052`, confirmed no Hyperliquid processes
  remain.
- Ran `ops/deploy.sh` on a fresh `/opt/trading-bot` install: system user, directories, venv,
  systemd units, `TRADING_EXECUTION_MODE` locked to `DRY_RUN`.
- Found and fixed a deployment-level bug: systemd's `EnvironmentFile=` parser (systemd ≥ 253,
  Ubuntu 24.04's systemd 255) silently strips `\n` escapes used to flatten a multi-line PEM onto
  one line, corrupting `COINBASE_API_SECRET` on every real service start even though manual
  `source`-based smoke testing succeeded (it doesn't apply the same escaping). Fixed by storing
  the PEM as a double-quoted, real-multi-line value instead; verified via `systemd-run` under the
  unit's actual sandboxed mechanism. No code change — config/deployment only.
- Corrected a stale note in `AI_HANDOFF.md` claiming the Coinbase adapter only supports ECDSA PEM
  keys; direct code inspection confirmed `CdpJwtAuth` supports both EC and Ed25519 PEM natively.
  It does not support a raw (non-PEM) base64 key format.
- `trading-data-recorder.service` and `trading-crypto-paper.service` both `active` on the server.
  48-hour DRY_RUN clock started 2026-07-08 05:13:21 UTC (recorder's stable start, used as the
  conservative gate timestamp over the paper engine's slightly earlier 05:10:58 UTC). Not yet
  complete — kill drill and PAPER switch still pending.
- Updated `ops/systemd/README.md`, `ops/systemd/data-recorder.env.example`, and `ops/deploy.sh`'s
  checklist to document the systemd escaping gotcha and the corrected Ed25519 support, so it
  isn't rediscovered on the next redeploy.

## Earlier history (pre-changelog)

- `49a52d4` feat(ibkr): adapter + continuous contracts (paper integration unverified)
- `7e094ae` Raise WebSocket max_size to 8MB for Coinbase l2_data
- `e4a48fe` Switch CFM funding to Advanced Trade product endpoint
- `68bff34` Add python-dotenv for local .env credential loading
- `3597a86` Fix funding-rate endpoint and recorder startup permission gate
- `26064d7` Enable Coinbase permission-gated funding fetch
- `adafb8a` Add Ubuntu deployment bootstrap script
- `d6b6261` Implement crypto paper deployment
- `20c1da7` Complete trend strategy OOS protocol
- `3bfc5f0` Clarify provisional trend research fee
- `6e1e1ae` Add in-sample research artifacts and handoff document
- `511020f` Recover Codex project before Claude continuation
