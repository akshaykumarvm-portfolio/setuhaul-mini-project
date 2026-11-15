"""Create the SetuHaul database, load the supplied snapshot, apply migrations.

    python -m booking.db.migrate            # create DB if missing, load snapshot once, migrate
    python -m booking.db.migrate --reset    # drop the DB and start again from the snapshot

The snapshot comes from SNAPSHOT_URI (s3://... in AWS, a local path in dev) and is
read into memory only. The database uses the C collation so ISO-8601 TEXT
timestamps compare exactly in time order.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from booking import clock
from booking.config import get_settings
from booking.db import connect
from booking.db.snapshot import load, read_snapshot

MIGRATIONS = Path(__file__).parent / "migrations"


def _admin_url(url: str) -> tuple[str, str]:
    params = conninfo_to_dict(url)
    dbname = params.get("dbname") or "setuhaul"
    return make_conninfo(url, dbname="postgres"), dbname


def create_database(url: str, *, reset: bool = False, template: str | None = None) -> bool:
    """Create the database if it doesn't exist. Returns True if it was created."""
    admin_url, dbname = _admin_url(url)
    with psycopg.connect(admin_url, autocommit=True) as admin:
        exists = admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)).fetchone()
        if exists and reset:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(dbname)))
            exists = None
        if exists:
            return False
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE {} ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'")
                      .format(sql.Identifier(dbname), sql.Identifier(template or "template0")))
        return True


def apply_migrations(conn: psycopg.Connection) -> list[str]:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now())")
    done = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations")}
    applied = []
    for f in sorted(MIGRATIONS.glob("*.sql")):
        if f.name in done:
            continue
        with conn.transaction():
            conn.execute(f.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (f.name,))
        applied.append(f.name)
    return applied


def migrate(url: str | None = None, *, reset: bool = False) -> dict:
    s = get_settings()
    url = url or s.database_url
    create_database(url, reset=reset)
    with connect(url) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now())")
        loaded = None
        if not conn.execute("SELECT 1 FROM schema_migrations WHERE name = '000_snapshot'").fetchone():
            with conn.transaction():
                loaded = load(conn, read_snapshot(s.snapshot_uri, s.aws_region))
                conn.execute("INSERT INTO schema_migrations (name) VALUES ('000_snapshot')")
        applied = apply_migrations(conn)
        clock.ensure_clock(conn, s.sim_start, s.sim_rate)
    return {"loaded_rows": loaded, "applied": applied}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--reset", action="store_true")
    a = p.parse_args()
    out = migrate(reset=a.reset)
    rows = out["loaded_rows"]
    print(f"snapshot: {'loaded ' + str(sum(rows.values())) + ' rows' if rows else 'already loaded'}; "
          f"migrations applied: {out['applied'] or 'none pending'}")
