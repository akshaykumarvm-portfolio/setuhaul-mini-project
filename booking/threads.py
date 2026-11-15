"""Conversation records written for the agent (Decision 2): threads, transcript, shipment link, exception.

The agent never writes the database; it calls these through the booking API.
`chat_threads.shipment_id` is written only once the link is certain.
"""
from __future__ import annotations

import psycopg

from booking import clock
from booking.clock import fmt_ts
from booking.common import audit, err, new_id, ok
from booking.feasibility import LIVE_STATUSES

LIVE_EXC = ("OPEN", "NEEDS_INFORMATION", "SLOT_OPTIONS_SHARED", "WAITING_CONFIRMATION")
EXC_TYPE = {"TRAFFIC": "TRAFFIC", "BREAKDOWN": "BREAKDOWN", "WEATHER": "WEATHER"}


def open_thread(conn, driver_id: str, thread_id: str | None = None, intent: str = "UNKNOWN") -> dict:
    if conn.execute("SELECT 1 FROM drivers WHERE driver_id = %s", (driver_id,)).fetchone() is None:
        return err("UNKNOWN_DRIVER", driver_id)
    with conn.transaction():
        now = clock.now(conn)
        if thread_id:
            t = conn.execute("SELECT * FROM chat_threads WHERE thread_id = %s", (thread_id,)).fetchone()
            if t:
                if t["driver_id"] != driver_id:
                    return err("NOT_YOURS", "thread belongs to another driver")
                return ok(thread=dict(t), created=False)
        thread_id = thread_id or new_id("THR")
        conn.execute("INSERT INTO chat_threads (thread_id, driver_id, opened_at, thread_status, thread_intent)"
                     " VALUES (%s,%s,%s,'OPEN',%s)", (thread_id, driver_id, fmt_ts(now), intent))
        t = conn.execute("SELECT * FROM chat_threads WHERE thread_id = %s", (thread_id,)).fetchone()
    return ok(thread=dict(t), created=True)


def add_message(conn, thread_id: str, sender_type: str, text: str, *, external_message_id: str | None = None,
                sender_reference: str | None = None, parsed_intent: str | None = None,
                extracted_eta_ts: str | None = None, requires_human_review: bool = False) -> dict:
    try:
        with conn.transaction():
            now = clock.now(conn)
            mid = new_id("MSG")
            conn.execute(
                "INSERT INTO chat_messages (chat_message_id, thread_id, sender_type, sender_reference, message_text,"
                " message_ts, external_message_id, is_duplicate, parsed_intent, extracted_eta_ts, requires_human_review)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,0,%s,%s,%s)",
                (mid, thread_id, sender_type, sender_reference, text, fmt_ts(now), external_message_id, parsed_intent,
                 extracted_eta_ts, int(requires_human_review)))
    except psycopg.errors.UniqueViolation:
        return err("DUPLICATE_MESSAGE", "This message id was already received.")
    except psycopg.errors.ForeignKeyViolation:
        return err("THREAD_NOT_FOUND", thread_id)
    return ok(chat_message_id=mid, message_ts=fmt_ts(now))


def transcript(conn, thread_id: str, limit: int = 50) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT chat_message_id, sender_type, message_text, message_ts FROM chat_messages WHERE thread_id = %s"
        " ORDER BY message_ts, chat_message_id LIMIT %s", (thread_id, limit))]


def link_shipment(conn, thread_id: str, driver_id: str, shipment_id: str) -> dict:
    with conn.transaction():
        now = clock.now(conn)
        sh = conn.execute("SELECT shipment_id, driver_id, current_status, order_reference FROM shipments"
                          " WHERE shipment_id = %s OR order_reference = %s", (shipment_id, shipment_id)).fetchone()
        if sh is None:
            return err("UNKNOWN_SHIPMENT", shipment_id)
        if sh["driver_id"] != driver_id:
            return err("NOT_YOURS", "That load is not assigned to you.")
        if sh["current_status"] == "CANCELLED":
            return err("CANCELLED", f"{sh['shipment_id']} was cancelled. Please contact your dispatcher.",
                       shipment_id=sh["shipment_id"])
        if sh["current_status"] not in LIVE_STATUSES:
            return err("SHIPMENT_NOT_ACTIVE", f"{sh['shipment_id']} is {sh['current_status']}",
                       shipment_id=sh["shipment_id"])
        r = conn.execute("UPDATE chat_threads SET shipment_id = %s WHERE thread_id = %s AND driver_id = %s"
                         " RETURNING thread_id", (sh["shipment_id"], thread_id, driver_id)).fetchone()
        if r is None:
            return err("THREAD_NOT_FOUND", thread_id)
        audit(conn, now, f"driver:{driver_id}", "LINK_THREAD", thread_id, None, {"shipment_id": sh["shipment_id"]})
    return ok(thread_id=thread_id, shipment_id=sh["shipment_id"], order_reference=sh["order_reference"])


def upsert_exception(conn, now, shipment_id: str, driver_id: str, thread_id: str | None, *, cause: str | None,
                     eta_hi: str, delay_min: int | None, low_confidence: bool, text: str) -> str:
    """One live exception per shipment per day (the DB index enforces it per dedupe key). Call inside a tx."""
    status = "NEEDS_INFORMATION" if low_confidence else "OPEN"
    live = conn.execute("SELECT exception_id FROM driver_exceptions WHERE shipment_id = %s AND exception_status = ANY(%s)"
                        " ORDER BY reported_at DESC LIMIT 1", (shipment_id, list(LIVE_EXC))).fetchone()
    if live:
        conn.execute("UPDATE driver_exceptions SET declared_eta_ts = %s, reported_delay_min = COALESCE(%s, reported_delay_min),"
                     " exception_status = %s WHERE exception_id = %s",
                     (eta_hi, delay_min, status, live["exception_id"]))
        return live["exception_id"]
    if thread_id is None or conn.execute("SELECT 1 FROM chat_threads WHERE thread_id = %s", (thread_id,)).fetchone() is None:
        return ""
    sev = conn.execute("SELECT priority_code FROM shipments WHERE shipment_id = %s", (shipment_id,)).fetchone()["priority_code"]
    sev = {"CRITICAL": "CRITICAL", "HIGH": "HIGH", "NORMAL": "MEDIUM", "LOW": "LOW"}[sev]
    exc_id = new_id("EXC")
    conn.execute(
        "INSERT INTO driver_exceptions (exception_id, shipment_id, driver_id, thread_id, exception_type, reported_at,"
        " reported_delay_min, declared_eta_ts, severity_code, exception_status, description, dedupe_key)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (exc_id, shipment_id, driver_id, thread_id, EXC_TYPE.get(cause or "", "DELAY"), fmt_ts(now), delay_min, eta_hi,
         sev, status, (text or "Driver reported a delay")[:500], f"{driver_id}-{shipment_id}-{now:%Y%m%d}"))
    return exc_id
