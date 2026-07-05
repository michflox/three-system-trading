from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from core.events import OrderRequest, OrderType, Side
from core.execution_mode import ExecutionMode, LiveGateConfig
from core.risk import (
    Approved,
    AssetClass,
    PortfolioSnapshot,
    Quote,
    RiskConfig,
    RiskManager,
    RiskReason,
    Venue,
    Vetoed,
)
from ops.journal import AppendOnlyJournal

NOW = datetime(2026, 7, 6, 15, 0, tzinfo=UTC)
D = Decimal
VENUE = Venue("coinbase")
ZERO = D("0")
ONE = D("1")
TWO = D("2")
TEN = D("10")
HUNDRED = D("100")
THOUSAND = D("1000")
TEN_THOUSAND = D("10000")
FIVE_SECONDS = timedelta(seconds=5)


def make_order(
    client_id: str = "order-1",
    *,
    quantity: Decimal = ONE,
    expected_price: Decimal | None = HUNDRED,
    side: Side = Side.BUY,
    is_exit: bool = False,
    created_at: datetime = NOW,
) -> OrderRequest:
    return OrderRequest(
        client_order_id=client_id,
        symbol="BTC-USD",
        side=side,
        quantity=quantity,
        order_type=OrderType.MARKET,
        created_at=created_at,
        strategy_id="momentum",
        expected_price=expected_price,
        is_exit=is_exit,
    )


def make_portfolio(
    *,
    equity: Decimal = THOUSAND,
    day_start: Decimal = THOUSAND,
    week_start: Decimal = THOUSAND,
    hwm: Decimal = THOUSAND,
    position: Decimal = ZERO,
    strategy_allocation: Decimal = ZERO,
    venue_allocation: Decimal = ZERO,
    quote_price: Decimal = HUNDRED,
    quote_volume: Decimal = TEN,
    quote_time: datetime = NOW,
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        equity=equity,
        day_start_equity=day_start,
        week_start_equity=week_start,
        high_water_mark=hwm,
        positions={"BTC-USD": position},
        strategy_allocations={"momentum": strategy_allocation},
        venue_allocations={"coinbase": venue_allocation},
        last_quotes={
            "BTC-USD": Quote(
                price=quote_price,
                volume=quote_volume,
                timestamp=quote_time,
                asset_class=AssetClass.CRYPTO,
            )
        },
    )


def make_manager(
    tmp_path: Path,
    *,
    mode: ExecutionMode = ExecutionMode.PAPER,
    live_enabled: bool = True,
    position_cap: Decimal = TEN,
    strategy_cap: Decimal = TEN_THOUSAND,
    venue_cap: Decimal = TEN_THOUSAND,
    notional_fraction: Decimal = TWO,
    stale_after: timedelta = FIVE_SECONDS,
) -> tuple[RiskManager, Path, Path]:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"live_enabled": live_enabled, "preserved": "value"}), encoding="utf-8"
    )
    arm_file = tmp_path / "arm.live"
    arm_file.write_text("a" * 64, encoding="ascii")
    journal_path = tmp_path / "risk.jsonl"
    config = RiskConfig(
        live_gate=LiveGateConfig(
            live_enabled=live_enabled,
            arm_file=arm_file,
            arm_sha256="a" * 64,
        ),
        instrument_position_caps={"BTC-USD": position_cap},
        strategy_allocation_caps={"momentum": strategy_cap},
        venue_capital_caps={"coinbase": venue_cap},
        staleness_thresholds={AssetClass.CRYPTO: stale_after},
        config_path=config_path,
        max_order_notional_fraction=notional_fraction,
    )
    manager = RiskManager(
        mode,
        config,
        AppendOnlyJournal(journal_path),
        environ={"TRADING_LIVE": "1"},
    )
    return manager, journal_path, config_path


def assert_veto(decision: Approved | Vetoed, reason: RiskReason) -> Vetoed:
    assert isinstance(decision, Vetoed)
    assert decision.reason is reason
    return decision


def test_execution_mode_gate_is_first_and_fail_closed(tmp_path: Path) -> None:
    manager, _, _ = make_manager(tmp_path, mode=ExecutionMode.LIVE, live_enabled=False)
    decision = manager.approve(make_order(), make_portfolio(quote_price=D("NaN")), VENUE)
    assert_veto(decision, RiskReason.EXECUTION_MODE_GATE)


def test_position_cap_allows_equality_and_vetoes_above_it(tmp_path: Path) -> None:
    manager, _, _ = make_manager(tmp_path)
    assert isinstance(
        manager.approve(make_order("equal", quantity=D("10")), make_portfolio(), VENUE),
        Approved,
    )
    decision = manager.approve(
        make_order("above", quantity=D("10.00000001")), make_portfolio(), VENUE
    )
    assert_veto(decision, RiskReason.POSITION_CAP)


