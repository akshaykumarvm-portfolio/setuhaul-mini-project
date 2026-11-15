"""Hand-offs to a coordinator (Decisions 10 and 12). Code builds the record; a person owns the outcome.

Record fields: shipment, driver, ETA window + confidence, driver constraints, every
compatible dock with its rejection reason, the nearest out-of-policy option, the
contact it was routed to and the channel status. A failed or missing email is never
treated as delivered (A13): the console is the primary channel.
"""
from __future__ import annotations

import json
from datetime import datetime

from booking import clock
from booking.clock import fmt_ts
from booking.common import audit, dumps, err, new_id, ok, publish
from booking.feasibility import evaluate, find_options, load_state, candidate_runs

REASON_CODES = {"NO_FEASIBLE_SLOT", "CONTRADICTORY_DATA", "SAFETY", "CAUSE_NOT_FIXED", "PENALTY_OR_CUSTOMER",
                "DISPUTE", "CLARIFICATION_FAILED", "REQUEST_REJECTED", "PENDING_LAPSED", "APPROVAL_NEEDED",
                "DRIVER_REQUEST", "OTHER"}


def _route(conn, facility_id: str, now: datetime) -> tuple[dict | None, str]:
    night = now.hour >= 21 or now.hour < 7
    prefs = (["NIGHT_SHIFT_COORDINATOR", "SHIFT_MANAGER", "WAREHOUSE_COORDINATOR"] if night
             else ["SHIFT_MANAGER", "WAREHOUSE_COORDINATOR"])
    contacts = {c["contact_role"]: c for c in conn.execute(
        "SELECT * FROM facility_contacts WHERE facility_id = %s AND active_flag = 1", (facility_id,))}
    for role in prefs:
        if role in contacts:
            c = contacts[role]
            email = f"email {c['email']}: NOT_SENT (console first)" if c["email"] else "email: UNAVAILABLE (no address on file)"
            return dict(c), f"console: QUEUED; {email}; phone {c['phone'] or 'none'}"
    return None, "console: QUEUED; no facility contact on file"


def _nearest_out_of_policy(st) -> dict | None:
    """Best option once the driver's latest-finish limit is dropped (approval flags kept)."""
    opts, _ = find_options(st, limit=1)
    if opts:
        return opts[0] | {"why_out_of_policy": "ignores the driver's latest finish or needs approval"}
    # Nothing today: say when each compatible dock's blocking events end.
    ends = []
    for dock_id, dock in st.docks.items():
        runs = candidate_runs(st, dock_id)
        if runs:
            v = evaluate(st, runs[-1])
            if any(r.code == "DOCK_TYPE" or r.code == "OVERWEIGHT" for r in v.reasons):
                continue
        for e in st.events:
            if e["dock_id"] == dock_id and e["end"]:
                ends.append(f"{dock['dock_code']} free after {e['end']:%H:%M} ({e['dock_event_id']}); no later slots published")
    return {"none_today": True, "notes": ends} if ends else None


def build_record(conn, shipment_id: str, now: datetime, *, latest_finish: datetime | None = None,
                 summary: str = "", reason_code: str = "OTHER") -> dict:
    st = load_state(conn, shipment_id, now)
    sh = st.shipment
    opts, excluded = find_options(st, latest_finish=latest_finish)
    cur = conn.execute("SELECT a.appointment_id, a.appointment_status, sl.slot_start_ts, sl.slot_end_ts, d.dock_code"
                       " FROM appointments a JOIN appointment_slots sl USING (slot_id) JOIN docks d ON d.dock_id = sl.dock_id"
                       " WHERE a.shipment_id = %s AND a.is_current = 1", (shipment_id,)).fetchone()
    driver = conn.execute("SELECT driver_id, driver_name, phone FROM drivers WHERE driver_id = %s",
                          (sh["driver_id"],)).fetchone()
    return {
        "reason_code": reason_code, "summary": summary, "created_at": fmt_ts(now),
        "shipment": {k: sh[k] for k in ("shipment_id", "order_reference", "destination_facility_id", "priority_code",
                                        "load_weight_kg", "required_dock_type", "temperature_control_required",
                                        "expected_unload_min", "current_status", "vehicle_id")},
        "driver": dict(driver),
        "eta": st.eta.as_json(), "earliest_dock_start": fmt_ts(st.earliest_start),
        "driver_constraints": {"latest_finish": fmt_ts(latest_finish) if latest_finish else None},
        "current_appointment": dict(cur) if cur else None,
        "feasible_options": opts,
        "docks_checked": excluded,
        "nearest_out_of_policy": None if opts else _nearest_out_of_policy(st),
        "data_flags": [f.as_json() for f in st.flags],
    }


