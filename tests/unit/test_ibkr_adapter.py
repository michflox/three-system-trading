from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from adapters.ibkr import (
    FUTURES_SPECS,
    IBKRConfig,
    PaperAccountRequired,
    average_true_range,
    catastrophe_stop_request,
)
from core.events import Bar, OrderType, Side


def _bars() -> tuple[Bar, ...]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return tuple(
        Bar(
            "MES",
            start + timedelta(days=index),
            Decimal("100"),
            Decimal("102"),
            Decimal("98"),
            Decimal("100"),
            Decimal("10"),
        )
        for index in range(21)
    )


def test_all_required_micro_futures_are_declared_with_decimal_specs() -> None:
    assert set(FUTURES_SPECS) == {
        "MES",
        "MNQ",
        "M2K",
        "MYM",
        "MGC",
        "MHG",
        "MCL",
        "MNG",
        "M6E",
        "M6B",
        "M6A",
    }
    assert all(isinstance(spec.multiplier, Decimal) for spec in FUTURES_SPECS.values())
    assert all(isinstance(spec.tick_size, Decimal) for spec in FUTURES_SPECS.values())


def test_atr20_and_long_catastrophe_stop_are_exact_decimal() -> None:
    bars = _bars()
    assert average_true_range(bars) == Decimal("4")

    request = catastrophe_stop_request(
        symbol="MES",
        contract_month="202609",
        position_quantity=Decimal("2"),
        reference_price=Decimal("100"),
        bars=bars,
        created_at=datetime(2026, 7, 6, tzinfo=UTC),
    )

    assert request.order_type is OrderType.STOP
    assert request.side is Side.SELL
    assert request.quantity == Decimal("2")
    assert request.stop_price == Decimal("88")
    assert request.expected_price == Decimal("100")
    assert request.is_exit


def test_short_stop_rounds_away_from_position_at_contract_tick() -> None:
    request = catastrophe_stop_request(
        symbol="MES",
        contract_month="202609",
        position_quantity=Decimal("-1"),
        reference_price=Decimal("100.10"),
        bars=_bars(),
        created_at=datetime(2026, 7, 6, tzinfo=UTC),
    )
    assert request.side is Side.BUY
    assert request.stop_price == Decimal("112.25")


def test_configuration_refuses_live_account() -> None:
    with pytest.raises(PaperAccountRequired):
        IBKRConfig(account="U123456")