def test_strategy_cap_allows_equality_and_vetoes_above_it(tmp_path: Path) -> None:
    manager, _, _ = make_manager(tmp_path)
    equality = make_portfolio(strategy_allocation=D("9900"))
    assert isinstance(manager.approve(make_order("equal"), equality, VENUE), Approved)
    above = make_portfolio(strategy_allocation=D("9900.01"))
    assert_veto(
        manager.approve(make_order("above"), above, VENUE),
        RiskReason.STRATEGY_ALLOCATION_CAP,
    )


def test_venue_cap_allows_equality_and_vetoes_above_it(tmp_path: Path) -> None:
    manager, _, _ = make_manager(tmp_path)
    equality = make_portfolio(venue_allocation=D("9900"))
    assert isinstance(manager.approve(make_order("equal"), equality, VENUE), Approved)
    above = make_portfolio(venue_allocation=D("9900.01"))
    assert_veto(manager.approve(make_order("above"), above, VENUE), RiskReason.VENUE_CAPITAL_CAP)


def test_daily_loss_halts_at_exactly_negative_two_percent(tmp_path: Path) -> None:
    manager, _, _ = make_manager(tmp_path)
    decision = assert_veto(
        manager.approve(make_order(), make_portfolio(equity=D("980"), day_start=D("1000")), VENUE),
        RiskReason.DAILY_LOSS_HALT,
    )
    assert decision.flatten_and_halt


def test_weekly_loss_halts_at_exactly_negative_five_percent(tmp_path: Path) -> None:
    manager, _, _ = make_manager(tmp_path)
    decision = assert_veto(
        manager.approve(
            make_order(),
            make_portfolio(equity=D("950"), day_start=D("950"), week_start=D("1000")),
            VENUE,
        ),
        RiskReason.WEEKLY_LOSS_HALT,
    )
    assert decision.flatten_and_halt


def test_hwm_kill_switch_fires_at_exactly_negative_fifteen_percent(tmp_path: Path) -> None:
    manager, _, config_path = make_manager(tmp_path)
    decision = assert_veto(
        manager.approve(
            make_order(),
            make_portfolio(equity=D("850"), day_start=D("850"), week_start=D("850"), hwm=D("1000")),
            VENUE,
        ),
        RiskReason.HWM_KILL_SWITCH,
    )
    assert decision.flatten_and_halt and decision.kill_switch
    rewritten = json.loads(config_path.read_text(encoding="utf-8"))
    assert rewritten == {"live_enabled": False, "preserved": "value"}


def test_multi_day_loss_cascade_escalates_daily_weekly_then_kill(tmp_path: Path) -> None:
    manager, _, config_path = make_manager(tmp_path)
    day = manager.approve(
        make_order("day"), make_portfolio(equity=D("980"), day_start=D("1000")), VENUE
    )
    assert_veto(day, RiskReason.DAILY_LOSS_HALT)
    week = manager.approve(
        make_order("week"),
        make_portfolio(equity=D("950"), day_start=D("960"), week_start=D("1000")),
        VENUE,
    )
    assert_veto(week, RiskReason.WEEKLY_LOSS_HALT)
    kill = manager.approve(
        make_order("kill"),
        make_portfolio(equity=D("850"), day_start=D("855"), week_start=D("890"), hwm=D("1000")),
        VENUE,
    )
    assert_veto(kill, RiskReason.HWM_KILL_SWITCH)
    assert json.loads(config_path.read_text(encoding="utf-8"))["live_enabled"] is False


def test_order_notional_allows_equality_and_vetoes_above_it(tmp_path: Path) -> None:
    manager, _, _ = make_manager(tmp_path, notional_fraction=D("0.10"))
    assert isinstance(manager.approve(make_order("equal"), make_portfolio(), VENUE), Approved)
    decision = manager.approve(
        make_order("above", expected_price=D("100.01")), make_portfolio(), VENUE
    )
    assert_veto(decision, RiskReason.ORDER_NOTIONAL)


def test_price_deviation_allows_two_percent_and_vetoes_above_it(tmp_path: Path) -> None:
    manager, _, _ = make_manager(tmp_path)
    assert isinstance(
        manager.approve(make_order("equal", expected_price=D("102")), make_portfolio(), VENUE),
        Approved,
    )
    decision = manager.approve(
        make_order("above", expected_price=D("102.00000001")), make_portfolio(), VENUE
    )
    assert_veto(decision, RiskReason.PRICE_DEVIATION)


