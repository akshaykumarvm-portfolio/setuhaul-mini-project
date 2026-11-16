"""Surge mode (Decision 7): who gets a contested slot when capacity is short.

Normal mode: first valid commit wins (the DB decides).
Surge mode: >= SURGE_THRESHOLD loads at a facility with a live exception and fresh activity (exception
reported or ETA updated) in the last SURGE_WINDOW_MIN minutes. Then a slot that was SHOWN to a
higher-priority truck in the last PROTECT_MIN minutes is kept for that truck while it decides; a
lower-priority truck asking for it gets PRIORITY_RESERVED and fresh options.

This is the spec's "batch for 2 minutes, then allocate by priority" made synchronous for a chat: the same
winner (priority first; equal priority keeps first-come) without making any driver wait for a batch to close.
Confirmed commitments are never displaced; the reservation only lasts while the higher-priority truck has
neither a hold nor a pending request.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from booking.clock import fmt_ts

SURGE_THRESHOLD = 5
SURGE_WINDOW_MIN = 30
PROTECT_MIN = 2
RANK = {"CRITICAL": 4, "HIGH": 3, "NORMAL": 2, "LOW": 1}
LIVE_EXC = ["OPEN", "NEEDS_INFORMATION", "SLOT_OPTIONS_SHARED", "WAITING_CONFIRMATION"]


def surge_state(conn, facility_id: str, now: datetime) -> dict:
    since, now_ts = fmt_ts(now - timedelta(minutes=SURGE_WINDOW_MIN)), fmt_ts(now)
    rows = conn.execute(
        "SELECT DISTINCT e.shipment_id FROM driver_exceptions e JOIN shipments s USING (shipment_id)"
        " WHERE s.destination_facility_id = %(f)s AND e.exception_status = ANY(%(live)s) AND e.reported_at <= %(now)s"
        "   AND (e.reported_at >= %(since)s OR EXISTS (SELECT 1 FROM eta_updates u WHERE u.shipment_id = e.shipment_id"
        "        AND u.created_at >= %(since)s AND u.created_at <= %(now)s))",
        {"f": facility_id, "live": LIVE_EXC, "since": since, "now": now_ts}).fetchall()
    ships = sorted(r["shipment_id"] for r in rows)
    return {"active": len(ships) >= SURGE_THRESHOLD, "count": len(ships), "threshold": SURGE_THRESHOLD,
            "window_min": SURGE_WINDOW_MIN, "shipments": ships}


def reserved_by(conn, shipment_id: str, slot_ids: list[str], now: datetime) -> dict | None:
    """The higher-priority truck this slot is being kept for, if any (only in surge mode)."""
    me = conn.execute("SELECT priority_code, destination_facility_id FROM shipments WHERE shipment_id = %s",
                      (shipment_id,)).fetchone()
    if not surge_state(conn, me["destination_facility_id"], now)["active"]:
        return None
    since = fmt_ts(now - timedelta(minutes=PROTECT_MIN))
    quotes = conn.execute(
        "SELECT q.shipment_id, q.options_json, q.created_at, s.priority_code FROM slot_quotes q"
        " JOIN shipments s USING (shipment_id) WHERE q.created_at >= %s AND q.created_at <= %s"
        " AND q.shipment_id <> %s AND s.destination_facility_id = %s ORDER BY q.created_at",
        (since, fmt_ts(now), shipment_id, me["destination_facility_id"])).fetchall()
    mine = set(slot_ids)
    for q in quotes:
        if RANK[q["priority_code"]] <= RANK[me["priority_code"]]:
            continue
        if not any(mine & set(o["slot_ids"]) for o in json.loads(q["options_json"])):
            continue
        busy = conn.execute(
            "SELECT 1 FROM slot_holds WHERE shipment_id = %(s)s AND state IN ('ACTIVE','CONVERTED') AND created_at >= %(q)s"
            " UNION SELECT 1 FROM appointments WHERE shipment_id = %(s)s AND booking_source = 'DRIVER_CHAT'"
            " AND appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED') AND booked_at >= %(q)s",
            {"s": q["shipment_id"], "q": q["created_at"]}).fetchone()
        if busy:
            continue   # it already chose something; the reservation is over
        return {"shipment_id": q["shipment_id"], "priority": q["priority_code"], "shown_at": q["created_at"]}
    return None
