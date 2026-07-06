from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from core.events import Bar
from data.futures_chain import (
    ContractBar,
    ContractSchedule,
    RollCalendar,
    build_roll_calendar,
    panama_back_adjust,
    simulate_position_rolls,
)


def _bar(session: date, close: str) -> Bar:
    price = Decimal(close)
    return Bar(
        "MES",
        datetime(session.year, session.month, session.day, tzinfo=UTC),
        price,
        price + Decimal("1"),
        price - Decimal("1"),
        price,
        Decimal("100"),
    )


@pytest.mark.parametrize(
    ("symbol", "month", "last_trade", "first_notice", "roll_date"),
    [
        ("MES", "202603", date(2026, 3, 20), None, date(2026, 3, 13)),
        ("M6E", "202603", date(2026, 3, 16), None, date(2026, 3, 11)),
        ("MGC", "202604", date(2026, 4, 28), date(2026, 3, 31), date(2026, 3, 24)),
        ("MHG", "202605", date(2026, 4, 28), None, date(2026, 4, 21)),
        ("MCL", "202605", date(2026, 4, 20), None, date(2026, 4, 14)),
        ("MNG", "202605", date(2026, 4, 27), None, date(2026, 4, 21)),
    ],
)
def test_known_cme_contract_calendars(
    symbol: str,
    month: str,
    last_trade: date,
    first_notice: date | None,
    roll_date: date,
) -> None:
    calendar = build_roll_calendar(symbol, (month, "202606"))
    schedule = calendar.contracts[0]
    assert schedule.last_trade_date == last_trade
    assert schedule.first_notice_date == first_notice
    assert schedule.roll_date == roll_date


def test_roll_moves_position_once_without_doubling_or_vanishing() -> None:
    calendar = build_roll_calendar("MES", ("202603", "202606"))
    sessions = (date(2026, 3, 12), date(2026, 3, 13), date(2026, 3, 16))
    states = simulate_position_rolls(calendar, sessions, Decimal("3"))

    assert [state.active_contract for state in states] == ["202603", "202606", "202606"]
    assert [sum(state.positions.values(), Decimal("0")) for state in states] == [
        Decimal("3"),
        Decimal("3"),
        Decimal("3"),
    ]
    assert all(len(state.positions) == 1 for state in states)


def test_panama_adjustment_removes_roll_gap_and_preserves_volume() -> None:
    calendar = RollCalendar(
        "MES",
        (
            ContractSchedule("MES", "202603", date(2026, 3, 20), None, date(2026, 3, 13), 5),
            ContractSchedule("MES", "202606", date(2026, 6, 19), None, date(2026, 6, 12), 5),
        ),
    )
    bars = (
        ContractBar("202603", _bar(date(2026, 3, 12), "100")),
        ContractBar("202603", _bar(date(2026, 3, 13), "101")),
        ContractBar("202606", _bar(date(2026, 3, 13), "111")),
        ContractBar("202606", _bar(date(2026, 3, 16), "112")),
    )

    continuous = panama_back_adjust(calendar, bars)

    assert [item.contract_month for item in continuous] == ["202603", "202606", "202606"]
    assert [item.bar.close for item in continuous] == [
        Decimal("110"),
        Decimal("111"),
        Decimal("112"),
    ]
    assert all(item.bar.volume == Decimal("100") for item in continuous)


def test_calendar_rejects_unlisted_contract_month() -> None:
    with pytest.raises(ValueError, match="does not list"):
        build_roll_calendar("MES", ("202604", "202606"))
