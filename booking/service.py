"""Driver-facing reads and the ETA write the agent tools call."""
from __future__ import annotations

from datetime import datetime

from booking import clock
from booking.bookings import claimed_slots, current_appointment, pending_expiry
from booking.clock import fmt_ts
from booking.common import audit, err, new_id, ok
from booking.eta import effective_eta
from booking.feasibility import LIVE_STATUSES, check_feasibility, shipment_flags
from booking.holds import _end_hold
from booking.threads import upsert_exception

CAUSES = {"TRAFFIC", "BREAKDOWN", "WEATHER", "LATE_DEPARTURE", "LOADING_DELAY", "ROUTE_ISSUE", "OTHER"}


def _appt_view(conn, a: dict | None) -> dict | None:
    if a is None:
        return None
    slots = claimed_slots(conn, a["appointment_id"])
    r = conn.execute("SELECT MIN(s.slot_start_ts) AS start, MAX(s.slot_end_ts) AS end, MIN(d.dock_code) AS dock"
                     " FROM appointment_slots s JOIN docks d USING (dock_id) WHERE s.slot_id = ANY(%s)", (slots,)).fetchone()
    return {"appointment_id": a["appointment_id"], "status": a["appointment_status"], "dock_code": r["dock"],
            "start": r["start"], "end": r["end"], "slot_ids": slots}


def shipment_status(conn, shipment_id: str) -> dict:
    sh = conn.execute("SELECT * FROM shipments WHERE shipment_id = %s", (shipment_id,)).fetchone()
    if sh is None:
        return err("UNKNOWN_SHIPMENT", shipment_id)
    now = clock.now(conn)
    pending = conn.execute("SELECT * FROM appointments WHERE shipment_id = %s AND appointment_status = 'PENDING_CONFIRMATION'"
                           " AND is_current = 0 ORDER BY booked_at DESC LIMIT 1", (shipment_id,)).fetchone()
    hold = conn.execute("SELECT * FROM slot_holds WHERE shipment_id = %s AND state = 'ACTIVE' AND expires_at > %s",
                        (shipment_id, fmt_ts(now))).fetchone()
    last = conn.execute("SELECT * FROM appointments WHERE shipment_id = %s AND appointment_status IN ('REJECTED','CANCELLED')"
                        " AND booking_source = 'DRIVER_CHAT' ORDER BY updated_at DESC LIMIT 1", (shipment_id,)).fetchone()
    handoff = conn.execute("SELECT handoff_id, reason_code, routed_to FROM handoffs WHERE shipment_id = %s AND state = 'OPEN'"
                           " ORDER BY created_at DESC LIMIT 1", (shipment_id,)).fetchone()
    cur = current_appointment(conn, shipment_id)
    p = _appt_view(conn, pending)
    if p:
        p["expires_at"] = fmt_ts(pending_expiry(conn, pending))
    stage = ("PENDING" if pending else "HELD" if hold else "CONFIRMED" if cur and cur["appointment_status"] == "CONFIRMED"
             else "NONE")
    return ok(shipment_id=shipment_id, shipment_status=sh["current_status"], stage=stage, sim_now=fmt_ts(now),
              current_appointment=_appt_view(conn, cur), pending_request=p,
              hold={"hold_id": hold["hold_id"], "expires_at": hold["expires_at"]} if hold else None,
              last_closed_request=({"appointment_id": last["appointment_id"], "status": last["appointment_status"],
                                    "reason": last["cancellation_reason"]} if last else None),
              open_handoff=dict(handoff) if handoff else None, eta=effective_eta(conn, shipment_id).as_json())


def driver_context(conn, driver_id: str) -> dict:
    d = conn.execute("SELECT driver_id, driver_name, carrier_id, driver_status FROM drivers WHERE driver_id = %s",
                     (driver_id,)).fetchone()
    if d is None:
        return err("UNKNOWN_DRIVER", driver_id)
    out = []
    for sh in conn.execute("SELECT * FROM shipments WHERE driver_id = %s ORDER BY original_eta_ts", (driver_id,)):
        chk = conn.execute("SELECT gate_in_ts, queue_state, arrival_state FROM facility_checkins WHERE shipment_id = %s",
                           (sh["shipment_id"],)).fetchone()
        exc = conn.execute("SELECT exception_id, exception_type, exception_status FROM driver_exceptions WHERE shipment_id = %s"
                           " AND exception_status NOT IN ('RESOLVED','DUPLICATE','CANCELLED')", (sh["shipment_id"],)).fetchall()
        out.append({
            "shipment_id": sh["shipment_id"], "order_reference": sh["order_reference"],
            "facility_id": sh["destination_facility_id"], "status": sh["current_status"],
            "live": sh["current_status"] in LIVE_STATUSES, "priority": sh["priority_code"],
            "load_weight_kg": sh["load_weight_kg"], "dock_type": sh["required_dock_type"],
            "expected_unload_min": sh["expected_unload_min"],
            "eta": effective_eta(conn, sh["shipment_id"]).as_json(),
            "current_appointment": _appt_view(conn, current_appointment(conn, sh["shipment_id"])),
            "checkin": dict(chk) if chk else None, "open_exceptions": [dict(e) for e in exc],
            "flags": [f.as_json() for f in shipment_flags(conn, dict(sh))],
        })
    return ok(driver=dict(d), shipments=out, live_count=sum(s["live"] for s in out))


