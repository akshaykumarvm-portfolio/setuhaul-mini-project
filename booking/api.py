"""FastAPI booking service. Phase 1: health and simulated-clock admin only.

Phase 2 adds feasibility, quotes, holds, requests, confirm/reject, escalate, sweeper.
Run: uvicorn booking.api:app --port 8000
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Iterator

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from booking import cache, clock
from booking.db import connect, write_tx
from booking.db.migrate import migrate


@asynccontextmanager
async def lifespan(app: FastAPI):
    migrate()  # idempotent: create DB + load snapshot once, apply pending migrations
    yield


app = FastAPI(title="SetuHaul booking service", version="0.1.0", lifespan=lifespan)


def db() -> Iterator:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@app.get("/health")
def health(conn=Depends(db)):
    return {
        "status": "ok",
        "database": conn.info.dbname,
        "migrations": [r["name"] for r in conn.execute("SELECT name FROM schema_migrations ORDER BY name")],
        "sim_now": clock.fmt_ts(clock.now(conn)),
        "redis": cache.ping(),
    }


class ClockUpdate(BaseModel):
    sim_ts: str | None = None       # jump to this simulated time
    advance_min: float | None = None
    rate: float | None = None       # 0 freezes the clock


@app.get("/admin/clock")
def get_clock(conn=Depends(db)):
    return {"sim_now": clock.fmt_ts(clock.now(conn))}


@app.post("/admin/clock")
def update_clock(body: ClockUpdate, conn=Depends(db)):
    with write_tx(conn):
        if body.sim_ts:
            t = clock.set_clock(conn, body.sim_ts, body.rate)
        elif body.advance_min is not None:
            t = clock.advance(conn, body.advance_min)
            if body.rate is not None:
                t = clock.set_clock(conn, clock.fmt_ts(t), body.rate)
        elif body.rate is not None:
            t = clock.set_clock(conn, clock.fmt_ts(clock.now(conn)), body.rate)
        else:
            raise HTTPException(400, "give sim_ts, advance_min or rate")
    return {"sim_now": clock.fmt_ts(t)}
