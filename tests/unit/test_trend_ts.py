from datetime import UTC, datetime, timedelta
from decimal import Decimal

from core.events import Bar, SignalAction
from engines.trend_backtest import TrendBacktestConfig, TrendBacktestEngine
from research.trend_walkforward import parameter_grid
from strategies.trend_ts import DEFAULT_TREND_PARAMS, trend_score, trend_ts
from strategies.types import MarketState


def bars(closes: list[Decimal], symbol: str = "BTC-USD") -> tuple[Bar, ...]:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    return tuple(
        Bar(
            symbol,
            start + timedelta(days=index),
            close,
            close + Decimal("2"),
            max(Decimal("0.01"), close - Decimal("2")),
            close,
            Decimal("100"),
        )
        for index, close in enumerate(closes)
    )


def test_exact_uptrend_and_downtrend_clip_to_unit_bounds() -> None:
    rising = tuple(Decimal(100 + index) for index in range(160))
    falling = tuple(Decimal(300 - index) for index in range(160))
    assert trend_score(rising, DEFAULT_TREND_PARAMS) == Decimal("1")
    assert trend_score(falling, DEFAULT_TREND_PARAMS) == Decimal("-1")


def test_flat_series_is_hold_and_strategy_is_pure() -> None:
    history = bars([Decimal("100")] * 160)
    state = MarketState(history[-1], Decimal("0"), history)
    first = trend_ts(state, DEFAULT_TREND_PARAMS)
    second = trend_ts(state, DEFAULT_TREND_PARAMS)
    assert first == second
    assert first[0].action is SignalAction.HOLD
    assert first[0].quantity is None


def test_insufficient_warmup_is_zero() -> None:
    assert (
        trend_score(tuple(Decimal(100 + index) for index in range(127)), DEFAULT_TREND_PARAMS) == 0
    )


def test_every_parameter_has_minus_base_plus_fifty_grid_cells() -> None:
    grid = parameter_grid(DEFAULT_TREND_PARAMS)
    assert len(grid) == 24
    names = {name for name, _, _ in grid}
    assert names == {
        "fast_1",
        "slow_1",
        "fast_2",
        "slow_2",
        "fast_3",
        "slow_3",
        "breakout_days",
        "breakout_decay",
    }
    assert {label for _, label, _ in grid} == {"-50%", "base", "+50%"}


def test_trend_backtest_uses_trade_through_fees_and_allocation_cap() -> None:
    dates = 260
    btc = bars([Decimal(100 + index) for index in range(dates)], "BTC-USD")
    eth = bars([Decimal(50 + index) for index in range(dates)], "ETH-USD")
    engine = TrendBacktestEngine(TrendBacktestConfig(Decimal("100000"), Decimal("0.001")))
    result = engine.run({"BTC-USD": btc, "ETH-USD": eth}, DEFAULT_TREND_PARAMS)
    assert result.trades
    assert result.total_fees > 0
    assert result.turnover > 0
    assert all(trade.price > 0 and trade.quantity > 0 and trade.fee > 0 for trade in result.trades)
