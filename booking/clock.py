"""Simulated clock (A15: the snapshot has no single 'now').

sim_now = anchor_sim + (real_now - anchor_real) * rate, evaluated inside Postgres
with clock_timestamp(), so the API, the sweeper and test clients all agree.
Every tool takes `now` from here; business logic never calls datetime.now().
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg

IST = ZoneInfo("Asia/Kolkata")
_NOW_SQL = "SELECT anchor_sim + (clock_timestamp() - anchor_real) * rate AS t FROM sim_clock WHERE id = 1"


def parse_ts(ts: str) -> datetime:
    """Parse the DB's ISO-8601 '+05:30' text into an aware datetime."""
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=IST)


def fmt_ts(dt: datetime) -> str:
    """Format like the supplied data: 2026-08-04T09:30:00+05:30."""
    return dt.astimezone(IST).replace(microsecond=0).isoformat()


def ensure_clock(conn: psycopg.Connection, sim_start: str, rate: float = 1.0) -> None:
    conn.execute("INSERT INTO sim_clock (id, anchor_sim, rate) VALUES (1, %s, %s) ON CONFLICT (id) DO NOTHING",
                 (parse_ts(sim_start), rate))


def now(conn: psycopg.Connection) -> datetime:
    row = conn.execute(_NOW_SQL).fetchone()
    if row is None:
        raise RuntimeError("sim_clock not initialised; run `python -m booking.db.migrate`")
    return row["t"].astimezone(IST)


def set_clock(conn: psycopg.Connection, sim_ts: str | datetime, rate: float | None = None) -> datetime:
    t = parse_ts(sim_ts) if isinstance(sim_ts, str) else sim_ts
    conn.execute(
        "INSERT INTO sim_clock (id, anchor_sim, anchor_real, rate) VALUES (1, %(t)s, clock_timestamp(), "
        " COALESCE(%(r)s, 1.0)) ON CONFLICT (id) DO UPDATE SET anchor_sim = EXCLUDED.anchor_sim, "
        " anchor_real = EXCLUDED.anchor_real, rate = COALESCE(%(r)s, sim_clock.rate)",
        {"t": t, "r": rate},
    )
    return now(conn)


def advance(conn: psycopg.Connection, minutes: float) -> datetime:
    return set_clock(conn, now(conn) + timedelta(minutes=minutes))
