"""Simulated clock (A15: the snapshot has no single 'now').

sim_now = anchor_sim + (real_now - anchor_real) * rate

The anchor lives in the `sim_clock` table so every process (API workers, sweeper,
test clients) agrees on the same time. Every tool takes `now` from here; nothing in
the booking service calls datetime.now() for business logic.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def parse_ts(ts: str) -> datetime:
    """Parse the DB's ISO-8601 '+05:30' text into an aware datetime."""
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=IST)


def fmt_ts(dt: datetime) -> str:
    """Format in the DB's style: 2026-08-04T09:30:00+05:30."""
    return dt.astimezone(IST).replace(microsecond=0).isoformat()


def _real_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_clock(conn: sqlite3.Connection, sim_start: str, rate: float = 1.0) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO sim_clock (id, anchor_sim, anchor_real, rate) VALUES (1, ?, ?, ?)",
        (fmt_ts(parse_ts(sim_start)), _real_now().isoformat(), rate),
    )


def now(conn: sqlite3.Connection) -> datetime:
    row = conn.execute("SELECT anchor_sim, anchor_real, rate FROM sim_clock WHERE id = 1").fetchone()
    if row is None:
        raise RuntimeError("sim_clock not initialised; run `python -m booking.db.migrate`")
    anchor_sim, anchor_real, rate = parse_ts(row[0]), datetime.fromisoformat(row[1]), float(row[2])
    return (anchor_sim + (_real_now() - anchor_real) * rate).astimezone(IST)


def set_clock(conn: sqlite3.Connection, sim_ts: str, rate: float | None = None) -> datetime:
    cur_rate = conn.execute("SELECT rate FROM sim_clock WHERE id = 1").fetchone()
    r = rate if rate is not None else (float(cur_rate[0]) if cur_rate else 1.0)
    conn.execute(
        "INSERT INTO sim_clock (id, anchor_sim, anchor_real, rate) VALUES (1, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET anchor_sim = excluded.anchor_sim, "
        "anchor_real = excluded.anchor_real, rate = excluded.rate",
        (fmt_ts(parse_ts(sim_ts)), _real_now().isoformat(), r),
    )
    return now(conn)


def advance(conn: sqlite3.Connection, minutes: float) -> datetime:
    return set_clock(conn, fmt_ts(now(conn) + timedelta(minutes=minutes)))
