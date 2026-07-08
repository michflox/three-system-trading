# Three-system automated trading platform

This repository contains the shared deterministic core and the fixed package layout for the
three-system trading platform. Strategies are pure functions, all orders pass through one shared
risk manager, and backtest and live engines use identical strategy and risk code.

## LIVE triple gate

LIVE mode is denied by default. `core.execution_mode.is_live_permitted()` returns `True` only when
all three independent gates agree:

1. The process environment contains `TRADING_LIVE=1` (the value is exact and case-sensitive).
2. The loaded configuration has `live_enabled = true`.
3. The configured `arm.live` file exists, is a regular file, contains exactly one valid
   64-character hexadecimal SHA-256 capability value (surrounding whitespace is ignored), and
   that value matches the SHA-256 reference in configuration.

Missing, malformed, unreadable, or mismatched arm files fail closed. Neither this repository nor
the bot creates the arm file, changes `live_enabled`, or sets `TRADING_LIVE`; arming LIVE is an
explicit human-only operation. The comparison is constant-time, and no arm value is logged.

## Risk rules and safety coverage

Every order reaches `RiskManager.approve()` and every veto is durably appended to the operational
JSONL journal. Exposure caps and the notional limit permit equality and veto values above the cap;
loss and staleness thresholds fire at equality. A quote exactly 2% from the last price is still
"within 2%" and is accepted, while any greater deviation is vetoed.

| Ordered risk rule | Safety test |
| --- | --- |
| Execution-mode gate | `test_execution_mode_gate_is_first_and_fail_closed` |
| Decimal input integrity; positive finite price and volume | `test_nan_zero_or_negative_quote_price_or_volume_always_vetoes`, `test_nan_zero_or_negative_order_price_always_vetoes` |
| Per-instrument position cap | `test_position_cap_allows_equality_and_vetoes_above_it` |
| Per-strategy allocated-notional cap | `test_strategy_cap_allows_equality_and_vetoes_above_it` |
| Per-venue allocated-capital cap | `test_venue_cap_allows_equality_and_vetoes_above_it` |
| Daily -2% flatten-and-halt | `test_daily_loss_halts_at_exactly_negative_two_percent` |
| Weekly -5% flatten-and-halt | `test_weekly_loss_halts_at_exactly_negative_five_percent` |
| -15% high-water-mark kill switch and config rewrite | `test_hwm_kill_switch_fires_at_exactly_negative_fifteen_percent` |
| Multi-day daily -> weekly -> kill escalation | `test_multi_day_loss_cascade_escalates_daily_weekly_then_kill` |
| Order notional versus equity | `test_order_notional_allows_equality_and_vetoes_above_it` |
| Order price within 2% of last quote | `test_price_deviation_allows_two_percent_and_vetoes_above_it` |
| Duplicate `client_order_id` suppression | `test_duplicate_client_id_is_suppressed_after_first_approval` |
| Per-asset-class stale-data veto and exit exception | `test_staleness_veto_fires_at_exact_boundary`, `test_valid_exit_is_allowed_during_loss_halt_and_with_stale_data` |
| Exit must reduce risk without reversing the position | `test_exit_flag_cannot_hide_a_position_reversal` |
| Kill-switch flatten window | `test_kill_switch_live_window_allows_only_risk_reducing_exit` |
| Append-only veto journal | `test_every_veto_is_appended_as_jsonl_with_reason` |

## Development

Requires Python 3.11 or newer.

```bash
python -m pip install -e '.[dev]'
pytest
mypy core/
ruff check .
```

## Crypto paper deployment gate

The crypto trend service starts in `DRY_RUN`, evaluates the pure strategy at 00:05 UTC, sends every
candidate order through the shared `RiskManager`, routes only approved orders, and submits only in
`PAPER`. The deployment command rejects `LIVE` unconditionally; LIVE remains human-only and is not
enabled by this repository.

Operational promotion requires 48 continuous clean hours of DRY_RUN followed by at least eight
uninterrupted weeks of PAPER. Ubuntu deployment began 2026-07-08 (DRY_RUN clock started
05:13:21 UTC), superseding the original planning assumption of DRY_RUN beginning 2026-07-05 and
PAPER beginning 2026-07-07 — the earliest permitted date for any LIVE discussion is **56 days
after the actual uninterrupted PAPER start**, not yet set since PAPER has not started. See
`ops/paper_deployment_status.md`; local automated tests do not substitute for server-duration
evidence.

## IBKR micro-futures paper boundary

`adapters.ibkr` supports MES, MNQ, M2K, MYM, MGC, MHG, MCL, MNG, M6E, M6B, and M6A through
`ib_async` and IB Gateway. It is paper-only: configuration and Gateway account discovery fail
closed if a non-`DU` account is present. Every submitted order accepts only an `Approved` result
from the shared `RiskManager`; fixed-window market fallback needs a separate approval and confirmed
limit cancellation.

Catastrophe exits are exact 3xATR(20) broker-resident GTC stop orders. A daily systemd timer checks
that every supported open futures position has matching stop direction and quantity. Continuous
series use explicit CME-derived contract calendars, roll three to five business days before the
earlier of first notice or expiry, and backward Panama adjustment. See `ops/systemd/README.md` for
the paper-Gateway verification procedure. No LIVE futures operation is enabled here.
