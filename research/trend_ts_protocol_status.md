# Trend strategy protocol status

Status: **IN-SAMPLE COMPLETE WITH PROVISIONAL FEE; OOS BLOCKED AND UNCONSUMED**

The exact strategy, backtest engine, perturbation-grid generator, heatmap writer, and durable
one-shot holdout guard are implemented. Coinbase BTC-USD and ETH-USD daily history was backfilled
from the official Exchange candles endpoint on 2026-07-05. The aligned dataset contains 3,698
completed daily observations from 2016-05-18 through 2026-07-04.

The in-sample evaluation was completed using a maker fee of `0.004`. This value was entered
manually as a provisional assumption; it was **not retrieved or verified through the Coinbase
API**. The committed in-sample report and perturbation artifacts reflect that provisional rate.
All 24 one-at-a-time ±50% perturbations were profitable, but this does not waive fee verification.

Before the protected OOS run, create a new read-only Coinbase API key with View permission and
query the authenticated transaction-summary endpoint for the actual account-tier
`maker_fee_rate`. If the verified rate differs from `0.004`, rerun and review the in-sample
protocol without `--consume-oos` first. Strategy logic and parameters must remain unchanged.

The previously exposed Coinbase API key was confirmed revoked by the user on 2026-07-05.

The most recent 20% holdout has **not** been evaluated or consumed. Neither
`trend_ts_oos_consumed.json` nor `trend_ts_oos_signoff.md` exists. Do not run the following command
without explicit user approval after fee verification:

```bash
python -m research.trend_walkforward --data-root var/data --output-root research \
  --maker-fee '<API_VERIFIED_DECIMAL>' --consume-oos
```

The command refuses to run the holdout again after atomically creating its consumption marker.
