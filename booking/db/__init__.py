"""SQLite access for the booking service (the sole writer).

Connections run in autocommit mode; every write goes through `begin_immediate()`,
which takes SQLite's write lock up front so a second writer waits (busy timeout)
and then sees the first writer's rows. Feasibility is re-checked inside that block.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from booking.config import get_settings


def connect(db_path: Path | str | None = None, *, busy_timeout_ms: int | None = None) -> sqlite3.Connection:
    s = get_settings()
    path = Path(db_path) if db_path else s.db_path
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False,
                           timeout=(busy_timeout_ms or s.busy_timeout_ms) / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms or s.busy_timeout_ms}")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def begin_immediate(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One write transaction. Commits on success, rolls back on any exception."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
