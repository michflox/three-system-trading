from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from core.events import Bar


def test_bar_is_frozen_and_uses_decimals() -> None:
    bar = Bar(
        symbol="BTC-USD",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("90"),
        close=Decimal("105"),
        volume=Decimal("1.25"),
    )
    with pytest.raises(FrozenInstanceError):
        bar.close = Decimal("106")  # type: ignore[misc]


def test_bar_rejects_float_and_naive_time() -> None:
    values = dict(
        symbol="BTC-USD",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("90"),
        close=Decimal("105"),
        volume=Decimal("1.25"),
    )
    values["close"] = 105.0
    with pytest.raises(TypeError):
        Bar(**values)  # type: ignore[arg-type]
    values["close"] = Decimal("105")
    values["timestamp"] = datetime(2026, 1, 1)
    with pytest.raises(ValueError, match="timezone-aware"):
        Bar(**values)  # type: ignore[arg-type]
