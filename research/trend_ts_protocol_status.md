# Trend strategy protocol status

Status: **COMPLETE — AUTHENTICATED-FEE IS PASSED; ONE-SHOT OOS CONSUMED**

The exact strategy, backtest engine, perturbation-grid generator, heatmap writer, and durable
one-shot holdout guard are implemented. Coinbase BTC-USD and ETH-USD daily history contains 3,698
aligned completed observations from 2016-05-18 through 2026-07-04.

Coinbase's authenticated transaction-summary endpoint returned a maker fee of `0.006`. The
in-sample protocol was rerun with that rate without changing strategy logic or parameters. The
baseline produced a +453.7700% net return, 1.209 Sharpe, and -27.4944% maximum drawdown. All 24
one-at-a-time ±50% perturbations remained profitable, so the robustness requirement passed.

After explicit user approval on 2026-07-05, the protected OOS command was executed exactly once.
The reserved period ran from 2024-06-25 through 2026-07-04 and produced:

- Result: **PROFITABLE**
- Net return: +1.1400%
- Sharpe: 0.098
- Maximum drawdown: -14.8415%
- Turnover: 17.8461
- Fee drag: 89.81% of gross

`trend_ts_oos_consumed.json` and `trend_ts_oos_signoff.md` now record the consumption timestamp,
data hash, result hash, metrics, and sign-off. The runner will refuse another OOS execution.

The OOS period is permanently consumed. No further strategy iteration, parameter selection,
filtering, or tuning against this period is permitted.

The previously exposed Coinbase key was confirmed revoked by the user on 2026-07-05. Local
credentials remain in ignored `.env` only.
