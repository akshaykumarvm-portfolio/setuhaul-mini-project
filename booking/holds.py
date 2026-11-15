"""Holds (Decision 6): the driver picks an option -> a 5-minute hold, re-checked under lock.

A hold keeps other drivers off the slots while this driver decides. It is not a
booking: it expires on its own and the sweeper (or the next writer) clears it.
"""
from __future__ import annotations

from datetime import timedelta

import psycopg

from booking import clock
from booking.common import audit, bump_versions, err, idem_begin, idem_save, lock_slots, new_id, ok, publish, slot_versions
from booking.config import get_settings
from booking.eta import effective_eta
from booking.feasibility import check_feasibility
from booking.quotes import build_quote, get_quote


def expire_holds_on(conn, slot_ids: list[str], now_ts: str) -> list[str]:
    """Mark lapsed ACTIVE holds touching these slots EXPIRED so their unique slots free up."""
    rows = conn.execute(
        "UPDATE slot_holds h SET state = 'EXPIRED' WHERE h.state = 'ACTIVE' AND h.expires_at <= %s"
        " AND EXISTS (SELECT 1 FROM slot_hold_claims c WHERE c.hold_id = h.hold_id AND c.active = 1"
        "             AND c.slot_id = ANY(%s)) RETURNING hold_id", (now_ts, list(slot_ids))).fetchall()
    ids = [r["hold_id"] for r in rows]
    if ids:
        conn.execute("UPDATE slot_hold_claims SET active = 0 WHERE hold_id = ANY(%s)", (ids,))
    return ids


def _end_hold(conn, hold_id: str, state: str) -> list[str]:
    conn.execute("UPDATE slot_holds SET state = %s WHERE hold_id = %s AND state = 'ACTIVE'", (state, hold_id))
    slots = [r["slot_id"] for r in conn.execute(
        "UPDATE slot_hold_claims SET active = 0 WHERE hold_id = %s AND active = 1 RETURNING slot_id", (hold_id,))]
    bump_versions(conn, slots)
    return slots


def hold_option(conn, quote_id: str, option_no: int, *, idem_key: str | None = None, actor: str = "agent") -> dict:
    s = get_settings()
    try:
        with conn.transaction():
            if prior := idem_begin(conn, idem_key):
                return prior
            now = clock.now(conn)
            q = get_quote(conn, quote_id)
            if q is None:
                return err("QUOTE_NOT_FOUND", quote_id)
            shipment_id = q["shipment_id"]
            if not 1 <= option_no <= len(q["options"]):
                return err("BAD_OPTION", f"option {option_no} not in quote")
            opt = q["options"][option_no - 1]
            slots = opt["slot_ids"]
            lock_slots(conn, slots)
            now_ts = clock.fmt_ts(now)
            expire_holds_on(conn, slots, now_ts)

            if effective_eta(conn, shipment_id, now).confidence == "LOW":
                return err("ETA_UNCONFIRMED", "Confirm the arrival time before holding a slot.")
            verdict = check_feasibility(conn, shipment_id, slots, now)
            taken = [r for r in verdict.reasons if r.code in ("SLOT_TAKEN", "SLOT_HELD")]
            if taken:
                return err("SLOT_TAKEN", taken[0].detail, fresh=build_quote(conn, shipment_id, now, thread_id=q["thread_id"]))
            if q["expires_at"] <= now_ts or slot_versions(conn, slots) != {k: q["versions"][k] for k in slots}:
                return err("STALE", "This option changed since it was shown.",
                           fresh=build_quote(conn, shipment_id, now, thread_id=q["thread_id"]))
            if not verdict.ok:
                return err("NO_LONGER_FEASIBLE", verdict.reasons[0].detail, reasons=verdict.as_json()["reasons"],
                           fresh=build_quote(conn, shipment_id, now, thread_id=q["thread_id"]))

            prev = conn.execute("SELECT hold_id FROM slot_holds WHERE shipment_id = %s AND state = 'ACTIVE'",
                                (shipment_id,)).fetchone()
            if prev:  # one live hold per shipment: picking a new option releases the old one
                _end_hold(conn, prev["hold_id"], "RELEASED")
            hold_id = new_id("HLD")
            expires = now + timedelta(minutes=s.hold_minutes)
            conn.execute("INSERT INTO slot_holds (hold_id, slot_id, shipment_id, thread_id, quote_id, state, created_at,"
                         " expires_at) VALUES (%s,%s,%s,%s,%s,'ACTIVE',%s,%s)",
                         (hold_id, slots[0], shipment_id, q["thread_id"], quote_id, now_ts, clock.fmt_ts(expires)))
            for sid in slots:
                conn.execute("INSERT INTO slot_hold_claims (hold_id, slot_id) VALUES (%s,%s)", (hold_id, sid))
            bump_versions(conn, slots)
            result = ok(hold_id=hold_id, shipment_id=shipment_id, dock_code=opt["dock_code"], slot_ids=slots,
                        start=opt["start"], end=opt["end"], expires_at=clock.fmt_ts(expires),
                        needs_approval=opt["needs_approval"], status="HELD",
                        note=f"Held for {s.hold_minutes} minutes. Not booked yet.")
            audit(conn, now, actor, "HOLD", hold_id, None, result)
            idem_save(conn, idem_key, result, now)
    except psycopg.errors.UniqueViolation:
        # Defence in depth: the partial unique index caught a second active hold on a slot.
        with conn.transaction():
            now = clock.now(conn)
            q = get_quote(conn, quote_id)
            return err("SLOT_TAKEN", "Another truck just took this slot.",
                       fresh=build_quote(conn, q["shipment_id"], now, thread_id=q["thread_id"]))
    publish(_facility(conn, shipment_id), {"type": "SLOT_HELD", "slot_ids": slots, "hold_id": hold_id})
    return result


def release_hold(conn, hold_id: str, *, actor: str = "agent") -> dict:
    with conn.transaction():
        now = clock.now(conn)
        h = conn.execute("SELECT * FROM slot_holds WHERE hold_id = %s FOR UPDATE", (hold_id,)).fetchone()
        if h is None:
            return err("HOLD_NOT_FOUND", hold_id)
        if h["state"] != "ACTIVE":
            return ok(hold_id=hold_id, status=h["state"])
        slots = _end_hold(conn, hold_id, "RELEASED")
        audit(conn, now, actor, "RELEASE_HOLD", hold_id)
    publish(_facility(conn, h["shipment_id"]), {"type": "SLOT_FREED", "slot_ids": slots})
    return ok(hold_id=hold_id, status="RELEASED")


def _facility(conn, shipment_id: str) -> str:
    return conn.execute("SELECT destination_facility_id AS f FROM shipments WHERE shipment_id = %s",
                        (shipment_id,)).fetchone()["f"]
