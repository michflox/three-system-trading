from pathlib import Path

import pytest

from core.execution_mode import (
    ExecutionMode,
    LiveGateConfig,
    is_live_permitted,
    require_live_permission,
)

ARM_VALUE = "a" * 64


def config(arm_file: Path, *, enabled: bool = True, digest: str = ARM_VALUE) -> LiveGateConfig:
    return LiveGateConfig(live_enabled=enabled, arm_file=arm_file, arm_sha256=digest)


def test_all_three_independent_switches_permit_live(tmp_path: Path) -> None:
    arm_file = tmp_path / "arm.live"
    arm_file.write_text(ARM_VALUE, encoding="ascii")
    assert is_live_permitted(config(arm_file), {"TRADING_LIVE": "1"})


@pytest.mark.parametrize("environment", [{}, {"TRADING_LIVE": "0"}, {"TRADING_LIVE": "true"}])
def test_missing_or_inexact_environment_switch_blocks_live(
    tmp_path: Path, environment: dict[str, str]
) -> None:
    arm_file = tmp_path / "arm.live"
    arm_file.write_text(ARM_VALUE, encoding="ascii")
    assert not is_live_permitted(config(arm_file), environment)


def test_missing_config_switch_blocks_live(tmp_path: Path) -> None:
    arm_file = tmp_path / "arm.live"
    arm_file.write_text(ARM_VALUE, encoding="ascii")
    assert not is_live_permitted(config(arm_file, enabled=False), {"TRADING_LIVE": "1"})


def test_missing_arm_file_blocks_live(tmp_path: Path) -> None:
    assert not is_live_permitted(config(tmp_path / "arm.live"), {"TRADING_LIVE": "1"})


def test_mismatched_or_malformed_arm_file_blocks_live(tmp_path: Path) -> None:
    arm_file = tmp_path / "arm.live"
    arm_file.write_text("b" * 64, encoding="ascii")
    assert not is_live_permitted(config(arm_file), {"TRADING_LIVE": "1"})
    arm_file.write_text("not-a-sha256", encoding="ascii")
    assert not is_live_permitted(config(arm_file), {"TRADING_LIVE": "1"})


def test_live_request_fails_closed_but_non_live_modes_do_not(tmp_path: Path) -> None:
    gate = config(tmp_path / "missing-arm.live")
    with pytest.raises(PermissionError, match="triple gate"):
        require_live_permission(ExecutionMode.LIVE, gate, {"TRADING_LIVE": "1"})
    require_live_permission(ExecutionMode.PAPER, gate, {})
    require_live_permission(ExecutionMode.DRY_RUN, gate, {})
