"""Create the working DB copy and apply migrations.

    python -m booking.db.migrate            # copy if missing, then migrate
    python -m booking.db.migrate --reset    # discard the copy and start from the snapshot

The supplied snapshot is opened read-only and never written.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from booking import clock
from booking.config import get_settings
from booking.db import connect

MIGRATIONS = Path(__file__).parent / "migrations"


def copy_snapshot(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(dst)
    try:
        source.backup(target)
    finally:
        source.close()
        target.close()


def apply_migrations(conn: sqlite3.Connection) -> list[str]:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT)")
    done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
    applied = []
    for f in sorted(MIGRATIONS.glob("*.sql")):
        if f.name in done:
            continue
        sql = f.read_text(encoding="utf-8")
        name = f.name.replace("'", "''")
        conn.executescript(
            f"BEGIN IMMEDIATE;\n{sql}\nINSERT INTO schema_migrations VALUES ('{name}', datetime('now'));\nCOMMIT;"
        )
        applied.append(f.name)
    return applied


def migrate(db_path: Path | None = None, reset: bool = False) -> Path:
    s = get_settings()
    dst = Path(db_path) if db_path else s.db_path
    if reset:
        for suffix in ("", "-wal", "-shm"):
            Path(str(dst) + suffix).unlink(missing_ok=True)
    if not dst.exists():
        copy_snapshot(s.source_db, dst)
    conn = connect(dst)
    try:
        apply_migrations(conn)
        clock.ensure_clock(conn, s.sim_start, s.sim_rate)
    finally:
        conn.close()
    return dst


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--reset", action="store_true")
    p.add_argument("--db", type=Path)
    a = p.parse_args()
    print(f"working DB ready: {migrate(a.db, a.reset)}")
