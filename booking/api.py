"""FastAPI booking service: the only component that changes capacity.

Run: uvicorn booking.api:app --port 8000   (docs at /docs)
Business refusals return HTTP 409 (404 for unknown ids) with {"ok": false, "code": ...};
a lock wait over 5 s returns 503 {"code": "BUSY"}. Every write takes an optional
Idempotency-Key header so retries never double-book.

Access (booking/auth.py): drivers act only on their own loads (driver id from the token),
coordinators confirm/reject and see hand-offs for their facility, planners also run the
clock and sweeper, the agent uses a service key. Behind CloudFront every path is also
served under /api.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from typing import Iterator

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from booking import bookings, cache, clock, handoffs, holds, quotes, service, sweeper, threads
from booking.agent_gateway import invoke_agent
from booking.auth import Principal, auth_mode, check_facility, get_principal, require_planner, require_staff
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


app = FastAPI(title="SetuHaul booking service", version="0.4.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:5173").split(","),
                   allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def strip_api_prefix(request: Request, call_next):
    """CloudFront routes /api/* here; serve the same routes with or without the prefix."""
    path = request.scope["path"]
    if path == "/api" or path.startswith("/api/"):
        request.scope["path"] = path[4:] or "/"
    return await call_next(request)


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


Who = Depends(get_principal)


def scope(p: Principal) -> str | None:
    """The driver id a request is limited to (None = staff, not limited)."""
    if p.kind in ("driver", "agent"):
        if not p.driver_id:
            raise HTTPException(401, "no driver identity on this request")
        return p.driver_id
    if p.kind == "dev":
        return p.driver_id
    return None


def _owner_of(conn, *, shipment_id=None, quote_id=None, hold_id=None, appointment_id=None) -> str | None:
    if quote_id:
        r = conn.execute("SELECT shipment_id FROM slot_quotes WHERE quote_id = %s", (quote_id,)).fetchone()
        shipment_id = r and r["shipment_id"]
    if hold_id:
        r = conn.execute("SELECT shipment_id FROM slot_holds WHERE hold_id = %s", (hold_id,)).fetchone()
        shipment_id = r and r["shipment_id"]
    if appointment_id:
        r = conn.execute("SELECT shipment_id FROM appointments WHERE appointment_id = %s", (appointment_id,)).fetchone()
        shipment_id = r and r["shipment_id"]
    if not shipment_id:
        return None
    r = conn.execute("SELECT driver_id FROM shipments WHERE shipment_id = %s", (shipment_id,)).fetchone()
    return r and r["driver_id"]


def forbid(conn, driver_id: str | None, **ids) -> JSONResponse | None:
    if driver_id is None:
        return None
    owner = _owner_of(conn, **ids)
    if owner is not None and owner != driver_id:
        return JSONResponse({"ok": False, "code": "NOT_YOURS", "message": "That load is not assigned to you."},
                            status_code=403)
    return None


def _facility_of_appointment(conn, appointment_id: str) -> str | None:
    r = conn.execute("SELECT s.destination_facility_id AS f FROM appointments a JOIN shipments s USING (shipment_id)"
                     " WHERE a.appointment_id = %s", (appointment_id,)).fetchone()
    return r and r["f"]


# ---------- health, identity, simulated clock ----------

@app.get("/health")
def health(conn=Depends(db)):
    return {
        "status": "ok",
        "database": conn.info.dbname,
        "migrations": [r["name"] for r in conn.execute("SELECT name FROM schema_migrations ORDER BY name")],
        "sim_now": clock.fmt_ts(clock.now(conn)),
        "redis": cache.ping(),
        "auth": auth_mode(),
    }


@app.get("/me")
def me(p: Principal = Who, conn=Depends(db)):
    out = {"kind": p.kind, "username": p.username, "driver_id": p.driver_id, "facility_id": p.facility_id,
           "groups": p.groups, "sim_now": clock.fmt_ts(clock.now(conn))}
    if p.driver_id:
        ctx = service.driver_context(conn, p.driver_id)
        out["driver"] = ctx.get("driver")
        out["shipments"] = ctx.get("shipments")
    return out


class ClockUpdate(BaseModel):
    sim_ts: str | None = None       # jump to this simulated time
    advance_min: float | None = None
    rate: float | None = None       # 0 freezes the clock


@app.get("/admin/clock")
def get_clock(conn=Depends(db), p: Principal = Who):
    return {"sim_now": clock.fmt_ts(clock.now(conn))}


@app.post("/admin/clock")
def update_clock(body: ClockUpdate, conn=Depends(db), p: Principal = Who):
    require_planner(p)
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


@app.post("/admin/reset")
def reset_demo(p: Principal = Who):
    """Planner only: reload the supplied snapshot (from S3 in AWS) and freeze the clock at 10:00."""
    require_planner(p)
    out = migrate(reset=True)
    conn = connect()
    try:
        with write_tx(conn):
            t = clock.set_clock(conn, "2026-08-04T10:00:00+05:30", rate=0)
    finally:
        conn.close()
    return {"ok": True, "loaded_rows": sum((out["loaded_rows"] or {}).values()), "sim_now": clock.fmt_ts(t)}


@app.post("/admin/sweep")
def run_sweep(conn=Depends(db), p: Principal = Who):
    require_staff(p)
    return sweeper.sweep(conn)


# ---------- driver chat (browser -> this service -> agent) ----------

class ChatReq(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    thread_id: str | None = None
    message_id: str | None = None
    driver_id: str | None = None    # honoured only when AUTH_MODE=off (local dev)


@app.post("/chat")
def chat(body: ChatReq, p: Principal = Who):
    driver_id = p.driver_id if p.kind == "driver" else (body.driver_id or p.driver_id if p.kind == "dev" else None)
    if not driver_id:
        raise HTTPException(403, "chat is for driver accounts")
    thread_id = body.thread_id or f"CHAT-{driver_id}-{uuid.uuid4().hex[:8]}"
    return invoke_agent(driver_id, body.text, thread_id, body.message_id or uuid.uuid4().hex, p.token)


# ---------- reads ----------

@app.get("/drivers/{driver_id}/context")
def driver_context(driver_id: str, conn=Depends(db), p: Principal = Who):
    caller = scope(p)
    if caller and caller != driver_id:
        return JSONResponse({"ok": False, "code": "NOT_YOURS"}, status_code=403)
    return respond(service.driver_context(conn, driver_id))


@app.get("/shipments/{shipment_id}/status")
def shipment_status(shipment_id: str, conn=Depends(db), p: Principal = Who):
    return forbid(conn, scope(p), shipment_id=shipment_id) or respond(service.shipment_status(conn, shipment_id))


@app.get("/shipments/{shipment_id}/feasibility")
def feasibility(shipment_id: str, slot_ids: str, latest_finish: str | None = None, conn=Depends(db),
                p: Principal = Who):
    require_staff(p)
    v = check_feasibility(conn, shipment_id, slot_ids.split(","), clock.now(conn), latest_finish=_ts(latest_finish))
    return v.as_json()


@app.get("/facilities/{facility_id}/timeline")
def timeline(facility_id: str, conn=Depends(db), p: Principal = Who):
    """Dock timeline for the ops console: every slot with who holds or books it."""
    require_staff(p)
    check_facility(p, facility_id)
    now = clock.fmt_ts(clock.now(conn))
    rows = conn.execute(
        "SELECT sl.slot_id, d.dock_code, d.dock_type, d.max_vehicle_weight_kg, sl.slot_start_ts, sl.slot_end_ts,"
        " sl.slot_status, sl.block_reason, sl.version, a.appointment_id, a.shipment_id, a.appointment_status,"
        " sh.priority_code, sh.expected_unload_min, h.hold_id, h.shipment_id AS held_by, h.expires_at"
        " FROM appointment_slots sl JOIN docks d ON d.dock_id = sl.dock_id"
        " LEFT JOIN appointment_slot_claims c ON c.slot_id = sl.slot_id AND c.active = 1"
        "      AND EXISTS (SELECT 1 FROM appointments x WHERE x.appointment_id = c.appointment_id"
        "                  AND x.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS'))"
        " LEFT JOIN appointments a ON a.appointment_id = c.appointment_id"
        " LEFT JOIN shipments sh ON sh.shipment_id = a.shipment_id"
        " LEFT JOIN slot_hold_claims hc ON hc.slot_id = sl.slot_id AND hc.active = 1"
        " LEFT JOIN slot_holds h ON h.hold_id = hc.hold_id AND h.state = 'ACTIVE' AND h.expires_at > %s"
        " WHERE sl.facility_id = %s ORDER BY d.dock_code, sl.slot_start_ts", (now, facility_id)).fetchall()
    events = conn.execute("SELECT e.*, d.dock_code FROM dock_status_events e JOIN docks d USING (dock_id)"
                          " WHERE d.facility_id = %s", (facility_id,)).fetchall()
    docks = conn.execute("SELECT dock_id, dock_code, dock_type, max_vehicle_weight_kg FROM docks WHERE facility_id = %s"
                         " ORDER BY dock_code", (facility_id,)).fetchall()
    return {"sim_now": now, "docks": docks, "slots": rows, "dock_events": events}


@app.get("/facilities/{facility_id}/proposal")
def facility_proposal(facility_id: str, time_limit: float = 5.0, conn=Depends(db), p: Principal = Who):
    """Current promises vs dispatch-rule baseline vs CP-SAT for every truck still to unload. Never commits."""
    require_staff(p)
    check_facility(p, facility_id)
    from booking.scheduling import propose
    return propose(conn, facility_id, clock.now(conn), time_limit_s=min(max(time_limit, 1.0), 20.0))


@app.get("/shipments/{shipment_id}/proposal")
def shipment_proposal(shipment_id: str, conn=Depends(db), p: Principal = Who):
    """One truck's line of the recommended facility plan (no other trucks' data). Never commits."""
    if denied := forbid(conn, scope(p), shipment_id=shipment_id):
        return denied
    from booking.scheduling import proposal_for
    return respond(proposal_for(conn, shipment_id, clock.now(conn)))


@app.get("/bookings")
def list_bookings(status: str = "PENDING_CONFIRMATION", facility_id: str | None = None, conn=Depends(db),
                  p: Principal = Who):
    require_staff(p)
    facility_id = facility_id or (p.facility_id if p.kind == "coordinator" else None)
    check_facility(p, facility_id)
    rows = conn.execute(
        "SELECT a.*, s.destination_facility_id, s.priority_code, s.driver_id, s.load_weight_kg, s.order_reference"
        " FROM appointments a JOIN shipments s USING (shipment_id) WHERE a.appointment_status = %(st)s"
        " AND (%(f)s::text IS NULL OR s.destination_facility_id = %(f)s) ORDER BY a.booked_at",
        {"st": status, "f": facility_id}).fetchall()
    out = []
    for a in rows:
        view = service._appt_view(conn, a) | {"shipment_id": a["shipment_id"], "priority": a["priority_code"],
                                              "driver_id": a["driver_id"], "booked_at": a["booked_at"],
                                              "replaces": a["replaced_appointment_id"],
                                              "facility_id": a["destination_facility_id"],
                                              "order_reference": a["order_reference"],
                                              "load_weight_kg": a["load_weight_kg"],
                                              "source": a["booking_source"]}
        if a["appointment_status"] == "PENDING_CONFIRMATION":
            view["expires_at"] = clock.fmt_ts(bookings.pending_expiry(conn, a))
            if a["replaced_appointment_id"]:
                view["replaces_view"] = service._appt_view(conn, conn.execute(
                    "SELECT * FROM appointments WHERE appointment_id = %s", (a["replaced_appointment_id"],)).fetchone())
        out.append(view)
    return {"bookings": out}


@app.get("/handoffs")
def get_handoffs(state: str | None = "OPEN", facility_id: str | None = None, conn=Depends(db), p: Principal = Who):
    require_staff(p)
    facility_id = facility_id or (p.facility_id if p.kind == "coordinator" else None)
    check_facility(p, facility_id)
    return {"handoffs": handoffs.list_handoffs(conn, state, facility_id)}


@app.get("/events/{facility_id}")
async def events(facility_id: str, p: Principal = Who):
    """Server-sent events for the ops console, fed by Redis pub/sub (slot held, freed, booked, hand-off)."""
    require_staff(p)
    check_facility(p, facility_id)

    async def stream():
        yield "retry: 5000\n\n"
        r = cache.get_redis()
        if r is None:
            while True:                     # no Redis: heartbeat only; the console also polls
                yield ": no-redis\n\n"
                await asyncio.sleep(15)
        ps = r.pubsub(ignore_subscribe_messages=True)
        await asyncio.to_thread(ps.subscribe, f"facility:{facility_id}")
        try:
            idle = 0.0
            while True:
                msg = await asyncio.to_thread(ps.get_message, timeout=1.0)
                if msg and msg.get("type") == "message":
                    yield f"data: {msg['data']}\n\n"
                    idle = 0.0
                else:
                    idle += 1.0
                    if idle >= 15:
                        yield ": ping\n\n"
                        idle = 0.0
        finally:
            await asyncio.to_thread(ps.close)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------- writes ----------

class QuoteReq(BaseModel):
    shipment_id: str
    thread_id: str | None = None
    latest_finish: str | None = None
    after: str | None = None


@app.post("/quotes")
def create_quote(body: QuoteReq, conn=Depends(db), p: Principal = Who):
    if denied := forbid(conn, scope(p), shipment_id=body.shipment_id):
        return denied
    with write_tx(conn):
        r = quotes.build_quote(conn, body.shipment_id, clock.now(conn), thread_id=body.thread_id,
                               latest_finish=_ts(body.latest_finish), after=_ts(body.after))
    return respond(r)


class HoldReq(BaseModel):
    quote_id: str
    option_no: int = Field(ge=1)


@app.post("/holds")
def create_hold(body: HoldReq, conn=Depends(db), idempotency_key: str | None = Header(None), p: Principal = Who):
    return forbid(conn, scope(p), quote_id=body.quote_id) or respond(
        holds.hold_option(conn, body.quote_id, body.option_no, idem_key=idempotency_key, actor=p.actor))


@app.delete("/holds/{hold_id}")
def delete_hold(hold_id: str, conn=Depends(db), p: Principal = Who):
    return forbid(conn, scope(p), hold_id=hold_id) or respond(holds.release_hold(conn, hold_id, actor=p.actor))


class BookingReq(BaseModel):
    hold_id: str


@app.post("/bookings")
def create_booking(body: BookingReq, conn=Depends(db), idempotency_key: str | None = Header(None), p: Principal = Who):
    return forbid(conn, scope(p), hold_id=body.hold_id) or respond(
        bookings.request_booking(conn, body.hold_id, idem_key=idempotency_key, actor=p.actor))


class ConfirmReq(BaseModel):
    actor: str = "coordinator"
    confirmation_ref: str | None = None


@app.post("/bookings/{appointment_id}/confirm")
def confirm_booking(appointment_id: str, body: ConfirmReq | None = None, conn=Depends(db), p: Principal = Who):
    require_staff(p)
    check_facility(p, _facility_of_appointment(conn, appointment_id))
    body = body or ConfirmReq()
    actor = body.actor if p.kind == "dev" else p.actor
    return respond(bookings.confirm(conn, appointment_id, actor=actor, confirmation_ref=body.confirmation_ref))


class RejectReq(BaseModel):
    reason: str
    actor: str = "coordinator"


@app.post("/bookings/{appointment_id}/reject")
def reject_booking(appointment_id: str, body: RejectReq, conn=Depends(db), p: Principal = Who):
    require_staff(p)
    check_facility(p, _facility_of_appointment(conn, appointment_id))
    actor = body.actor if p.kind == "dev" else p.actor
    return respond(bookings.reject(conn, appointment_id, body.reason, actor=actor))


class EtaReq(BaseModel):
    shipment_id: str
    eta_lo: str
    eta_hi: str
    confidence: str
    cause: str | None = None
    cause_fixed: bool | None = None
    source_text: str = ""
    thread_id: str | None = None


@app.post("/eta")
def post_eta(body: EtaReq, conn=Depends(db), p: Principal = Who):
    driver_id = scope(p)
    if denied := forbid(conn, driver_id, shipment_id=body.shipment_id):
        return denied
    return respond(service.record_eta(conn, body.shipment_id, body.eta_lo, body.eta_hi, body.confidence,
                                      cause=body.cause, cause_fixed=body.cause_fixed, source_text=body.source_text,
                                      driver_id=driver_id, thread_id=body.thread_id, actor=p.actor))


class EscalateReq(BaseModel):
    shipment_id: str
    reason_code: str
    summary: str
    thread_id: str | None = None
    latest_finish: str | None = None


@app.post("/handoffs")
def post_handoff(body: EscalateReq, conn=Depends(db), p: Principal = Who):
    if denied := forbid(conn, scope(p), shipment_id=body.shipment_id):
        return denied
    return respond(handoffs.escalate(conn, body.shipment_id, body.reason_code, body.summary, thread_id=body.thread_id,
                                     latest_finish=_ts(body.latest_finish), actor=p.actor))


class CloseReq(BaseModel):
    resolution: str
    actor: str = "coordinator"


@app.post("/handoffs/{handoff_id}/close")
def close(handoff_id: str, body: CloseReq, conn=Depends(db), p: Principal = Who):
    require_staff(p)
    r = conn.execute("SELECT s.destination_facility_id AS f FROM handoffs h JOIN shipments s USING (shipment_id)"
                     " WHERE h.handoff_id = %s", (handoff_id,)).fetchone()
    check_facility(p, r and r["f"])
    actor = body.actor if p.kind == "dev" else p.actor
    return respond(handoffs.close_handoff(conn, handoff_id, body.resolution, actor=actor))


# ---------- conversation records (agent intake) ----------

class ThreadReq(BaseModel):
    driver_id: str
    thread_id: str | None = None
    intent: str = "UNKNOWN"


@app.post("/threads")
def post_thread(body: ThreadReq, conn=Depends(db), p: Principal = Who):
    caller = scope(p)
    if caller and caller != body.driver_id:
        return JSONResponse({"ok": False, "code": "NOT_YOURS"}, status_code=403)
    return respond(threads.open_thread(conn, body.driver_id, body.thread_id, body.intent))


class MessageReq(BaseModel):
    sender_type: str = Field(pattern="^(DRIVER|AGENT|OPERATIONS|WAREHOUSE|SYSTEM)$")
    text: str
    external_message_id: str | None = None
    sender_reference: str | None = None
    parsed_intent: str | None = None
    extracted_eta_ts: str | None = None
    requires_human_review: bool = False


def _thread_driver(conn, thread_id: str) -> str | None:
    r = conn.execute("SELECT driver_id FROM chat_threads WHERE thread_id = %s", (thread_id,)).fetchone()
    return r and r["driver_id"]


@app.post("/threads/{thread_id}/messages")
def post_message(thread_id: str, body: MessageReq, conn=Depends(db), p: Principal = Who):
    caller = scope(p)
    if p.kind == "driver":
        raise HTTPException(403, "drivers send messages through /chat")
    if caller and _thread_driver(conn, thread_id) not in (None, caller):
        return JSONResponse({"ok": False, "code": "NOT_YOURS"}, status_code=403)
    return respond(threads.add_message(conn, thread_id, body.sender_type, body.text,
                                       external_message_id=body.external_message_id,
                                       sender_reference=body.sender_reference, parsed_intent=body.parsed_intent,
                                       extracted_eta_ts=body.extracted_eta_ts,
                                       requires_human_review=body.requires_human_review))


@app.get("/threads/{thread_id}/messages")
def get_messages(thread_id: str, conn=Depends(db), p: Principal = Who):
    caller = scope(p)
    if caller and _thread_driver(conn, thread_id) not in (None, caller):
        return JSONResponse({"ok": False, "code": "NOT_YOURS"}, status_code=403)
    return {"messages": threads.transcript(conn, thread_id)}


class LinkReq(BaseModel):
    driver_id: str
    shipment_id: str


@app.post("/threads/{thread_id}/link")
def post_link(thread_id: str, body: LinkReq, conn=Depends(db), p: Principal = Who):
    caller = scope(p)
    if caller and caller != body.driver_id:
        return JSONResponse({"ok": False, "code": "NOT_YOURS"}, status_code=403)
    return respond(threads.link_shipment(conn, thread_id, body.driver_id, body.shipment_id))