def test_duplicate_client_id_is_suppressed_after_first_approval(tmp_path: Path) -> None:
    manager, _, _ = make_manager(tmp_path)
    order = make_order("duplicate")
    assert isinstance(manager.approve(order, make_portfolio(), VENUE), Approved)
    assert_veto(manager.approve(order, make_portfolio(), VENUE), RiskReason.DUPLICATE_ORDER)


def test_staleness_veto_fires_at_exact_boundary(tmp_path: Path) -> None:
    manager, _, _ = make_manager(tmp_path, stale_after=timedelta(seconds=5))
    just_fresh = make_portfolio(quote_time=NOW - timedelta(microseconds=4_999_999))
    assert isinstance(manager.approve(make_order("fresh"), just_fresh, VENUE), Approved)
    stale = make_portfolio(quote_time=NOW - timedelta(seconds=5))
    assert_veto(manager.approve(make_order("stale"), stale, VENUE), RiskReason.STALE_DATA)


def test_valid_exit_is_allowed_during_loss_halt_and_with_stale_data(tmp_path: Path) -> None:
    manager, _, _ = make_manager(tmp_path)
    portfolio = make_portfolio(
        equity=D("980"),
        day_start=D("1000"),
        position=D("1"),
        quote_time=NOW - timedelta(days=1),
    )
    exit_order = make_order("exit", side=Side.SELL, is_exit=True)
    assert isinstance(manager.approve(exit_order, portfolio, VENUE), Approved)


def test_exit_flag_cannot_hide_a_position_reversal(tmp_path: Path) -> None:
    manager, _, _ = make_manager(tmp_path)
    portfolio = make_portfolio(position=D("1"))
    reversal = make_order("reversal", quantity=D("1.5"), side=Side.SELL, is_exit=True)
    assert_veto(manager.approve(reversal, portfolio, VENUE), RiskReason.INVALID_EXIT)


def test_kill_switch_live_window_allows_only_risk_reducing_exit(tmp_path: Path) -> None:
    manager, _, _ = make_manager(tmp_path, mode=ExecutionMode.LIVE)
    killed = make_portfolio(equity=D("850"), day_start=D("850"), week_start=D("850"), hwm=D("1000"))
    assert_veto(manager.approve(make_order("trip"), killed, VENUE), RiskReason.HWM_KILL_SWITCH)
    assert_veto(
        manager.approve(make_order("new-entry"), killed, VENUE),
        RiskReason.EXECUTION_MODE_GATE,
    )
    exit_portfolio = replace(killed, positions={"BTC-USD": D("1")})
    exit_order = make_order("flatten", side=Side.SELL, is_exit=True)
    assert isinstance(manager.approve(exit_order, exit_portfolio, VENUE), Approved)


@pytest.mark.parametrize("bad_value", [D("NaN"), D("0"), D("-1")])
@pytest.mark.parametrize("field", ["price", "volume"])
def test_nan_zero_or_negative_quote_price_or_volume_always_vetoes(
    tmp_path: Path, field: str, bad_value: Decimal
) -> None:
    manager, _, _ = make_manager(tmp_path)
    values = {"quote_price": D("100"), "quote_volume": D("10")}
    values[f"quote_{field}"] = bad_value
    decision = manager.approve(make_order(), make_portfolio(**values), VENUE)
    assert_veto(decision, RiskReason.INVALID_NUMERIC_INPUT)


@pytest.mark.parametrize("bad_price", [D("NaN"), D("0"), D("-1")])
def test_nan_zero_or_negative_order_price_always_vetoes(tmp_path: Path, bad_price: Decimal) -> None:
    manager, _, _ = make_manager(tmp_path)
    decision = manager.approve(make_order(expected_price=bad_price), make_portfolio(), VENUE)
    assert_veto(decision, RiskReason.INVALID_NUMERIC_INPUT)


def test_every_veto_is_appended_as_jsonl_with_reason(tmp_path: Path) -> None:
    manager, journal_path, _ = make_manager(tmp_path)
    assert_veto(
        manager.approve(make_order("one"), make_portfolio(quote_price=D("0")), VENUE),
        RiskReason.INVALID_NUMERIC_INPUT,
    )
    assert_veto(
        manager.approve(make_order("two", quantity=D("10.01")), make_portfolio(), VENUE),
        RiskReason.POSITION_CAP,
    )
    records = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
    assert [record["reason"] for record in records] == [
        RiskReason.INVALID_NUMERIC_INPUT.value,
        RiskReason.POSITION_CAP.value,
    ]
    assert all(record["event"] == "risk_veto" for record in records)
