"""Crash-safe, versioned SQLite state storage."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1


class UnsupportedSchemaError(RuntimeError):
    """Raised when the database schema is newer than this application."""


class StateStore:
    """Small transactional byte-value store backed by SQLite.

    A fresh connection is used for each operation, allowing a process that starts
    after a crash to recover without inheriting connection state.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self.transaction() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_info ("
                "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
                "version INTEGER NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS state ("
                "key TEXT PRIMARY KEY NOT NULL, value BLOB NOT NULL)"
            )
            row = connection.execute(
                "SELECT version FROM schema_info WHERE singleton = 1"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO schema_info(singleton, version) VALUES (1, ?)",
                    (SCHEMA_VERSION,),
                )
            elif int(row[0]) > SCHEMA_VERSION:
                raise UnsupportedSchemaError(
                    f"database schema {row[0]} is newer than supported schema {SCHEMA_VERSION}"
                )
            elif int(row[0]) < SCHEMA_VERSION:
                self._migrate(connection, int(row[0]))

    @staticmethod
    def _migrate(connection: sqlite3.Connection, from_version: int) -> None:
        # Version 1 is the initial schema. Future migrations belong here and run
        # inside the same atomic transaction as the version update.
        if from_version != 0:
            raise UnsupportedSchemaError(f"no migration path from schema {from_version}")
        connection.execute(
            "UPDATE schema_info SET version = ? WHERE singleton = 1", (SCHEMA_VERSION,)
        )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection inside an atomic immediate transaction."""

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    @property
    def schema_version(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT version FROM schema_info WHERE singleton = 1"
            ).fetchone()
        if row is None:
            raise RuntimeError("schema version row is missing")
        return int(row[0])

    def get(self, key: str) -> bytes | None:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return None if row is None else bytes(row[0])

    def set(self, key: str, value: bytes) -> None:
        if not isinstance(value, bytes):
            raise TypeError("state values must be bytes")
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO state(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def delete(self, key: str) -> None:
        with self.transaction() as connection:
            connection.execute("DELETE FROM state WHERE key = ?", (key,))
