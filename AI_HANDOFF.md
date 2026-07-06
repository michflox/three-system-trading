# AI Handoff Document

**Generated:** 2026-07-05  
**Branch:** main  
**Status:** In-sample evaluation complete — out-of-sample holdout unconsumed

---

## Repository State

The codebase is clean. All source modules, tests, and configuration are committed. The working tree contains only the four research artifacts added by the in-sample run (staged for the next commit). The OOS protection is intact.

---

## Completed Work

| Area | Status |
|---|---|
| Core risk engine (execution-mode gate, 8-rule ordered policy, veto journal) | Complete |
| Crash-safe SQLite state store | Complete |
| Volatility-target position sizing | Complete |
| Coinbase and Kraken broker adapters (JWT/HMAC auth, idempotent crash recovery) | Complete |
| Paper broker with limit fill simulation | Complete |
| Capability-aware execution router with failover and migration | Complete |
| Venue health state machine | Complete |
| Persistent order lifecycle manager with MakerFirstPolicy | Complete |
| Parquet market-data store (atomic writes, partitioned) | Complete |
| Coinbase + Kraken WebSocket and REST data recorder | Complete |
| Daily data quality reporter | Complete |
| Trend TS strategy (3-EWMA crossover + breakout, pure function) | Complete |
| Trend backtest engine (maker-limit fill, vol-target sizing) | Complete |
| Fee schedule loader (refuses null rates) | Complete |
| In-sample research protocol with OOS one-shot guard | Complete |
| Coinbase BTC-USD / ETH-USD daily data backfill (3,698 bars) | Complete |
| In-sample evaluation run | **Complete — see results below** |
| Out-of-sample evaluation | **Pending — not yet consumed** |

---

## In-Sample Results (maker fee 0.004, 80% of data through 2024-06-24)

| Metric | Value |
|---|---|
| Period | 2016-09-25 → 2024-06-24 (7.75 years) |
| Net return | +516.87% |
| CAGR | ~26.5% p.a. |
| Sharpe ratio | 1.281 |
| Maximum drawdown | -25.48% |
| Ending equity | $616,867 (started $100,000) |
| Number of trades | 1,062 |
| Total fees | $87,981 |
| Fee drag | 14.55% of gross profit |
| Turnover | 55.4× |
| ±50% perturbation test | **PASS (24/24 profitable)** |

**Worst perturbation:** `breakout_decay −50%` at +355% — still strongly positive.  
**Most robust axis:** EWMA pairs (`fast_*`, `slow_*`) — all within ±15% of baseline return.

---

## Generated Artifacts

| File | Description |
|---|---|
| `research/trend_ts_backtest_report.md` | Human-readable IS summary with all metrics |
| `research/trend_ts_equity_curve.csv` | Daily net and gross equity (2,831 rows) |
| `research/trend_ts_perturbation.csv` | 24 perturbation runs with net returns |
| `research/trend_ts_perturbation_heatmap.svg` | Colour-coded robustness heatmap |
| `research/trend_ts_protocol_status.md` | Protocol status (existing, pre-run) |

OOS marker files (`trend_ts_oos_consumed.json`, `trend_ts_oos_signoff.md`) do **not** exist — the holdout is intact.

---

## Remaining Blockers

1. **Maker fee precision:** `0.004` was used as a placeholder. The authenticated Coinbase `/api/v3/brokerage/transaction_summary` endpoint returns the exact account-tier rate. A new CDP API key with View permission is required (the previously used key `34d55202-…` returned 401). Re-run the IS protocol with the precise rate if it differs from 0.004 before consuming OOS.

2. **Security — revoke exposed credentials:** The file `get_coinbase_fee.py` (now deleted) contained plaintext credentials (`3e21d67c-3f6a-410c-b8af-fb2ab4e84949`). These were never committed to git, but the file existed untracked on disk. Revoke this API key in your Coinbase account immediately.

3. **Kraken fee schedule expiry:** `config/fees/kraken.yaml` hardcodes the base-tier rate with a note that Kraken announced changes effective 2026-07-09. That date has passed — re-verify before using Kraken for paper or live trading.

---

## Recommended Next Step

The in-sample evidence supports proceeding to OOS evaluation:

- CAGR of 26.5%, Sharpe 1.28, and a max drawdown of −25% are credible crypto-trend results over a nearly 8-year window.
- The ±50% perturbation gate passed 24/24, with no run below +350%.
- The fee drag (14.55%) is accounted for at the placeholder rate; confirm the exact rate first if possible.

When ready, run the one-shot holdout **exactly once**:

```bash
python -m research.trend_walkforward \
  --data-root var/data \
  --output-root research \
  --maker-fee <CONFIRMED_RATE> \
  --consume-oos
```

This command creates `research/trend_ts_oos_consumed.json` and `research/trend_ts_oos_signoff.md` atomically. After that, no further iteration against the OOS period is permitted.

---

## Live Trading Infrastructure Status

The full live-trading stack (adapters, router, health monitor, order manager, risk manager) is implemented and tested. A wiring entry point (`ops/live_engine.py` or similar `main.py`) does not yet exist. That is the next engineering task after OOS sign-off.
