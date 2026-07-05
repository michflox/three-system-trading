"""Validated fee schedule loading and fee calculation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml


class FeeModel(StrEnum):
    MAKER_TAKER = "maker_taker"
    PER_CONTRACT = "per_contract"


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    venue: str
    model: FeeModel
    maker_rate: Decimal | None = None
    taker_rate: Decimal | None = None
    commission_per_contract: Decimal | None = None
    additional_per_contract: Decimal = Decimal("0")
    minimum_per_order: Decimal = Decimal("0")

    def calculate(self, *, quantity: Decimal, notional: Decimal, maker: bool) -> Decimal:
        if quantity <= 0 or notional <= 0:
            raise ValueError("fee quantity and notional must be positive")
        if self.model is FeeModel.MAKER_TAKER:
            rate = self.maker_rate if maker else self.taker_rate
            if rate is None or rate <= 0:
                raise ValueError(f"{self.venue} requires an explicit positive fee-rate override")
            return notional * rate
        if self.commission_per_contract is None or self.commission_per_contract <= 0:
            raise ValueError(f"{self.venue} requires a positive per-contract commission")
        broker_commission = max(
            self.minimum_per_order,
            abs(quantity) * self.commission_per_contract,
        )
        return broker_commission + abs(quantity) * self.additional_per_contract


def load_fee_schedule(
    path: Path | str,
    *,
    overrides: Mapping[str, Decimal] | None = None,
) -> FeeSchedule:
    """Load a YAML schedule, applying explicit account-tier overrides when required."""

    raw: Any = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("fee schedule root must be a mapping")
    data = dict(raw)
    for key, value in (overrides or {}).items():
        data[key] = str(value)

    model = FeeModel(_required_string(data, "model"))
    schedule = FeeSchedule(
        venue=_required_string(data, "venue"),
        model=model,
        maker_rate=_optional_decimal(data.get("maker_rate")),
        taker_rate=_optional_decimal(data.get("taker_rate")),
        commission_per_contract=_optional_decimal(data.get("commission_per_contract")),
        additional_per_contract=_optional_decimal(data.get("additional_per_contract", "0"))
        or Decimal("0"),
        minimum_per_order=_optional_decimal(data.get("minimum_per_order", "0")) or Decimal("0"),
    )
    _validate_schedule(schedule)
    return schedule


def _required_string(data: Mapping[object, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"fee schedule requires string field {key!r}")
    return value


def _optional_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, (str, int)):
        raise ValueError("fee values must be quoted decimal strings")
    parsed = Decimal(str(value))
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("fee values must be non-negative finite Decimals")
    return parsed


def _validate_schedule(schedule: FeeSchedule) -> None:
    if schedule.model is FeeModel.MAKER_TAKER:
        if schedule.maker_rate is None or schedule.taker_rate is None:
            raise ValueError(
                f"{schedule.venue} fee rates are account-specific; provide maker_rate and "
                "taker_rate overrides"
            )
    elif schedule.commission_per_contract is None:
        raise ValueError(f"{schedule.venue} requires commission_per_contract")
