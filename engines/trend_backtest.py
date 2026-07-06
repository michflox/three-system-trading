"""Daily multi-asset target-position backtest for the exact trend strategy."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from math import sqrt
from statistics import fmean, pstdev

from core.events import Bar, Side
from core.sizing import SQRT_TRADING_DAYS, ewma_volatility
from strategies.trend_ts import TrendParams, trend_score

ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class TrendBacktestConfig:
    initial_equity: Decimal
    maker_fee_rate: Decimal
    target_vol_contribution: Decimal = Decimal("0.10")
    per_instrument_leverage_cap: Decimal = Decimal("2")
    allocation_cap: Decimal = Decimal("0.25")
    rebalance_buffer: Decimal = Decimal("0.25")

    def __post_init__(self) -> None:
        positive = (
            self.initial_equity,
            self.maker_fee_rate,
            self.target_vol_contribution,
            self.per_instrument_leverage_cap,
            self.allocation_cap,
            self.rebalance_buffer,
        )
        if any(not value.is_finite() or value <= ZERO for value in positive):
            raise ValueError("backtest configuration values must be positive finite Decimals")


@dataclass(frozen=True, slots=True)
class TrendEquityPoint:
    timestamp: datetime
    equity: Decimal
    gross_equity: Decimal


@dataclass(frozen=True, slots=True)
class TrendTrade:
    timestamp: datetime
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    fee: Decimal


@dataclass(frozen=True, slots=True)
class TrendBacktestResult:
    equity_curve: tuple[TrendEquityPoint, ...]
    trades: tuple[TrendTrade, ...]
    ending_equity: Decimal
    net_return: Decimal
    sharpe: float
    max_drawdown: Decimal
    turnover: Decimal
    fee_drag_percent_of_gross: Decimal
    total_fees: Decimal


def trend_target_quantity(
    *,
    equity: Decimal,
    score: Decimal,
    sigma: Decimal,
    price: Decimal,
    config: TrendBacktestConfig,
) -> Decimal:
    """Return the exact target quantity shared by trend backtest and paper execution."""

    if any(not value.is_finite() or value <= ZERO for value in (equity, sigma, price)):
        return ZERO
    if not score.is_finite() or score < Decimal("-1") or score > Decimal("1"):
        raise ValueError("trend score must be a finite Decimal in [-1, 1]")
    raw = (
        equity
        * config.target_vol_contribution
        * score
        / (sigma * price * SQRT_TRADING_DAYS)
    )
    maximum_notional = min(
        equity * config.per_instrument_leverage_cap,
        equity * config.allocation_cap,
    )
    maximum_quantity = maximum_notional / price
    target = max(-maximum_quantity, min(maximum_quantity, raw))
    return target.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)


class TrendBacktestEngine:
    def __init__(self, config: TrendBacktestConfig) -> None:
        self.config = config

    def run(
        self,
        bars: Mapping[str, Sequence[Bar]],
        params: TrendParams,
        *,
        trade_start_index: int | None = None,
    ) -> TrendBacktestResult:
        symbols = tuple(sorted(bars))
        if not symbols:
            raise ValueError("at least one symbol is required")
        length = len(bars[symbols[0]])
        if length < 2 or any(len(bars[symbol]) != length for symbol in symbols):
            raise ValueError("bar histories must be aligned and equal length")
        for index in range(length):
            timestamps = {bars[symbol][index].timestamp for symbol in symbols}
            if len(timestamps) != 1:
                raise ValueError("bar histories must share timestamps")
        start = trade_start_index or max(
            params.slow_1, params.slow_2, params.slow_3, params.breakout_days + 1, 33
        )
        if start >= length:
            raise ValueError("insufficient bars after warmup")

        cash = self.config.initial_equity
        gross_cash = cash
        positions = {symbol: ZERO for symbol in symbols}
        curve: list[TrendEquityPoint] = []
        trades: list[TrendTrade] = []
        total_notional = ZERO
        total_fees = ZERO

        for index in range(start, length):
            previous_prices = {symbol: bars[symbol][index - 1].close for symbol in symbols}
            equity_before = cash + sum(
                positions[symbol] * previous_prices[symbol] for symbol in symbols
            )
            for symbol in symbols:
                history = tuple(bar.close for bar in bars[symbol][:index])
                score = trend_score(history, params)
                sigma = ewma_volatility(history[-33:], span=32)
                target = trend_target_quantity(
                    equity=equity_before,
                    score=score,
                    sigma=sigma,
                    price=previous_prices[symbol],
                    config=self.config,
                )
                current = positions[symbol]
                if (
                    current != ZERO
                    and abs(target - current) / abs(current) < self.config.rebalance_buffer
                ):
                    continue
                delta = target - current
                if delta == ZERO:
                    continue
                today = bars[symbol][index]
                limit = previous_prices[symbol]
                side = Side.BUY if delta > ZERO else Side.SELL
                traded_through = today.low < limit if side is Side.BUY else today.high > limit
                if not traded_through:
                    continue
                quantity = abs(delta)
                notional = quantity * limit
                fee = notional * self.config.maker_fee_rate
                cash += -notional - fee if side is Side.BUY else notional - fee
                gross_cash += -notional if side is Side.BUY else notional
                positions[symbol] = target
                total_notional += notional
                total_fees += fee
                trades.append(TrendTrade(today.timestamp, symbol, side, quantity, limit, fee))
            close_equity = cash + sum(
                positions[symbol] * bars[symbol][index].close for symbol in symbols
            )
            gross_equity = gross_cash + sum(
                positions[symbol] * bars[symbol][index].close for symbol in symbols
            )
            curve.append(
                TrendEquityPoint(bars[symbols[0]][index].timestamp, close_equity, gross_equity)
            )

        equities = [point.equity for point in curve]
        returns = [float(equities[i] / equities[i - 1] - 1) for i in range(1, len(equities))]
        volatility = pstdev(returns) if len(returns) > 1 else 0.0
        sharpe = 0.0 if volatility == 0 else fmean(returns) / volatility * sqrt(252)
        peak = equities[0]
        max_drawdown = ZERO
        for equity in equities:
            peak = max(peak, equity)
            max_drawdown = min(max_drawdown, equity / peak - 1)
        ending = equities[-1]
        gross_profit = curve[-1].gross_equity - self.config.initial_equity
        fee_drag = ZERO if gross_profit == ZERO else total_fees / abs(gross_profit) * Decimal("100")
        average_equity = sum(equities, ZERO) / Decimal(len(equities))
        turnover = total_notional / average_equity
        return TrendBacktestResult(
            tuple(curve),
            tuple(trades),
            ending,
            ending / self.config.initial_equity - 1,
            sharpe,
            max_drawdown,
            turnover,
            fee_drag,
            total_fees,
        )
