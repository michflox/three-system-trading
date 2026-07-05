from decimal import Decimal

from core.sizing import (
    apply_rebalance_buffer,
    apply_sector_cap,
    ewma_volatility,
    size_position,
    vol_target_contracts,
)

D = Decimal


def test_ewma_span_32_volatility_uses_decimal_returns() -> None:
    # Returns are exactly +10% and -10%; equal squares keep EWMA RMS vol at 10%.
    assert ewma_volatility([D("100"), D("110"), D("99")], span=32) == D("0.1")


def test_vol_target_formula_and_round_toward_zero() -> None:
    contracts = vol_target_contracts(
        equity=D("100000"),
        target_vol=D("0.10"),
        weight=D("0.50"),
        sigma=D("0.01"),
        point_value=D("1"),
        price=D("100"),
    )
    assert contracts == D("314")
    short = vol_target_contracts(
        equity=D("100000"),
        target_vol=D("0.10"),
        weight=D("-0.50"),
        sigma=D("0.01"),
        point_value=D("1"),
        price=D("100"),
    )
    assert short == D("-314")


def test_sub_contract_and_invalid_inputs_size_to_zero() -> None:
    assert vol_target_contracts(
        equity=D("100"),
        target_vol=D("0.01"),
        weight=D("0.01"),
        sigma=D("0.50"),
        point_value=D("50"),
        price=D("100"),
    ) == D("0")
    assert vol_target_contracts(
        equity=D("100"),
        target_vol=D("0.01"),
        weight=D("1"),
        sigma=D("0"),
        point_value=D("50"),
        price=D("100"),
    ) == D("0")


def test_sector_cap_clips_to_remaining_whole_contract_capacity() -> None:
    assert apply_sector_cap(
        target_contracts=D("314"),
        equity=D("100000"),
        sector_cap_fraction=D("0.10"),
        current_sector_notional=D("1000"),
        point_value=D("1"),
        price=D("100"),
    ) == D("90")


def test_rebalance_buffer_holds_below_25_percent_and_trades_at_boundary() -> None:
    assert apply_rebalance_buffer(current_contracts=D("80"), target_contracts=D("99")) == D("80")
    assert apply_rebalance_buffer(current_contracts=D("80"), target_contracts=D("100")) == D("100")


def test_size_position_applies_sector_cap_then_buffer() -> None:
    assert size_position(
        equity=D("100000"),
        target_vol=D("0.10"),
        weight=D("0.50"),
        sigma=D("0.01"),
        point_value=D("1"),
        price=D("100"),
        sector_cap_fraction=D("0.10"),
        current_sector_notional=D("9000"),
        current_contracts=D("80"),
    ) == D("80")
