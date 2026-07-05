from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from core.events import Bar, OrderType
from core.execution_mode import ExecutionMode, LiveGateConfig
from core.risk import AssetClass, RiskConfig, RiskManager, Venue
from engines.backtest_engine import BacktestEngine, DailyMarketEvent
from engines.fees import load_fee_schedule
from ops.journal import AppendOnlyJournal
from strategies.buy_and_hold import BuyAndHoldParams, buy_and_hold

D = Decimal
ROOT = Path(__file__).parents[2]
CENT = D("0.01")


def make_risk_manager(tmp_path: Path) -> RiskManager:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"live_enabled": False}), encoding="utf-8")
    return RiskManager(
        ExecutionMode.PAPER,
        RiskConfig(
            live_gate=LiveGateConfig(
                live_enabled=False,
                arm_file=tmp_path / "absent-arm.live",
                arm_sha256="0" * 64,
            ),
            instrument_position_caps={"BTC-USD": D("10")},
            strategy_allocation_caps={"buy_and_hold": D("10000")},
            venue_capital_caps={"kraken": D("10000")},
            staleness_thresholds={AssetClass.CRYPTO: timedelta(seconds=1)},
            config_path=config_path,
            max_order_notional_fraction=D("0.50"),
        ),
        AppendOnlyJournal(tmp_path / "risk.jsonl"),
        environ={},
    )


def market_event(day: int, close: str, *, bid: str, ask: str) -> DailyMarketEvent:
    price = D(close)
    timestamp = datetime(2026, 1, day, 21, 0, tzinfo=UTC)
    return DailyMarketEvent(
        bar=Bar(
            symbol="BTC-USD",
            timestamp=timestamp,
            open=price,
            high=price + D("2"),
            low=price - D("2"),
            close=price,
            volume=D("100"),
        ),
        bid=D(bid),
        ask=D(ask),
        venue=Venue("kraken"),
        asset_class=AssetClass.CRYPTO,
    )


def test_buy_and_hold_matches_hand_computed_spreadsheet_to_cent(tmp_path: Path) -> None:
    engine = BacktestEngine(
        initial_equity=D("1000"),
        strategy=buy_and_hold,
        strategy_params=BuyAndHoldParams(symbol="BTC-USD", quantity=D("2")),
        risk_manager=make_risk_manager(tmp_path),
        fee_schedules={"kraken": load_fee_schedule(ROOT / "config" / "fees" / "kraken.yaml")},
        slippage_rate=D("0.001"),
    )
    result = engine.run(
        [
            market_event(2, "100", bid="99", ask="101"),
            market_event(5, "110", bid="109", ask="111"),
            market_event(6, "105", bid="104", ask="106"),
        ]
    )

    with (ROOT / "tests" / "fixtures" / "buy_and_hold_expected.csv").open(
        newline="", encoding="utf-8"
    ) as stream:
        expected = list(csv.DictReader(stream))
    actual_cents = [
        point.equity.quantize(CENT, rounding=ROUND_HALF_UP) for point in result.equity_curve
    ]
    expected_cents = [D(row["equity"]).quantize(CENT) for row in expected]
    assert actual_cents == expected_cents == [D("996.99"), D("1016.99"), D("1006.99")]
    assert result.ending_cash == D("796.989192")
    assert result.ending_positions == {"BTC-USD": D("2")}
    assert len(result.fills) == 1
    assert result.fills[0].fill.price == D("101.101")
    assert result.fills[0].commission == D("0.808808")
    assert result.fills[0].slippage == D("0.202")


def test_limit_requires_trade_through_not_a_touch(tmp_path: Path) -> None:
    def run(low: Decimal, suffix: str) -> int:
        engine = BacktestEngine(
            initial_equity=D("1000"),
            strategy=buy_and_hold,
            strategy_params=BuyAndHoldParams(symbol="BTC-USD", quantity=D("1")),
            risk_manager=make_risk_manager(tmp_path / suffix),
            fee_schedules={"kraken": load_fee_schedule(ROOT / "config" / "fees" / "kraken.yaml")},
            slippage_rate=D("0.001"),
            default_order_type=OrderType.LIMIT,
        )
        event = market_event(2, "100", bid="99", ask="101")
        event = DailyMarketEvent(
            bar=Bar(
                symbol=event.bar.symbol,
                timestamp=event.bar.timestamp,
                open=event.bar.open,
                high=event.bar.high,
                low=low,
                close=event.bar.close,
                volume=event.bar.volume,
            ),
            bid=event.bid,
            ask=event.ask,
            venue=event.venue,
            asset_class=event.asset_class,
        )
        return len(engine.run([event]).fills)

    assert run(D("100"), "touch") == 0
    assert run(D("99.99"), "through") == 1
