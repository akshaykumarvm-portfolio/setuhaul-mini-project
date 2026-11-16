"""Decision 7: in a surge, a lower-priority truck can't beat a higher-priority one to a slot by typing faster."""
from booking import clock, holds, quotes, service, threads
from booking.db import write_tx
from booking.surge import surge_state


def quote(conn, sid):
    with write_tx(conn):
        return quotes.build_quote(conn, sid, clock.now(conn))


def opt_with(q, slot):
    return next(o["option_no"] for o in q["options"] if slot in o["slot_ids"])


def start_surge(conn):
    """A fifth load reports a delay inside the 30-minute window (SHP1009, CRITICAL)."""
    threads.open_thread(conn, "DRV009", "T-SURGE-9")
    service.record_eta(conn, "SHP1009", "2026-08-04T10:50:00+05:30", "2026-08-04T10:50:00+05:30", "HIGH",
                       cause="TRAFFIC", thread_id="T-SURGE-9", source_text="Reaching 10:50")


def test_surge_detection(frozen):
    s = surge_state(frozen, "FAC-JAI-01", clock.now(frozen))
    assert not s["active"] and s["count"] == 4                      # seed at 10:00: below the threshold
    start_surge(frozen)
    s = surge_state(frozen, "FAC-JAI-01", clock.now(frozen))
    assert s["active"] and s["count"] == 5 and "SHP1009" in s["shipments"]


def test_lower_priority_cannot_take_slot_shown_to_critical(frozen):
    start_surge(frozen)
    crit = quote(frozen, "SHP1014")                                 # CRITICAL, shown D2 12:00 (019+020)
    norm = quote(frozen, "SHP1012")                                 # NORMAL, also sees D2 12:00 (019)
    r = holds.hold_option(frozen, norm["quote_id"], opt_with(norm, "SLOT-JAI-019"))
    assert r["code"] == "PRIORITY_RESERVED" and r["reserved_for"] == "CRITICAL" and r["fresh"]["options"]
    got = holds.hold_option(frozen, crit["quote_id"], opt_with(crit, "SLOT-JAI-019"))
    assert got["ok"]                                                # the CRITICAL load gets it
    # once the critical truck has chosen, the reservation is over: normal first-come again
    norm2 = quote(frozen, "SHP1012")
    assert all("SLOT-JAI-019" not in o["slot_ids"] for o in norm2["options"])
    assert holds.hold_option(frozen, norm2["quote_id"], 1)["ok"]


def test_no_surge_first_come_wins(frozen):
    quote(frozen, "SHP1014")
    norm = quote(frozen, "SHP1012")
    r = holds.hold_option(frozen, norm["quote_id"], opt_with(norm, "SLOT-JAI-019"))
    assert r["ok"]                                                  # quiet period: the DB decides, first valid commit


def test_reservation_expires_after_two_minutes(frozen):
    start_surge(frozen)
    quote(frozen, "SHP1014")
    with write_tx(frozen):
        clock.advance(frozen, 3)
    norm = quote(frozen, "SHP1012")
    assert holds.hold_option(frozen, norm["quote_id"], opt_with(norm, "SLOT-JAI-019"))["ok"]


def test_reject_reoffers_freed_slot_by_priority(frozen):
    from booking import bookings
    q = quote(frozen, "SHP1006")
    h = holds.hold_option(frozen, q["quote_id"], opt_with(q, "SLOT-JAI-019"))
    r = bookings.request_booking(frozen, h["hold_id"])
    out = bookings.reject(frozen, r["appointment_id"], "crew short")
    ids = [x["shipment_id"] for x in out["reoffer"]]
    # SHP1006 itself is now with a coordinator (rejection = hand-off); the slot goes to others, CRITICAL first
    assert ids and "SHP1006" not in ids and ids[0] == "SHP1014"
    ranks = {"CRITICAL": 4, "HIGH": 3, "NORMAL": 2, "LOW": 1}
    pri = [ranks[x["priority"]] for x in out["reoffer"]]
    assert pri == sorted(pri, reverse=True)                         # highest priority first
