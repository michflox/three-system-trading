# AI Handoff Document

**Generated:** 2026-07-05 (last updated 2026-07-08)
**Branch:** main  
**Status:** Prompt 1.6 complete; Prompt 1.7 deployed to Ubuntu, 48-hour DRY_RUN clock running (not yet complete)

## Repository State

The Trend TS implementation, authenticated-fee in-sample research artifacts, perturbation grid,
one-shot OOS consumption record, and OOS sign-off are complete. The OOS period is permanently
closed to further strategy iteration, parameter selection, or filtering.

## Completed Work

| Area | Status |
|---|---|
| Core risk, state, sizing, adapters, router, health, order manager, and data recorder | Complete |
| Trend TS pure strategy and backtest integration | Complete |
| Coinbase BTC-USD / ETH-USD daily data backfill | Complete |
| Coinbase account-tier maker fee verification | **Complete — 0.006** |
| In-sample evaluation and ±50% perturbation protocol | **Complete — PASS (24/24 profitable)** |
| One-shot out-of-sample evaluation | **Complete — consumed exactly once** |

## Authenticated-Fee In-Sample Results

Coinbase returned `maker_fee_rate=0.006`, `taker_fee_rate=0.012`, pricing tier `Intro 1` from the
authenticated transaction-summary endpoint. The IS protocol was rerun with the maker rate; no
strategy logic or parameters changed.

| Metric | Value |
|---|---|
| Period end | 2024-06-24 |
| Net return | +453.7700% |
| Sharpe ratio | 1.209 |
| Maximum drawdown | -27.4944% |
| Ending equity | $553,770.02 (started $100,000) |
| Total fees | $123,774.32 |
| Fee drag | 21.43% of gross |
| Turnover | 55.1301 |
| ±50% perturbation test | **PASS (24/24 profitable)** |

The weakest perturbation was `breakout_decay -50%`, with a positive net return of 295.59%.

## One-Shot OOS Result

The user explicitly authorized the protected run on 2026-07-05. It was executed once with the
authenticated maker fee of `0.006` against the reserved period from 2024-06-25 through 2026-07-04.

| Metric | Value |
|---|---|
| Result | **PROFITABLE** |
| Net return | +1.1400% |
| Sharpe ratio | 0.098 |
| Maximum drawdown | -14.8415% |
| Turnover | 17.8461 |
| Fee drag | 89.81% of gross |

The OOS result is positive but weak after fees. It is reported without tuning or reinterpretation.
No further strategy iteration, parameter selection, or filtering against this OOS period is
permitted.

## Research Artifacts

| File | Description |
|---|---|
| `research/trend_ts_backtest_report.md` | Authenticated-fee IS report |
| `research/trend_ts_equity_curve.csv` | IS daily net and gross equity |
| `research/trend_ts_perturbation.csv` | Authenticated-fee perturbation results |
| `research/trend_ts_perturbation_heatmap.svg` | Perturbation heatmap |
| `research/trend_ts_oos_consumed.json` | Immutable one-shot consumption metadata and hashes |
| `research/trend_ts_oos_signoff.md` | Signed-off OOS result and no-further-iteration rule |
| `research/trend_ts_protocol_status.md` | Final protocol status |

## Security and Operations Notes

- The user confirmed on 2026-07-05 that the previously exposed Coinbase key was revoked.
- Local credentials are stored only in ignored `.env`; never commit or log them.
- `CdpJwtAuth` (`crypto/adapters/coinbase.py`) accepts either an EC or Ed25519 PEM private key
  natively — confirmed by direct code inspection 2026-07-08, correcting an earlier note in this
  file that said Ed25519 needed separate adapter work. It does NOT support a raw (non-PEM) base64
  key; if a CDP key JSON gives only `id` + a non-PEM `privateKey`, that key format isn't usable
  as-is (see "Ubuntu deployment" section below).
- Never enable LIVE mode. LIVE activation remains a human-only action.
- Re-verify the Kraken fee schedule before any Kraken paper or live use.

## Next Engineering Step

Prompt 1.6 is closed. Do not rerun `--consume-oos` and do not use the consumed OOS period for
further Trend TS development.

Prompt 1.7 now includes the DRY_RUN/PAPER-only crypto engine, persistent restart reconciliation,
Telegram monitoring, systemd service/timers, and daily live-vs-backtest diff. Automated tests pass,
including a simulated mid-cycle death and restart with aggregate multi-venue reconciliation.

## Ubuntu deployment (2026-07-08)

Deployed to `trading-bot-01` (DigitalOcean, 165.22.196.24) — the server previously hosting the
Hyperliquid whale-tracker bot, which was retired first: stopped, disabled, and archived to
`/opt/archive/Hyperliquid-Whale-Tracker-20260708-033052` (confirmed no Hyperliquid processes
remain). `ops/deploy.sh` bootstrap ran clean on a fresh `/opt/trading-bot` install.

`trading-data-recorder.service` and `trading-crypto-paper.service` are both `active`,
`TRADING_EXECUTION_MODE=DRY_RUN` confirmed locked. See `ops/paper_deployment_status.md` for the
live gate table.

Deployment surfaced a real, reproducible bug, not a one-off paste error: systemd's
`EnvironmentFile=` parser (systemd ≥ 253, confirmed on Ubuntu 24.04's systemd 255) applies
shell-like backslash escaping and silently strips a `\n`-escaped single-line PEM secret before the
process ever sees it, corrupting `COINBASE_API_SECRET` on every real service start — this passed
undetected through manual `source`-based testing, which doesn't apply the same escaping, so the
smoke test kept succeeding while the actual systemd unit kept failing (14 restart attempts before
diagnosis). Fixed at the deployment/config level only — store the PEM as a double-quoted,
real-multi-line value in `/etc/trading-bot/data-recorder.env` instead of a `\n`-escaped single
line — verified via `systemd-run` under the exact sandboxed mechanism the unit uses before
re-enabling it. No code, architecture, or trading logic was changed. `ops/systemd/README.md`,
`ops/systemd/data-recorder.env.example`, and `ops/deploy.sh`'s post-install checklist were updated
so this isn't rediscovered on the next redeploy.

Operational acceptance is still pending: 48 continuous clean DRY_RUN hours (**in progress**,
started 2026-07-08 05:13:21 UTC, not complete — do not treat as passed before roughly
2026-07-10 05:13 UTC with zero interruptions), the real systemd kill drill (not yet performed on
the server), then a human switch to PAPER. PAPER must remain uninterrupted for eight weeks. The
original 2026-09-01 LIVE-discussion estimate assumed PAPER starting 2026-07-07 and no longer
holds since DRY_RUN itself only started 2026-07-08; the earliest possible LIVE discussion date is
56 days after the actual uninterrupted PAPER start, once that start happens. The agent must
never enable LIVE.

## Prompt 2.1 status

The code boundary is implemented for the eleven requested IBKR micro futures: paper-only contract
qualification, approved limit/market/stop submission, persistent idempotency, positions/account
values, daily-restart reconnect handling, and exact broker-resident 3xATR(20) stop verification.
Deterministic CME-derived roll calendars, backward-Panama adjustment, and simulated position-roll
continuity are covered by tests. A systemd daily verification timer and paper environment template
are under `ops/systemd/`.

Local mocked integration and calendar tests pass. Operational acceptance remains pending until the
opt-in read-only integration test is run against a real IB Gateway paper session and its output is
recorded; the local suite correctly skips that test without explicit paper credentials and a
running Gateway. No orders are placed by that integration test, and no LIVE account is supported.
