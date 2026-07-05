"""Append-only operational journal."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from pathlib import Path


class AppendOnlyJournal:
    """Durably append structured records as one JSON object per line."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, record: Mapping[str, object]) -> None:
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with self._lock, self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
