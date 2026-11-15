"""Requests and confirmations (Decision 6). The ONLY step that claims capacity.

request_booking: hold -> new appointment PENDING_CONFIRMATION, is_current = 0,
    replaced_appointment_id = the current one. Its claim rows now block the slots.
confirm: one transaction swaps old -> CANCELLED (is_current 0) and new -> CONFIRMED
    (is_current 1). The old slot stays the driver's until this moment.
reject: PENDING -> REJECTED, slots freed, hand-off to a coordinator.
Feasibility is re-checked under row locks inside every one of these writes.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import psycopg

from booking import clock
from booking.clock import fmt_ts, parse_ts
from booking.common import audit, bump_versions, err, idem_begin, idem_save, lock_slots, new_id, ok, publish
from booking.config import get_settings
from booking.eta import effective_eta
from booking.feasibility import ACTIVE_APPT, check_feasibility

ACTIVE = list(ACTIVE_APPT)


def current_appointment(conn, shipment_id: str) -> dict | None:
    return conn.execute("SELECT * FROM appointments WHERE shipment_id = %s AND is_current = 1"
                        " AND appointment_status = ANY(%s)", (shipment_id, ACTIVE)).fetchone()


def claimed_slots(conn, appointment_id: str) -> list[str]:
    rows = conn.execute("SELECT slot_id FROM appointment_slot_claims WHERE appointment_id = %s AND active = 1"
                        " ORDER BY slot_id", (appointment_id,)).fetchall()
    if rows:
        return [r["slot_id"] for r in rows]
    a = conn.execute("SELECT slot_id FROM appointments WHERE appointment_id = %s", (appointment_id,)).fetchone()
    return [a["slot_id"]] if a else []


def pending_expiry(conn, appt: dict) -> datetime:
    """15 min after the request, or 30 min before the slot starts, whichever is first."""
    s = get_settings()
    start = parse_ts(conn.execute("SELECT slot_start_ts FROM appointment_slots WHERE slot_id = %s",
                                  (appt["slot_id"],)).fetchone()["slot_start_ts"])
    return min(parse_ts(appt["booked_at"]) + timedelta(minutes=s.pending_expiry_min),
               start - timedelta(minutes=s.pending_cutoff_before_start_min))


def _facility(conn, shipment_id: str) -> str:
    return conn.execute("SELECT destination_facility_id AS f FROM shipments WHERE shipment_id = %s",
                        (shipment_id,)).fetchone()["f"]


def _slot_window(conn, slot_ids: list[str]) -> tuple[str, str, str]:
    r = conn.execute("SELECT MIN(s.slot_start_ts) AS a, MAX(s.slot_end_ts) AS b, MIN(d.dock_code) AS dock"
                     " FROM appointment_slots s JOIN docks d USING (dock_id) WHERE s.slot_id = ANY(%s)",
                     (slot_ids,)).fetchone()
    return r["dock"], r["a"], r["b"]


def insert_pending(conn, shipment_id: str, slot_ids: list[str], now: datetime, *,
                   replaces: str | None, source: str = "DRIVER_CHAT") -> str:
    """Write the claim. The partial unique indexes reject a second active claim on any slot."""
    appt_id = new_id("APT")
    conn.execute(
        "INSERT INTO appointments (appointment_id, shipment_id, slot_id, appointment_status, booking_source, is_current,"
        " booked_at, replaced_appointment_id, updated_at) VALUES (%s,%s,%s,'PENDING_CONFIRMATION',%s,0,%s,%s,%s)",
        (appt_id, shipment_id, _first_slot(conn, slot_ids), source,
         fmt_ts(now), replaces, fmt_ts(now)))
    for sid in slot_ids:
        conn.execute("INSERT INTO appointment_slot_claims (appointment_id, slot_id) VALUES (%s,%s)", (appt_id, sid))
    return appt_id


def _first_slot(conn, slot_ids: list[str]) -> str:
    return conn.execute("SELECT slot_id FROM appointment_slots WHERE slot_id = ANY(%s) ORDER BY slot_start_ts LIMIT 1",
                        (slot_ids,)).fetchone()["slot_id"]


def request_booking(conn, hold_id: str, *, idem_key: str | None = None, actor: str = "agent") -> dict:
    try:
        with conn.transaction():
            if prior := idem_begin(conn, idem_key):
                return prior
            now = clock.now(conn)
            hold = conn.execute("SELECT * FROM slot_holds WHERE hold_id = %s FOR UPDATE", (hold_id,)).fetchone()
            if hold is None:
                return err("HOLD_NOT_FOUND", hold_id)
            if hold["state"] == "CONVERTED":
                a = conn.execute("SELECT entity_id AS appointment_id FROM audit_log WHERE action = 'REQUEST' AND before_json = %s",
                                 (f'"{hold_id}"',)).fetchone()
                return err("ALREADY_REQUESTED", "This hold was already turned into a request.",
                           appointment_id=a["appointment_id"] if a else None)
            if hold["state"] != "ACTIVE" or hold["expires_at"] <= fmt_ts(now):
                return err("HOLD_EXPIRED", "The hold lapsed; pick an option again.")
            slots = [r["slot_id"] for r in conn.execute(
                "SELECT slot_id FROM slot_hold_claims WHERE hold_id = %s AND active = 1", (hold_id,))]
            lock_slots(conn, slots)
            shipment_id = hold["shipment_id"]
            if effective_eta(conn, shipment_id, now).confidence == "LOW":
                return err("ETA_UNCONFIRMED", "Confirm the arrival time before requesting.")
            verdict = check_feasibility(conn, shipment_id, slots, now, ignore_hold=hold_id)
            if not verdict.ok:
                return err("NO_LONGER_FEASIBLE", verdict.reasons[0].detail, reasons=verdict.as_json()["reasons"])
            cur = current_appointment(conn, shipment_id)
            appt_id = insert_pending(conn, shipment_id, slots, now, replaces=cur["appointment_id"] if cur else None)
            conn.execute("UPDATE slot_holds SET state = 'CONVERTED' WHERE hold_id = %s", (hold_id,))
            conn.execute("UPDATE slot_hold_claims SET active = 0 WHERE hold_id = %s", (hold_id,))
            bump_versions(conn, slots)
            dock, start, _ = _slot_window(conn, slots)
            appt = conn.execute("SELECT * FROM appointments WHERE appointment_id = %s", (appt_id,)).fetchone()
            result = ok(appointment_id=appt_id, shipment_id=shipment_id, status="PENDING_CONFIRMATION",
                        dock_code=dock, slot_ids=slots, start=start, end=fmt_ts(verdict.end),
                        replaces=cur["appointment_id"] if cur else None,
                        needs_approval=[r.as_json() for r in verdict.needs_approval],
                        expires_at=fmt_ts(pending_expiry(conn, appt)),
                        note="Requested, waiting for the warehouse. Keep your current slot until confirmed.")
            conn.execute("INSERT INTO audit_log (ts, actor, action, entity_id, before_json, after_json)"
                         " VALUES (%s,%s,'REQUEST',%s,%s,NULL)", (fmt_ts(now), actor, appt_id, f'"{hold_id}"'))
            idem_save(conn, idem_key, result, now)
    except psycopg.errors.UniqueViolation:
        return err("SLOT_TAKEN", "Another truck's booking already holds this slot.")
    except psycopg.errors.CheckViolation as e:
        return err("SLOT_NOT_OPEN", str(e).splitlines()[0])
    publish(_facility(conn, shipment_id), {"type": "SLOT_REQUESTED", "slot_ids": slots, "appointment_id": appt_id})
    return result


def confirm(conn, appointment_id: str, *, actor: str = "coordinator", confirmation_ref: str | None = None) -> dict:
    with conn.transaction():
        now = clock.now(conn)
        appt = conn.execute("SELECT * FROM appointments WHERE appointment_id = %s FOR UPDATE", (appointment_id,)).fetchone()
        if appt is None:
            return err("APPOINTMENT_NOT_FOUND", appointment_id)
        if appt["appointment_status"] != "PENDING_CONFIRMATION":
            return err("INVALID_STATE", f"appointment is {appt['appointment_status']}")
        if pending_expiry(conn, appt) <= now:
            return err("PENDING_EXPIRED", "The request lapsed before confirmation.")
        slots = claimed_slots(conn, appointment_id)
        lock_slots(conn, slots)
        shipment_id = appt["shipment_id"]
        verdict = check_feasibility(conn, shipment_id, slots, now, ignore_appointments=(appointment_id,))
        if not verdict.ok:
            return err("NO_LONGER_FEASIBLE", verdict.reasons[0].detail, reasons=verdict.as_json()["reasons"])
        freed: list[str] = []
        olds = conn.execute("SELECT * FROM appointments WHERE shipment_id = %s AND appointment_id <> %s"
                            " AND is_current = 1 AND appointment_status = ANY(%s) FOR UPDATE",
                            (shipment_id, appointment_id, ACTIVE)).fetchall()
        for old in olds:
            freed += claimed_slots(conn, old["appointment_id"])
            conn.execute("UPDATE appointments SET appointment_status = 'CANCELLED', is_current = 0, cancelled_at = %s,"
                         " cancellation_reason = %s, updated_at = %s WHERE appointment_id = %s",
                         (fmt_ts(now), f"Replaced by {appointment_id}", fmt_ts(now), old["appointment_id"]))
            conn.execute("UPDATE appointment_slot_claims SET active = 0 WHERE appointment_id = %s", (old["appointment_id"],))
        conn.execute("UPDATE appointments SET appointment_status = 'CONFIRMED', is_current = 1, confirmed_at = %s,"
                     " warehouse_confirmation_ref = %s, updated_at = %s WHERE appointment_id = %s",
                     (fmt_ts(now), confirmation_ref or f"{actor}@{fmt_ts(now)}", fmt_ts(now), appointment_id))
        conn.execute("UPDATE driver_exceptions SET exception_status = 'RESOLVED' WHERE shipment_id = %s"
                     " AND exception_status IN ('OPEN','NEEDS_INFORMATION','SLOT_OPTIONS_SHARED','WAITING_CONFIRMATION')",
                     (shipment_id,))
        bump_versions(conn, slots + freed)
        dock, start, _ = _slot_window(conn, slots)
        result = ok(appointment_id=appointment_id, shipment_id=shipment_id, status="CONFIRMED", dock_code=dock,
                    start=start, end=fmt_ts(verdict.end), slot_ids=slots,
                    cancelled=[o["appointment_id"] for o in olds], freed_slot_ids=freed)
        audit(conn, now, actor, "CONFIRM", appointment_id, dict(appt), result)
    fac = _facility(conn, shipment_id)
    publish(fac, {"type": "BOOKING_CONFIRMED", "appointment_id": appointment_id, "slot_ids": slots})
    if freed:
        publish(fac, {"type": "SLOT_FREED", "slot_ids": freed})
    return result


def reject(conn, appointment_id: str, reason: str, *, actor: str = "coordinator", status: str = "REJECTED") -> dict:
    from booking.handoffs import create_handoff  # avoid import cycle

    with conn.transaction():
        now = clock.now(conn)
        appt = conn.execute("SELECT * FROM appointments WHERE appointment_id = %s FOR UPDATE", (appointment_id,)).fetchone()
        if appt is None:
            return err("APPOINTMENT_NOT_FOUND", appointment_id)
        if appt["appointment_status"] != "PENDING_CONFIRMATION":
            return err("INVALID_STATE", f"appointment is {appt['appointment_status']}")
        slots = claimed_slots(conn, appointment_id)
        conn.execute("UPDATE appointments SET appointment_status = %s, cancelled_at = %s, cancellation_reason = %s,"
                     " updated_at = %s WHERE appointment_id = %s",
                     (status, fmt_ts(now), reason, fmt_ts(now), appointment_id))
        conn.execute("UPDATE appointment_slot_claims SET active = 0 WHERE appointment_id = %s", (appointment_id,))
        bump_versions(conn, slots)
        code = "REQUEST_REJECTED" if status == "REJECTED" else "PENDING_LAPSED"
        hof = create_handoff(conn, appt["shipment_id"], code, reason, now, actor=actor)
        audit(conn, now, actor, code, appointment_id, dict(appt), {"status": status, "reason": reason})
    publish(_facility(conn, appt["shipment_id"]), {"type": "SLOT_FREED", "slot_ids": slots})
    return ok(appointment_id=appointment_id, status=status, freed_slot_ids=slots, handoff_id=hof["handoff_id"])
