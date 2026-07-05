"""One-shot trend research protocol and artifact generator.

The held-out 20% is guarded by a durable consumption marker. This command never
supplies a default Coinbase fee because the rate is account-tier-specific.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from dataclasses import fields, replace
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, cast

from core.events import Bar
from data.store import ParquetStore
from engines.trend_backtest import (
    TrendBacktestConfig,
    TrendBacktestEngine,
    TrendBacktestResult,
)
from strategies.trend_ts import DEFAULT_TREND_PARAMS, TrendParams

SYMBOLS = ("BTC-USD", "ETH-USD")
PARAMETER_NAMES = tuple(field.name for field in fields(TrendParams))


def load_daily_bars(data_root: Path) -> dict[str, tuple[Bar, ...]]:
    table = ParquetStore(data_root).read("ohlcv")
    rows = table.to_pylist()
    by_symbol: dict[str, dict[datetime, Bar]] = {symbol: {} for symbol in SYMBOLS}
    for row in rows:
        symbol = str(row["symbol"])
        if row["venue"] != "coinbase" or row["interval"] != "1d" or symbol not in by_symbol:
            continue
        timestamp = row["timestamp"].astimezone(UTC)
        by_symbol[symbol][timestamp] = Bar(
            symbol,
            timestamp,
            Decimal(str(row["open"])),
            Decimal(str(row["high"])),
            Decimal(str(row["low"])),
            Decimal(str(row["close"])),
            Decimal(str(row["volume"])),
        )
    common = sorted(set.intersection(*(set(values) for values in by_symbol.values())))
    if len(common) < 300:
        raise RuntimeError(f"need at least 300 aligned Coinbase daily bars; found {len(common)}")
    return {
        symbol: tuple(by_symbol[symbol][timestamp] for timestamp in common) for symbol in SYMBOLS
    }


def parameter_grid(base: TrendParams) -> tuple[tuple[str, str, TrendParams], ...]:
    """One-at-a-time ±50% grid; production defaults never change."""

    grid: list[tuple[str, str, TrendParams]] = []
    for name in PARAMETER_NAMES:
        original = getattr(base, name)
        for label, factor in (
            ("-50%", Decimal("0.5")),
            ("base", Decimal("1")),
            ("+50%", Decimal("1.5")),
        ):
            if isinstance(original, int):
                value: int | Decimal = max(
                    1,
                    int((Decimal(original) * factor).to_integral_value(rounding=ROUND_HALF_UP)),
                )
            else:
                value = original * factor
            grid.append((name, label, replace(base, **cast(Any, {name: value}))))
    return tuple(grid)


def run_protocol(
    data_root: Path,
    output_root: Path,
    maker_fee: Decimal,
    *,
    consume_oos: bool,
) -> None:
    if not maker_fee.is_finite() or maker_fee <= 0:
        raise ValueError("maker_fee must be an explicit positive API-derived Decimal")
    bars = load_daily_bars(data_root)
    length = len(next(iter(bars.values())))
    split = int(Decimal(length) * Decimal("0.8"))
    in_sample = {symbol: values[:split] for symbol, values in bars.items()}
    engine = TrendBacktestEngine(TrendBacktestConfig(Decimal("100000"), maker_fee))
    baseline = engine.run(in_sample, DEFAULT_TREND_PARAMS)
    perturbations: list[tuple[str, str, TrendBacktestResult]] = []
    for name, label, params in parameter_grid(DEFAULT_TREND_PARAMS):
        perturbations.append((name, label, engine.run(in_sample, params)))
    grid_profitable = all(result.net_return > 0 for _, _, result in perturbations)

    output_root.mkdir(parents=True, exist_ok=True)
    _write_equity(output_root / "trend_ts_equity_curve.csv", baseline)
    _write_grid(output_root / "trend_ts_perturbation.csv", perturbations)
    _write_heatmap(output_root / "trend_ts_perturbation_heatmap.svg", perturbations)
    (output_root / "trend_ts_backtest_report.md").write_text(
        _report("In-sample", baseline, bars, split, maker_fee, grid_profitable),
        encoding="utf-8",
    )
    if not consume_oos:
        return

    marker = output_root / "trend_ts_oos_consumed.json"
    note = output_root / "trend_ts_oos_signoff.md"
    if marker.exists() or note.exists():
        raise RuntimeError("one-shot out-of-sample set has already been consumed")
    warmup = max(
        DEFAULT_TREND_PARAMS.slow_1,
        DEFAULT_TREND_PARAMS.slow_2,
        DEFAULT_TREND_PARAMS.slow_3,
        DEFAULT_TREND_PARAMS.breakout_days + 1,
        33,
    )
    oos_start = max(0, split - warmup)
    oos_bars = {symbol: values[oos_start:] for symbol, values in bars.items()}
    oos = engine.run(oos_bars, DEFAULT_TREND_PARAMS, trade_start_index=split - oos_start)
    data_hash = _data_hash(bars)
    signed_payload = {
        "consumed_at": datetime.now(UTC).isoformat(),
        "data_sha256": data_hash,
        "in_sample_end": bars[SYMBOLS[0]][split - 1].timestamp.isoformat(),
        "out_of_sample_start": bars[SYMBOLS[0]][split].timestamp.isoformat(),
        "out_of_sample_end": bars[SYMBOLS[0]][-1].timestamp.isoformat(),
        "maker_fee_rate": str(maker_fee),
        "net_return": str(oos.net_return),
        "sharpe": oos.sharpe,
        "max_drawdown": str(oos.max_drawdown),
    }
    signature = hashlib.sha256(
        json.dumps(signed_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    signed_payload["record_sha256"] = signature
    _atomic_text(marker, json.dumps(signed_payload, indent=2, sort_keys=True) + "\n")
    result_word = "PROFITABLE" if oos.net_return > 0 else "UNPROFITABLE"
    note_text = (
        "# Trend strategy one-shot out-of-sample sign-off\n\n"
        f"Result: **{result_word}**\n\n"
        f"- Net return: {oos.net_return:.4%}\n"
        f"- Sharpe: {oos.sharpe:.3f}\n"
        f"- Maximum drawdown: {oos.max_drawdown:.4%}\n"
        f"- Turnover: {oos.turnover:.4f}\n"
        f"- Fee drag: {oos.fee_drag_percent_of_gross:.2f}% of gross\n"
        f"- Data SHA-256: `{data_hash}`\n"
        f"- Record SHA-256: `{signature}`\n\n"
        "Signed-off-by: automated anti-overfitting protocol\n\n"
        "The most recent 20% holdout has been consumed exactly once. No further strategy "
        "iteration, parameter selection, or filtering against this period is permitted.\n"
    )
    _atomic_text(note, note_text)


def _report(
    label: str,
    result: TrendBacktestResult,
    bars: dict[str, tuple[Bar, ...]],
    split: int,
    maker_fee: Decimal,
    grid_profitable: bool,
) -> str:
    status = "PASS" if grid_profitable else "FAIL"
    return (
        f"# Trend strategy backtest report — {label}\n\n"
        f"Data: Coinbase daily BTC-USD and ETH-USD, through "
        f"{bars[SYMBOLS[0]][split - 1].timestamp.date().isoformat()}.\n\n"
        f"- Ending equity: ${result.ending_equity:,.2f}\n"
        f"- Net return: {result.net_return:.4%}\n"
        f"- Sharpe: {result.sharpe:.3f}\n"
        f"- Maximum drawdown: {result.max_drawdown:.4%}\n"
        f"- Turnover: {result.turnover:.4f}\n"
        f"- Total fees: ${result.total_fees:,.2f}\n"
        f"- Fee drag: {result.fee_drag_percent_of_gross:.2f}% of gross\n"
        f"- Coinbase maker fee supplied from API: {maker_fee}\n"
        f"- ±50% perturbation profitability requirement: **{status}**\n\n"
        "Sizing constraints: 10% volatility-target contribution, 2x per-instrument leverage "
        "cap, 25% allocation cap, and 25% rebalance buffer. Maker limits fill only on strict "
        "trade-through.\n"
    )


def _write_equity(path: Path, result: TrendBacktestResult) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("timestamp", "equity", "gross_equity"))
        writer.writerows(
            (point.timestamp.isoformat(), point.equity, point.gross_equity)
            for point in result.equity_curve
        )


def _write_grid(path: Path, rows: list[tuple[str, str, TrendBacktestResult]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("parameter", "perturbation", "net_return", "profitable"))
        writer.writerows(
            (name, label, result.net_return, result.net_return > 0) for name, label, result in rows
        )


def _write_heatmap(path: Path, rows: list[tuple[str, str, TrendBacktestResult]]) -> None:
    lookup = {(name, label): result.net_return for name, label, result in rows}
    labels = ("-50%", "base", "+50%")
    cell_w, cell_h, left, top = 120, 32, 180, 40
    values = list(lookup.values())
    scale = max(abs(min(values)), abs(max(values)), Decimal("0.0001"))
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{left + cell_w * 3 + 20}" '
        f'height="{top + cell_h * len(PARAMETER_NAMES) + 30}">',
        '<rect width="100%" height="100%" fill="white"/>',
    ]
    for column, label in enumerate(labels):
        parts.append(
            f'<text x="{left + column * cell_w + 10}" y="25" font-size="14">{label}</text>'
        )
    for row, name in enumerate(PARAMETER_NAMES):
        y = top + row * cell_h
        parts.append(f'<text x="5" y="{y + 21}" font-size="13">{name}</text>')
        for column, label in enumerate(labels):
            value = lookup[(name, label)]
            intensity_value = min(
                Decimal("220"), abs(value / scale) * Decimal("220")
            ).to_integral_value(rounding=ROUND_HALF_UP)
            intensity = int(str(intensity_value))
            color = (
                f"rgb({255 - intensity},255,{255 - intensity})"
                if value > 0
                else f"rgb(255,{255 - intensity},{255 - intensity})"
            )
            x = left + column * cell_w
            parts.append(
                f'<rect x="{x}" y="{y}" width="{cell_w - 2}" height="{cell_h - 2}" fill="{color}"/>'
            )
            parts.append(f'<text x="{x + 5}" y="{y + 21}" font-size="12">{value:.2%}</text>')
    parts.append("</svg>")
    path.write_text("\n".join(parts), encoding="utf-8")


def _data_hash(bars: dict[str, tuple[Bar, ...]]) -> str:
    digest = hashlib.sha256()
    for symbol in SYMBOLS:
        for bar in bars[symbol]:
            digest.update(
                f"{symbol}|{bar.timestamp.isoformat()}|{bar.open}|{bar.high}|{bar.low}|{bar.close}|{bar.volume}\n".encode()
            )
    return digest.hexdigest()


def _atomic_text(path: Path, value: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, default=Path("var/data"))
    parser.add_argument("--output-root", type=Path, default=Path("research"))
    parser.add_argument("--maker-fee", type=Decimal, required=True)
    parser.add_argument("--consume-oos", action="store_true")
    args = parser.parse_args()
    run_protocol(args.data_root, args.output_root, args.maker_fee, consume_oos=args.consume_oos)


if __name__ == "__main__":
    main()
