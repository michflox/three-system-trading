import subprocess
import sys
import time
from pathlib import Path

from core.state import StateStore


def test_killed_writer_leaves_last_committed_value_and_database_usable(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    ready = tmp_path / "writer-ready"
    store = StateStore(database)
    store.set("risk-state", b"committed")
    writer = """
import sqlite3
import sys
import time
from pathlib import Path
database, ready = sys.argv[1], Path(sys.argv[2])
connection = sqlite3.connect(database, isolation_level=None)
connection.execute("PRAGMA journal_mode = DELETE")
connection.execute("PRAGMA synchronous = FULL")
connection.execute("BEGIN IMMEDIATE")
connection.execute("UPDATE state SET value = ? WHERE key = ?", (b"uncommitted", "risk-state"))
ready.write_text("ready", encoding="ascii")
time.sleep(60)
"""
    process = subprocess.Popen([sys.executable, "-c", writer, str(database), str(ready)])
    deadline = time.monotonic() + 10
    while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.exists(), "writer did not reach its open transaction"
    process.kill()
    process.wait(timeout=10)
    recovered = StateStore(database)
    assert recovered.get("risk-state") == b"committed"
    recovered.set("risk-state", b"after-recovery")
    assert recovered.get("risk-state") == b"after-recovery"
