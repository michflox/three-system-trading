import sqlite3
from pathlib import Path

import pytest

from core.state import SCHEMA_VERSION, StateStore, UnsupportedSchemaError


def test_state_round_trip_and_atomic_rollback(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.set("position", b"old")
    with pytest.raises(RuntimeError, match="abort"), store.transaction() as connection:
        connection.execute("UPDATE state SET value = ? WHERE key = ?", (b"new", "position"))
        raise RuntimeError("abort")
    assert store.get("position") == b"old"
    assert store.schema_version == SCHEMA_VERSION


def test_delete(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.set("key", b"value")
    store.delete("key")
    assert store.get("key") is None


def test_schema_zero_is_migrated_atomically(tmp_path: Path) -> None:
    database = tmp_path / "old.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE schema_info ("
            "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), version INTEGER NOT NULL)"
        )
        connection.execute("INSERT INTO schema_info VALUES (1, 0)")
    assert StateStore(database).schema_version == SCHEMA_VERSION


def test_newer_schema_is_refused(tmp_path: Path) -> None:
    database = tmp_path / "future.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE schema_info ("
            "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), version INTEGER NOT NULL)"
        )
        connection.execute("INSERT INTO schema_info VALUES (1, ?)", (SCHEMA_VERSION + 1,))
    with pytest.raises(UnsupportedSchemaError, match="newer"):
        StateStore(database)
