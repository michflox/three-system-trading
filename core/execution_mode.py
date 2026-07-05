"""Execution modes and the fail-closed LIVE activation gate."""

from __future__ import annotations

import hmac
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class ExecutionMode(StrEnum):
    DRY_RUN = "DRY_RUN"
    PAPER = "PAPER"
    LIVE = "LIVE"


@dataclass(frozen=True, slots=True)
class LiveGateConfig:
    """Configuration-controlled parts of the LIVE triple gate."""

    live_enabled: bool
    arm_file: Path
    arm_sha256: str


_SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}", flags=re.ASCII)


def is_live_permitted(
    config: LiveGateConfig,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return whether every independent LIVE gate is present and valid.

    Every error fails closed. This function only reads state; it never creates or
    modifies the arm file, environment, or configuration.
    """

    environment = os.environ if environ is None else environ
    if environment.get("TRADING_LIVE") != "1" or not config.live_enabled:
        return False
    if _SHA256_PATTERN.fullmatch(config.arm_sha256) is None:
        return False

    try:
        if not config.arm_file.is_file():
            return False
        arm_value = config.arm_file.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return False

    if _SHA256_PATTERN.fullmatch(arm_value) is None:
        return False
    return hmac.compare_digest(arm_value.lower(), config.arm_sha256.lower())


def require_live_permission(
    mode: ExecutionMode,
    config: LiveGateConfig,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Raise before startup if LIVE was requested without all three gates."""

    if mode is ExecutionMode.LIVE and not is_live_permitted(config, environ):
        raise PermissionError("LIVE mode requested but the triple gate is not satisfied")
