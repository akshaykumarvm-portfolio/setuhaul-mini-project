"""Postgres access for the booking service (the only service that writes capacity).

Concurrency model (replaces the spec's SQLite BEGIN IMMEDIATE):
  * every write runs in one transaction via `write_tx()`;
  * inside it, the rows being claimed are locked with SELECT ... FOR UPDATE
    (slots in slot_id order, so two writers never deadlock), feasibility is
    re-checked under that lock, then the row is inserted;
  * the partial unique indexes reject any second active claim even if
    application code has a bug (IntegrityError -> SLOT_TAKEN).
A waiting writer gives up after `lock_timeout` and the API returns BUSY.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

from booking.config import get_settings


def connect(url: str | None = None, *, autocommit: bool = True) -> psycopg.Connection:
    s = get_settings()
    conn = psycopg.connect(url or s.database_url, autocommit=autocommit, row_factory=dict_row,
                           connect_timeout=10)
    conn.execute(f"SET lock_timeout = {int(s.lock_timeout_ms)}")
    conn.execute("SET TIME ZONE 'Asia/Kolkata'")
    return conn


@contextmanager
def write_tx(conn: psycopg.Connection) -> Iterator[psycopg.Connection]:
    """One write transaction. Commits on success, rolls back on any exception."""
    with conn.transaction():
        yield conn
