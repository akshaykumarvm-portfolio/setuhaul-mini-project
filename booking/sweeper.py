"""Sweeper (Decisions 6 and 9): runs every 30 s inside the booking service.

* expires holds past their 5 minutes;
* lapses pending requests at 15 min after the request or 30 min before slot start
  (whichever is first) and hands them to a coordinator;
* for every freed slot, lists open exceptions it now fits, highest priority first,
  and publishes a re-offer event (the agent picks it up in phase 3).
"""
from __future__ import annotations

import asyncio
import logging

from booking import clock
from booking.bookings import pending_expiry, reject
from booking.clock import fmt_ts
from booking.common import audit, bump_versions, publish
from booking.feasibility import find_options, load_state

log = logging.getLogger(__name__)
PRIORITY = {"CRITICAL": 0, "HIGH": 1, "NORMAL": 2, "LOW": 3}


def _reoffers(conn, freed: dict[str, list[str]], now) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for fac, slots in freed.items():
        cands = conn.execute(
            "SELECT DISTINCT e.shipment_id, s.priority_code FROM driver_exceptions e JOIN shipments s USING (shipment_id)"
            " WHERE s.destination_facility_id = %s AND e.exception_status IN"
            " ('OPEN','NEEDS_INFORMATION','SLOT_OPTIONS_SHARED','WAITING_CONFIRMATION')", (fac,)).fetchall()
        hits = []
        for c in sorted(cands, key=lambda r: PRIORITY[r["priority_code"]]):
            st = load_state(conn, c["shipment_id"], now)
            opts, _ = find_options(st, limit=10)
            if any(set(o["slot_ids"]) & set(slots) for o in opts):
                hits.append({"shipment_id": c["shipment_id"], "priority": c["priority_code"]})
        out[fac] = hits
        publish(fac, {"type": "SLOT_FREED", "slot_ids": slots, "reoffer": hits})
    return out


def sweep(conn) -> dict:
    now = clock.now(conn)
    now_ts = fmt_ts(now)
    freed: dict[str, list[str]] = {}
    with conn.transaction():
        expired = conn.execute(
            "UPDATE slot_holds h SET state = 'EXPIRED' FROM shipments s WHERE h.state = 'ACTIVE' AND h.expires_at <= %s"
            " AND s.shipment_id = h.shipment_id RETURNING h.hold_id, s.destination_facility_id AS fac", (now_ts,)).fetchall()
        for h in expired:
            slots = [r["slot_id"] for r in conn.execute(
                "UPDATE slot_hold_claims SET active = 0 WHERE hold_id = %s AND active = 1 RETURNING slot_id", (h["hold_id"],))]
            bump_versions(conn, slots)
            freed.setdefault(h["fac"], []).extend(slots)
            audit(conn, now, "sweeper", "EXPIRE_HOLD", h["hold_id"])
    lapsed = []
    for a in conn.execute("SELECT * FROM appointments WHERE appointment_status = 'PENDING_CONFIRMATION'").fetchall():
        if pending_expiry(conn, a) <= now:
            r = reject(conn, a["appointment_id"], "Pending confirmation lapsed (15 min / 30 min before start)",
                       actor="sweeper", status="CANCELLED")
            if r["ok"]:
                lapsed.append(a["appointment_id"])
                fac = conn.execute("SELECT destination_facility_id AS f FROM shipments WHERE shipment_id = %s",
                                   (a["shipment_id"],)).fetchone()["f"]
                freed.setdefault(fac, []).extend(r["freed_slot_ids"])
    reoffers = _reoffers(conn, freed, now) if freed else {}
    return {"sim_now": now_ts, "expired_holds": [h["hold_id"] for h in expired], "lapsed_requests": lapsed,
            "freed": freed, "reoffers": reoffers}


async def run_forever(connect, interval_s: int) -> None:
    while True:
        try:
            conn = await asyncio.to_thread(connect)
            try:
                res = await asyncio.to_thread(sweep, conn)
                if res["expired_holds"] or res["lapsed_requests"]:
                    log.info("sweep: %s", res)
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 - the loop must survive DB blips
            log.exception("sweep failed")
        await asyncio.sleep(interval_s)
