import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from crypto.normalize import (
    BelowMinimumNotional,
    CredentialPermissionError,
    DailyFeeLookup,
    FeeQuote,
    InstrumentRules,
    SnapMode,
    normalize_order,
    parse_coinbase_fee,
    snap_to_increment,
    verify_coinbase_key_permissions,
    verify_kraken_key_permissions,
)
from crypto.symbols import Venue


@given(
    units=st.integers(min_value=0, max_value=10**12),
    fractional=st.integers(min_value=0, max_value=999),
    places=st.integers(min_value=0, max_value=12),
)
def test_snap_down_never_exceeds_input_and_error_is_below_one_tick(
    units: int, fractional: int, places: int
) -> None:
    increment = Decimal("1").scaleb(-places)
    value = Decimal(units) * increment + Decimal(fractional) * increment / Decimal("1000")
    snapped = snap_to_increment(value, increment, SnapMode.DOWN)
    assert snapped <= value
    assert value - snapped < increment
    assert snapped % increment == 0


@given(
    units=st.integers(min_value=0, max_value=10**12),
    fractional=st.integers(min_value=1, max_value=999),
    places=st.integers(min_value=0, max_value=12),
)
def test_snap_up_never_falls_below_input_and_error_is_below_one_tick(
    units: int, fractional: int, places: int
) -> None:
    increment = Decimal("1").scaleb(-places)
    value = Decimal(units) * increment + Decimal(fractional) * increment / Decimal("1000")
    snapped = snap_to_increment(value, increment, SnapMode.UP)
    assert snapped >= value
    assert snapped - value < increment
    assert snapped % increment == 0


def test_order_normalization_checks_minimum_after_snapping() -> None:
    rules = InstrumentRules(
        canonical_symbol="BTC-USD",
        tick_size=Decimal("0.10"),
        step_size=Decimal("0.001"),
        minimum_size=Decimal("0.001"),
        minimum_notional=Decimal("10"),
    )
    with pytest.raises(BelowMinimumNotional):
        normalize_order(price=Decimal("9999.99"), size=Decimal("0.001"), rules=rules)
    normalized = normalize_order(price=Decimal("10000.09"), size=Decimal("0.0019"), rules=rules)
    assert normalized.price == Decimal("10000.00")
    assert normalized.size == Decimal("0.001")
    assert normalized.notional == Decimal("10.00000")


def test_coinbase_fee_parser_uses_api_response_values() -> None:
    parsed = parse_coinbase_fee(
        {"fee_tier": {"maker_fee_rate": "0.0025", "taker_fee_rate": "0.0040"}},
        ["BTC-USD"],
    )
    assert parsed["BTC-USD"].maker_rate == Decimal("0.0025")
    assert parsed["BTC-USD"].taker_rate == Decimal("0.0040")


def test_fee_lookup_refreshes_daily_and_never_uses_a_static_fallback() -> None:
    class Api:
        calls = 0

        async def fetch(self, venue: Venue, symbols: list[str]) -> dict[str, FeeQuote]:
            self.calls += 1
            rate = Decimal(self.calls) / Decimal("1000")
            return {symbol: FeeQuote(rate, rate * 2, clock[0]) for symbol in symbols}

    clock = [datetime(2026, 7, 4, tzinfo=UTC)]
    api = Api()
    lookup = DailyFeeLookup(api, clock=lambda: clock[0])

    async def exercise() -> None:
        first = await lookup.get(Venue.COINBASE, ["BTC-USD"])
        clock[0] += timedelta(hours=23)
        cached = await lookup.get(Venue.COINBASE, ["BTC-USD"])
        clock[0] += timedelta(hours=1)
        refreshed = await lookup.get(Venue.COINBASE, ["BTC-USD"])
        assert first["BTC-USD"].maker_rate == Decimal("0.001")
        assert cached["BTC-USD"].maker_rate == Decimal("0.001")
        assert refreshed["BTC-USD"].maker_rate == Decimal("0.002")

    asyncio.run(exercise())
    assert api.calls == 2


def test_fee_api_permission_checks_refuse_withdrawal_authority() -> None:
    with pytest.raises(CredentialPermissionError):
        verify_coinbase_key_permissions({"can_view": True, "can_trade": True, "can_transfer": True})
    with pytest.raises(CredentialPermissionError):
        verify_kraken_key_permissions(
            {"result": {"permissions": ["modify-trades", "withdraw-funds"]}}
        )


def test_fee_api_permission_checks_accept_trade_without_withdrawal() -> None:
    verify_coinbase_key_permissions({"can_view": True, "can_trade": True, "can_transfer": False})
    verify_kraken_key_permissions(
        {"result": {"permissions": ["query-funds", "modify-trades", "close-trades"]}}
    )
