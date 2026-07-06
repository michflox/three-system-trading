"""Deterministic futures roll calendars and Panama back-adjusted series.

Contract rules were checked against current CME Group product specifications on 2026-07-06:
- Equity micros: https://www.cmegroup.com/markets/equities/micro-emini-equity.html
- Micro Gold: https://www.cmegroup.com/markets/metals/precious/e-micro-gold.contractSpecs.html
- Micro Copper: https://www.cmegroup.com/education/files/micro-copper-futures-factcard.pdf
- Micro WTI: https://www.cmegroup.com/markets/energy/crude-oil/micro-wti-crude-oil.contractSpecs.html
- Micro Henry Hub: https://www.cmegroup.com/notices/clearing/2023/09/Chadv23-294.pdf
- Micro FX: https://www.cmegroup.com/trading/fx/files/FX-241_EmicroSellSheet_Updated_11_10.pdf
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum

from core.events import Bar

QUARTERLY_MONTHS = frozenset({3, 6, 9, 12})
GOLD_MONTHS = frozenset({2, 4, 6, 8, 10, 12})
SUPPORTED_SYMBOLS = frozenset(
    {"MES", "MNQ", "M2K", "MYM", "MGC", "MHG", "MCL", "MNG", "M6E", "M6B", "M6A"}
)


class RollConvention(StrEnum):
    EQUITY_QUARTERLY = "equity_quarterly"
    FX_QUARTERLY = "fx_quarterly"
    MICRO_GOLD = "micro_gold"
    MICRO_COPPER = "micro_copper"
    MICRO_WTI = "micro_wti"
    MICRO_NATURAL_GAS = "micro_natural_gas"


@dataclass(frozen=True, slots=True)
class RollRule:
    convention: RollConvention
    listed_months: frozenset[int] | None
    roll_business_days: int

    def __post_init__(self) -> None:
        if self.roll_business_days < 3 or self.roll_business_days > 5:
            raise ValueError("roll lead must be between three and five business days")


ROLL_RULES: Mapping[str, RollRule] = {
    "MES": RollRule(RollConvention.EQUITY_QUARTERLY, QUARTERLY_MONTHS, 5),
    "MNQ": RollRule(RollConvention.EQUITY_QUARTERLY, QUARTERLY_MONTHS, 5),
    "M2K": RollRule(RollConvention.EQUITY_QUARTERLY, QUARTERLY_MONTHS, 5),
    "MYM": RollRule(RollConvention.EQUITY_QUARTERLY, QUARTERLY_MONTHS, 5),
    "MGC": RollRule(RollConvention.MICRO_GOLD, GOLD_MONTHS, 5),
    "MHG": RollRule(RollConvention.MICRO_COPPER, None, 5),
    "MCL": RollRule(RollConvention.MICRO_WTI, None, 4),
    "MNG": RollRule(RollConvention.MICRO_NATURAL_GAS, None, 4),
    "M6E": RollRule(RollConvention.FX_QUARTERLY, QUARTERLY_MONTHS, 3),
    "M6B": RollRule(RollConvention.FX_QUARTERLY, QUARTERLY_MONTHS, 3),
    "M6A": RollRule(RollConvention.FX_QUARTERLY, QUARTERLY_MONTHS, 3),
}


@dataclass(frozen=True, slots=True)
class ContractSchedule:
    symbol: str
    contract_month: str
    last_trade_date: date
    first_notice_date: date | None
    roll_date: date
    roll_business_days: int

    def __post_init__(self) -> None:
        _parse_contract_month(self.contract_month)
        anchor = min(
            self.last_trade_date,
            self.first_notice_date or self.last_trade_date,
        )
        if self.roll_date >= anchor:
            raise ValueError("roll date must precede first notice/last trade")
        if self.roll_business_days < 3 or self.roll_business_days > 5:
            raise ValueError("roll lead must be between three and five business days")


@dataclass(frozen=True, slots=True)
class RollCalendar:
    symbol: str
    contracts: tuple[ContractSchedule, ...]

    def __post_init__(self) -> None:
        if self.symbol not in SUPPORTED_SYMBOLS:
            raise ValueError(f"unsupported futures symbol {self.symbol!r}")
        if len(self.contracts) < 2:
            raise ValueError("a roll calendar requires at least two contracts")
        months = [item.contract_month for item in self.contracts]
        if months != sorted(months) or len(months) != len(set(months)):
            raise ValueError("contract schedules must be unique and chronological")
        if any(item.symbol != self.symbol for item in self.contracts):
            raise ValueError("calendar contains a different root symbol")

    def active_contract(self, on_date: date) -> ContractSchedule:
        active = self.contracts[0]
        for following in self.contracts[1:]:
            if on_date < active.roll_date:
                break
            active = following
        return active

    def transitions(self) -> tuple[tuple[date, str, str], ...]:
        return tuple(
            (current.roll_date, current.contract_month, following.contract_month)
            for current, following in zip(
                self.contracts,
                self.contracts[1:],
                strict=False,
            )
        )


@dataclass(frozen=True, slots=True)
class ContractBar:
    contract_month: str
    bar: Bar

    def __post_init__(self) -> None:
        _parse_contract_month(self.contract_month)


@dataclass(frozen=True, slots=True)
class ContinuousBar:
    contract_month: str
    bar: Bar


@dataclass(frozen=True, slots=True)
class PositionRollState:
    session: date
    active_contract: str
    positions: Mapping[str, Decimal]


def build_roll_calendar(
    symbol: str,
    contract_months: Sequence[str],
    *,
    holidays: Iterable[date] = (),
) -> RollCalendar:
    """Build the configured 3-5 business-day roll calendar for a micro future."""

    rule = ROLL_RULES.get(symbol)
    if rule is None:
        raise ValueError(f"unsupported futures symbol {symbol!r}")
    holiday_set = frozenset(holidays)
    schedules: list[ContractSchedule] = []
    for contract_month in contract_months:
        year, month = _parse_contract_month(contract_month)
        if rule.listed_months is not None and month not in rule.listed_months:
            raise ValueError(f"{symbol} does not list contract month {contract_month}")
        last_trade, first_notice = _contract_dates(
            rule.convention,
            year,
            month,
            holiday_set,
        )
        anchor = min(last_trade, first_notice or last_trade)
        roll_date = _business_days_before(anchor, rule.roll_business_days, holiday_set)
        schedules.append(
            ContractSchedule(
                symbol,
                contract_month,
                last_trade,
                first_notice,
                roll_date,
                rule.roll_business_days,
            )
        )
    return RollCalendar(symbol, tuple(schedules))


def panama_back_adjust(
    calendar: RollCalendar,
    contract_bars: Iterable[ContractBar],
) -> tuple[ContinuousBar, ...]:
    """Create a backward-Panama series with each roll gap applied to prior history."""

    by_contract: dict[str, dict[date, Bar]] = {}
    all_dates: set[date] = set()
    for item in contract_bars:
        if item.bar.symbol != calendar.symbol:
            raise ValueError("contract bar root does not match calendar")
        session = item.bar.timestamp.date()
        by_contract.setdefault(item.contract_month, {})[session] = item.bar
        all_dates.add(session)
    if not all_dates:
        return ()

    output: list[ContinuousBar] = []
    prior_contract: str | None = None
    for session in sorted(all_dates):
        active = calendar.active_contract(session).contract_month
        active_bar = by_contract.get(active, {}).get(session)
        if active_bar is None:
            continue
        if prior_contract is not None and active != prior_contract:
            expiring_bar = by_contract.get(prior_contract, {}).get(session)
            if expiring_bar is None:
                raise ValueError(
                    f"missing overlap bar for {prior_contract} on roll session {session}"
                )
            gap = active_bar.close - expiring_bar.close
            output = [
                ContinuousBar(item.contract_month, _shift_bar(item.bar, gap))
                for item in output
            ]
        output.append(
            ContinuousBar(
                active,
                replace(active_bar, symbol=calendar.symbol),
            )
        )
        prior_contract = active
    return tuple(output)


def simulate_position_rolls(
    calendar: RollCalendar,
    sessions: Iterable[date],
    quantity: Decimal,
) -> tuple[PositionRollState, ...]:
    """Move one position between contracts without duplicating or dropping exposure."""

    if not quantity.is_finite():
        raise ValueError("position quantity must be finite")
    positions: dict[str, Decimal] = {}
    states: list[PositionRollState] = []
    prior_contract: str | None = None
    for session in sorted(set(sessions)):
        active = calendar.active_contract(session).contract_month
        if active != prior_contract:
            if prior_contract is not None:
                positions[prior_contract] = Decimal("0")
            positions[active] = quantity
            prior_contract = active
        if sum(positions.values(), Decimal("0")) != quantity:
            raise RuntimeError("roll changed aggregate position quantity")
        nonzero = {month: value for month, value in positions.items() if value != 0}
        states.append(PositionRollState(session, active, nonzero))
    return tuple(states)


def _contract_dates(
    convention: RollConvention,
    year: int,
    month: int,
    holidays: frozenset[date],
) -> tuple[date, date | None]:
    if convention is RollConvention.EQUITY_QUARTERLY:
        return _third_weekday(year, month, 4), None
    if convention is RollConvention.FX_QUARTERLY:
        third_wednesday = _third_weekday(year, month, 2)
        return _business_days_before(third_wednesday, 2, holidays), None
    if convention is RollConvention.MICRO_GOLD:
        previous_year, previous_month = _previous_month(year, month)
        first_notice = _last_business_day(previous_year, previous_month, holidays)
        return _nth_last_business_day(year, month, 3, holidays), first_notice
    if convention is RollConvention.MICRO_COPPER:
        previous_year, previous_month = _previous_month(year, month)
        return _nth_last_business_day(previous_year, previous_month, 3, holidays), None
    if convention is RollConvention.MICRO_WTI:
        previous_year, previous_month = _previous_month(year, month)
        twenty_fifth = date(previous_year, previous_month, 25)
        lead = 4 if _is_business_day(twenty_fifth, holidays) else 5
        return _business_days_before(twenty_fifth, lead, holidays), None
    if convention is RollConvention.MICRO_NATURAL_GAS:
        previous_year, previous_month = _previous_month(year, month)
        return _nth_last_business_day(previous_year, previous_month, 4, holidays), None
    raise AssertionError(f"unhandled roll convention {convention}")


def _parse_contract_month(value: str) -> tuple[int, int]:
    if len(value) != 6 or not value.isdigit():
        raise ValueError("contract month must use YYYYMM")
    year = int(value[:4])
    month = int(value[4:])
    if year < 2000 or month < 1 or month > 12:
        raise ValueError("invalid contract month")
    return year, month


def _third_weekday(year: int, month: int, weekday: int) -> date:
    current = date(year, month, 1)
    offset = (weekday - current.weekday()) % 7
    return current + timedelta(days=offset + 14)


def _business_days_before(
    anchor: date,
    count: int,
    holidays: frozenset[date],
) -> date:
    current = anchor
    remaining = count
    while remaining:
        current -= timedelta(days=1)
        if _is_business_day(current, holidays):
            remaining -= 1
    return current


def _last_business_day(year: int, month: int, holidays: frozenset[date]) -> date:
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    current = date(next_year, next_month, 1) - timedelta(days=1)
    while not _is_business_day(current, holidays):
        current -= timedelta(days=1)
    return current


def _nth_last_business_day(
    year: int,
    month: int,
    count: int,
    holidays: frozenset[date],
) -> date:
    last = _last_business_day(year, month, holidays)
    return last if count == 1 else _business_days_before(last, count - 1, holidays)


def _previous_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _is_business_day(value: date, holidays: frozenset[date]) -> bool:
    return value.weekday() < 5 and value not in holidays


def _shift_bar(bar: Bar, amount: Decimal) -> Bar:
    shifted = (bar.open + amount, bar.high + amount, bar.low + amount, bar.close + amount)
    if any(value <= 0 for value in shifted):
        raise ValueError("Panama adjustment produced a non-positive price")
    return replace(
        bar,
        open=shifted[0],
        high=shifted[1],
        low=shifted[2],
        close=shifted[3],
    )
