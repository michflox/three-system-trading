from decimal import Decimal
from pathlib import Path

import pytest

from engines.fees import load_fee_schedule

D = Decimal
FEE_DIR = Path(__file__).parents[2] / "config" / "fees"


def test_coinbase_requires_account_specific_rates() -> None:
    with pytest.raises(ValueError, match="account-specific"):
        load_fee_schedule(FEE_DIR / "coinbase.yaml")
    schedule = load_fee_schedule(
        FEE_DIR / "coinbase.yaml",
        overrides={"maker_rate": D("0.002"), "taker_rate": D("0.004")},
    )
    assert schedule.calculate(quantity=D("1"), notional=D("100"), maker=False) == D("0.4")


def test_kraken_base_tier_maker_and_taker_rates() -> None:
    schedule = load_fee_schedule(FEE_DIR / "kraken.yaml")
    assert schedule.calculate(quantity=D("1"), notional=D("100"), maker=True) == D("0.2500")
    assert schedule.calculate(quantity=D("1"), notional=D("100"), maker=False) == D("0.4000")


def test_ibkr_futures_per_contract_fee() -> None:
    schedule = load_fee_schedule(FEE_DIR / "ibkr_futures.yaml")
    assert schedule.calculate(quantity=D("2"), notional=D("100000"), maker=False) == D("1.70")


def test_ibkr_options_minimum_and_known_external_fees() -> None:
    schedule = load_fee_schedule(FEE_DIR / "ibkr_options.yaml")
    assert schedule.calculate(quantity=D("1"), notional=D("500"), maker=False) == D("1.04825")
    assert schedule.calculate(quantity=D("2"), notional=D("1000"), maker=False) == D("1.39650")