def record_eta(conn, shipment_id: str, eta_lo: str, eta_hi: str, confidence: str, *, cause: str | None = None,
               cause_fixed: bool | None = None, source_text: str = "", driver_id: str | None = None,
               thread_id: str | None = None, actor: str = "agent") -> dict:
    """Store a driver ETA as a window, then re-check the current appointment and any live hold."""
    if confidence not in ("LOW", "MEDIUM", "HIGH"):
        return err("BAD_CONFIDENCE", confidence)
    lo, hi = clock.parse_ts(eta_lo), clock.parse_ts(eta_hi)
    if hi < lo:
        return err("BAD_WINDOW", "eta_hi is before eta_lo")
    with conn.transaction():
        now = clock.now(conn)
        sh = conn.execute("SELECT * FROM shipments WHERE shipment_id = %s FOR UPDATE", (shipment_id,)).fetchone()
        if sh is None:
            return err("UNKNOWN_SHIPMENT", shipment_id)
        if sh["current_status"] not in LIVE_STATUSES:
            return err("SHIPMENT_NOT_ACTIVE", f"{shipment_id} is {sh['current_status']}")
        eid = new_id("ETA")
        reason = cause if cause in CAUSES else ("OTHER" if cause else None)
        conn.execute("INSERT INTO eta_updates (eta_update_id, shipment_id, source_type, reported_by_driver_id,"
                     " declared_eta_ts, confidence_code, delay_reason_code, note, created_at)"
                     " VALUES (%s,%s,'DRIVER_DECLARED',%s,%s,%s,%s,%s,%s)",
                     (eid, shipment_id, driver_id or sh["driver_id"], fmt_ts(hi), confidence, reason,
                      source_text[:500] or None, fmt_ts(now)))
        conn.execute("INSERT INTO eta_windows (eta_update_id, shipment_id, eta_lo, eta_hi, confidence, cause, cause_fixed,"
                     " source_text) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                     (eid, shipment_id, fmt_ts(lo), fmt_ts(hi), confidence, cause,
                      None if cause_fixed is None else int(cause_fixed), source_text[:500] or None))
        conn.execute("UPDATE shipments SET latest_eta_ts = %s, updated_at = %s WHERE shipment_id = %s",
                     (fmt_ts(hi), fmt_ts(now), shipment_id))
        planned = clock.parse_ts(sh["original_eta_ts"])
        delay = max(int((hi - planned).total_seconds() // 60), 0)
        exc_id = upsert_exception(conn, now, shipment_id, sh["driver_id"], thread_id, cause=cause, eta_hi=fmt_ts(hi),
                                  delay_min=delay, low_confidence=confidence == "LOW", text=source_text)
        cur = current_appointment(conn, shipment_id)
        fits = None
        if cur:
            v = check_feasibility(conn, shipment_id, claimed_slots(conn, cur["appointment_id"]), now,
                                  ignore_appointments=(cur["appointment_id"],))
            fits = {"appointment_id": cur["appointment_id"], "still_fits": v.ok,
                    "reasons": [r.as_json() for r in v.reasons]}
        released = None
        hold = conn.execute("SELECT hold_id FROM slot_holds WHERE shipment_id = %s AND state = 'ACTIVE'",
                            (shipment_id,)).fetchone()
        if hold:
            slots = [r["slot_id"] for r in conn.execute(
                "SELECT slot_id FROM slot_hold_claims WHERE hold_id = %s AND active = 1", (hold["hold_id"],))]
            if not check_feasibility(conn, shipment_id, slots, now, ignore_hold=hold["hold_id"]).ok:
                _end_hold(conn, hold["hold_id"], "RELEASED")
                released = hold["hold_id"]
        result = ok(eta_update_id=eid, eta={"eta_lo": fmt_ts(lo), "eta_hi": fmt_ts(hi), "confidence": confidence},
                    current_appointment=fits, released_hold=released, exception_id=exc_id or None,
                    needs_clarification=confidence == "LOW")
        audit(conn, now, actor, "RECORD_ETA", eid, None, result)
    return result
