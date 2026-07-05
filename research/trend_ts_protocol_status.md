# Trend strategy protocol status

Status: **BLOCKED BEFORE OUT-OF-SAMPLE CONSUMPTION**

The exact strategy, backtest engine, perturbation-grid generator, heatmap writer, and durable
one-shot holdout guard are implemented. Coinbase BTC-USD and ETH-USD daily history was backfilled
from the official Exchange candles endpoint on 2026-07-05. The aligned dataset contains 3,698
completed daily observations from 2016-05-18 through 2026-07-04.

The research run has not started because the Coinbase fee configuration intentionally contains
no static maker rate. Coinbase maker fees are account-tier-specific, and no authenticated
transaction-summary response or explicit API-derived maker rate is available in this workspace.
Running with zero, a guessed rate, or a public example would violate the requirement that fees are
always applied and never invented.

The most recent 20% holdout has **not** been evaluated or consumed. Neither
`trend_ts_oos_consumed.json` nor `trend_ts_oos_signoff.md` exists. Once an API-derived maker rate is
supplied, run exactly once:

```bash
python -m research.trend_walkforward --data-root var/data --output-root research \
  --maker-fee '<API_DERIVED_DECIMAL>' --consume-oos
```

The command refuses to run the holdout again after atomically creating its consumption marker.
