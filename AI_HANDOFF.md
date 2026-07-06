# AI Handoff Document

**Generated:** 2026-07-05  
**Branch:** main  
**Status:** In-sample evaluation complete with provisional/manual fee — out-of-sample holdout unconsumed

---

## Repository State

All source modules, tests, configuration, and in-sample research artifacts are committed on
`main`. The OOS protection is intact.

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

## In-Sample Results (provisional/manual maker fee 0.004, 80% of data through 2024-06-24)

**Fee clarification:** `0.004` was supplied manually for the in-sample run. It was not retrieved
or verified through the Coinbase API. The committed backtest report's wording that the rate was
"supplied from API" is historical output from that provisional run and must not be interpreted as
API verification.

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

1. **Maker fee verification:** `0.004` was used as a provisional/manual value and has not been
   verified by the Coinbase API. The authenticated Coinbase
   `/api/v3/brokerage/transaction_summary` endpoint returns the account-tier rate. A new read-only
   CDP API key with View permission is required. Re-run the IS protocol if the verified rate differs
   from `0.004`; do not consume OOS before this check.

2. **Security — old credential revoked:** The user confirmed on 2026-07-05 that the previously
   exposed Coinbase API key was revoked. Do not reuse it.

3. **Kraken fee schedule expiry:** `config/fees/kraken.yaml` hardcodes the base-tier rate with a note that Kraken announced changes effective 2026-07-09. That date has passed — re-verify before using Kraken for paper or live trading.

---

## Recommended Next Step

The in-sample evidence supports considering OOS evaluation after the maker fee is API-verified:

- CAGR of 26.5%, Sharpe 1.28, and a max drawdown of −25% are credible crypto-trend results over a nearly 8-year window.
- The ±50% perturbation gate passed 24/24, with no run below +350%.
- The fee drag (14.55%) is accounted for at the provisional/manual `0.004` rate. API verification
  is required before OOS approval.

Only after fee verification and explicit user approval, run the one-shot holdout **exactly once**:

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