def create_handoff(conn, shipment_id: str, reason_code: str, summary: str, now: datetime, *,
                   thread_id: str | None = None, latest_finish: datetime | None = None, actor: str = "agent") -> dict:
    """Insert inside the caller's transaction."""
    reason_code = reason_code if reason_code in REASON_CODES else "OTHER"
    record = build_record(conn, shipment_id, now, latest_finish=latest_finish, summary=summary, reason_code=reason_code)
    contact, channel = _route(conn, record["shipment"]["destination_facility_id"], now)
    record["routed_to"] = contact
    record["channel_status"] = channel
    hof_id = new_id("HOF")
    conn.execute("INSERT INTO handoffs (handoff_id, shipment_id, thread_id, reason_code, record_json, routed_to,"
                 " channel_status, state, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,'OPEN',%s)",
                 (hof_id, shipment_id, thread_id, reason_code, dumps(record),
                  f"{contact['contact_role']}:{contact['contact_name']}" if contact else None, channel, fmt_ts(now)))
    conn.execute("UPDATE driver_exceptions SET exception_status = 'ESCALATED' WHERE shipment_id = %s"
                 " AND exception_status IN ('OPEN','NEEDS_INFORMATION','SLOT_OPTIONS_SHARED','WAITING_CONFIRMATION')",
                 (shipment_id,))
    audit(conn, now, actor, "HANDOFF", hof_id, None, {"reason_code": reason_code, "shipment_id": shipment_id})
    return {"handoff_id": hof_id, "routed_to": contact, "channel_status": channel, "record": record}


def escalate(conn, shipment_id: str, reason_code: str, summary: str, *, thread_id: str | None = None,
             latest_finish: datetime | None = None, actor: str = "agent") -> dict:
    with conn.transaction():
        if conn.execute("SELECT 1 FROM shipments WHERE shipment_id = %s", (shipment_id,)).fetchone() is None:
            return err("UNKNOWN_SHIPMENT", shipment_id)
        now = clock.now(conn)
        h = create_handoff(conn, shipment_id, reason_code, summary, now, thread_id=thread_id,
                           latest_finish=latest_finish, actor=actor)
    publish(h["record"]["shipment"]["destination_facility_id"], {"type": "HANDOFF", "handoff_id": h["handoff_id"]})
    return ok(handoff_id=h["handoff_id"], routed_to=h["routed_to"], channel_status=h["channel_status"],
              record=h["record"])


def list_handoffs(conn, state: str | None = "OPEN", facility_id: str | None = None) -> list[dict]:
    rows = conn.execute(
        "SELECT h.*, s.destination_facility_id FROM handoffs h LEFT JOIN shipments s USING (shipment_id)"
        " WHERE (%(st)s::text IS NULL OR h.state = %(st)s) AND (%(f)s::text IS NULL OR s.destination_facility_id = %(f)s)"
        " ORDER BY h.created_at DESC", {"st": state, "f": facility_id}).fetchall()
    return [dict(r) | {"record": json.loads(r["record_json"])} for r in rows]


def close_handoff(conn, handoff_id: str, resolution: str, *, actor: str = "coordinator") -> dict:
    with conn.transaction():
        now = clock.now(conn)
        r = conn.execute("UPDATE handoffs SET state = 'CLOSED', closed_at = %s WHERE handoff_id = %s AND state = 'OPEN'"
                         " RETURNING handoff_id", (fmt_ts(now), handoff_id)).fetchone()
        if r is None:
            return err("HANDOFF_NOT_OPEN", handoff_id)
        audit(conn, now, actor, "CLOSE_HANDOFF", handoff_id, None, {"resolution": resolution})
    return ok(handoff_id=handoff_id, state="CLOSED")
