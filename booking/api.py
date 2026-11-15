"""FastAPI booking service: the only component that changes capacity.

Run: uvicorn booking.api:app --port 8000   (docs at /docs)
Business refusals return HTTP 409 (404 for unknown ids) with {"ok": false, "code": ...};
a lock wait over 5 s returns 503 {"code": "BUSY"}. Every write takes an optional
Idempotency-Key header so retries never double-book.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Iterator

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from booking import bookings, cache, clock, handoffs, holds, quotes, service, sweeper
from booking.config import get_settings
from booking.db import connect, write_tx
from booking.db.migrate import migrate
from booking.feasibility import check_feasibility


@asynccontextmanager
async def lifespan(app: FastAPI):
    migrate()  # idempotent: create DB + load snapshot once, apply pending migrations
    task = None
    if os.getenv("SWEEPER_ENABLED", "1") != "0":
        task = asyncio.create_task(sweeper.run_forever(connect, get_settings().sweeper_interval_s))
    yield
    if task:
        task.cancel()


app = FastAPI(title="SetuHaul booking service", version="0.2.0", lifespan=lifespan)


@app.exception_handler(psycopg.errors.LockNotAvailable)
async def busy(_: Request, __: Exception):
    return JSONResponse({"ok": False, "code": "BUSY", "message": "Busy, retry once."}, status_code=503)


def db() -> Iterator[psycopg.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def respond(result: dict) -> JSONResponse:
    if result.get("ok"):
        return JSONResponse(result)
    code = result.get("code", "")
    status = 404 if code.endswith("NOT_FOUND") or code.startswith("UNKNOWN") else 409
    return JSONResponse(result, status_code=status)


def _ts(v: str | None):
    return clock.parse_ts(v) if v else None


# ---------- health and simulated clock ----------

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
                t = clock.set_clock(conn, t, body.rate)
        elif body.rate is not None:
            t = clock.set_clock(conn, clock.now(conn), body.rate)
        else:
            raise HTTPException(400, "give sim_ts, advance_min or rate")
    return {"sim_now": clock.fmt_ts(t)}


@app.post("/admin/sweep")
def run_sweep(conn=Depends(db)):
    return sweeper.sweep(conn)


# ---------- reads ----------

@app.get("/drivers/{driver_id}/context")
def driver_context(driver_id: str, conn=Depends(db)):
    return respond(service.driver_context(conn, driver_id))


@app.get("/shipments/{shipment_id}/status")
def shipment_status(shipment_id: str, conn=Depends(db)):
    return respond(service.shipment_status(conn, shipment_id))


@app.get("/shipments/{shipment_id}/feasibility")
def feasibility(shipment_id: str, slot_ids: str, latest_finish: str | None = None, conn=Depends(db)):
    v = check_feasibility(conn, shipment_id, slot_ids.split(","), clock.now(conn), latest_finish=_ts(latest_finish))
    return v.as_json()


@app.get("/facilities/{facility_id}/timeline")
def timeline(facility_id: str, conn=Depends(db)):
    """Dock timeline for the ops console: every slot with who holds or books it."""
    now = clock.fmt_ts(clock.now(conn))
    rows = conn.execute(
        "SELECT sl.slot_id, d.dock_code, d.dock_type, sl.slot_start_ts, sl.slot_end_ts, sl.slot_status, sl.version,"
        " a.appointment_id, a.shipment_id, a.appointment_status, h.hold_id, h.shipment_id AS held_by, h.expires_at"
        " FROM appointment_slots sl JOIN docks d ON d.dock_id = sl.dock_id"
        " LEFT JOIN appointment_slot_claims c ON c.slot_id = sl.slot_id AND c.active = 1"
        " LEFT JOIN appointments a ON a.appointment_id = c.appointment_id"
        "      AND a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')"
        " LEFT JOIN slot_hold_claims hc ON hc.slot_id = sl.slot_id AND hc.active = 1"
        " LEFT JOIN slot_holds h ON h.hold_id = hc.hold_id AND h.state = 'ACTIVE' AND h.expires_at > %s"
        " WHERE sl.facility_id = %s ORDER BY d.dock_code, sl.slot_start_ts", (now, facility_id)).fetchall()
    events = conn.execute("SELECT e.*, d.dock_code FROM dock_status_events e JOIN docks d USING (dock_id)"
                          " WHERE d.facility_id = %s", (facility_id,)).fetchall()
    return {"sim_now": now, "slots": rows, "dock_events": events}


@app.get("/bookings")
def list_bookings(status: str = "PENDING_CONFIRMATION", facility_id: str | None = None, conn=Depends(db)):
    rows = conn.execute(
        "SELECT a.*, s.destination_facility_id, s.priority_code, s.driver_id FROM appointments a"
        " JOIN shipments s USING (shipment_id) WHERE a.appointment_status = %(st)s"
        " AND (%(f)s::text IS NULL OR s.destination_facility_id = %(f)s) ORDER BY a.booked_at",
        {"st": status, "f": facility_id}).fetchall()
    out = []
    for a in rows:
        view = service._appt_view(conn, a) | {"shipment_id": a["shipment_id"], "priority": a["priority_code"],
                                              "driver_id": a["driver_id"], "booked_at": a["booked_at"],
                                              "replaces": a["replaced_appointment_id"]}
        if a["appointment_status"] == "PENDING_CONFIRMATION":
            view["expires_at"] = clock.fmt_ts(bookings.pending_expiry(conn, a))
        out.append(view)
    return {"bookings": out}


@app.get("/handoffs")
def get_handoffs(state: str | None = "OPEN", facility_id: str | None = None, conn=Depends(db)):
    return {"handoffs": handoffs.list_handoffs(conn, state, facility_id)}


# ---------- writes ----------

class QuoteReq(BaseModel):
    shipment_id: str
    thread_id: str | None = None
    latest_finish: str | None = None
    after: str | None = None


@app.post("/quotes")
def create_quote(body: QuoteReq, conn=Depends(db)):
    with write_tx(conn):
        r = quotes.build_quote(conn, body.shipment_id, clock.now(conn), thread_id=body.thread_id,
                               latest_finish=_ts(body.latest_finish), after=_ts(body.after))
    return respond(r)


class HoldReq(BaseModel):
    quote_id: str
    option_no: int = Field(ge=1)


@app.post("/holds")
def create_hold(body: HoldReq, conn=Depends(db), idempotency_key: str | None = Header(None)):
    return respond(holds.hold_option(conn, body.quote_id, body.option_no, idem_key=idempotency_key))


@app.delete("/holds/{hold_id}")
def delete_hold(hold_id: str, conn=Depends(db)):
    return respond(holds.release_hold(conn, hold_id))


class BookingReq(BaseModel):
    hold_id: str


@app.post("/bookings")
def create_booking(body: BookingReq, conn=Depends(db), idempotency_key: str | None = Header(None)):
    return respond(bookings.request_booking(conn, body.hold_id, idem_key=idempotency_key))


class ConfirmReq(BaseModel):
    actor: str = "coordinator"
    confirmation_ref: str | None = None


@app.post("/bookings/{appointment_id}/confirm")
def confirm_booking(appointment_id: str, body: ConfirmReq | None = None, conn=Depends(db)):
    body = body or ConfirmReq()
    return respond(bookings.confirm(conn, appointment_id, actor=body.actor, confirmation_ref=body.confirmation_ref))


class RejectReq(BaseModel):
    reason: str
    actor: str = "coordinator"


@app.post("/bookings/{appointment_id}/reject")
def reject_booking(appointment_id: str, body: RejectReq, conn=Depends(db)):
    return respond(bookings.reject(conn, appointment_id, body.reason, actor=body.actor))


class EtaReq(BaseModel):
    shipment_id: str
    eta_lo: str
    eta_hi: str
    confidence: str
    cause: str | None = None
    cause_fixed: bool | None = None
    source_text: str = ""


@app.post("/eta")
def post_eta(body: EtaReq, conn=Depends(db)):
    return respond(service.record_eta(conn, body.shipment_id, body.eta_lo, body.eta_hi, body.confidence,
                                      cause=body.cause, cause_fixed=body.cause_fixed, source_text=body.source_text))


class EscalateReq(BaseModel):
    shipment_id: str
    reason_code: str
    summary: str
    thread_id: str | None = None
    latest_finish: str | None = None


@app.post("/handoffs")
def post_handoff(body: EscalateReq, conn=Depends(db)):
    return respond(handoffs.escalate(conn, body.shipment_id, body.reason_code, body.summary, thread_id=body.thread_id,
                                     latest_finish=_ts(body.latest_finish)))


class CloseReq(BaseModel):
    resolution: str
    actor: str = "coordinator"


@app.post("/handoffs/{handoff_id}/close")
def close(handoff_id: str, body: CloseReq, conn=Depends(db)):
    return respond(handoffs.close_handoff(conn, handoff_id, body.resolution, actor=body.actor))
