"""Decision 6 lifecycle: option -> hold -> request -> confirmed / rejected / lapsed, plus Decision 9 edge cases."""
from booking import bookings, clock, handoffs, holds, quotes, service, sweeper
from booking.db import write_tx
from tests.conftest import add_synthetic_shipments, set_now


def quote(conn, sid, **kw):
    with write_tx(conn):
        return quotes.build_quote(conn, sid, clock.now(conn), **kw)


def opt_no(q, slot_ids):
    return next(o["option_no"] for o in q["options"] if o["slot_ids"] == slot_ids)


def appt(conn, appt_id):
    return conn.execute("SELECT * FROM appointments WHERE appointment_id = %s", (appt_id,)).fetchone()


def test_full_flow_old_slot_kept_until_swap(frozen):
    q = quote(frozen, "SHP1006")
    h = holds.hold_option(frozen, q["quote_id"], opt_no(q, ["SLOT-JAI-019"]))
    assert h["ok"] and h["status"] == "HELD" and h["dock_code"] == "D2"
    r = bookings.request_booking(frozen, h["hold_id"])
    assert r["ok"] and r["status"] == "PENDING_CONFIRMATION" and r["replaces"] == "APT1006"
    # While pending: the old appointment is still current and confirmed; the new slot is blocked for others
    assert appt(frozen, "APT1006")["appointment_status"] == "CONFIRMED" and appt(frozen, "APT1006")["is_current"] == 1
    other = quote(frozen, "SHP1012")
    assert all("SLOT-JAI-019" not in o["slot_ids"] for o in other["options"])
    st = service.shipment_status(frozen, "SHP1006")
    assert st["stage"] == "PENDING" and st["current_appointment"]["appointment_id"] == "APT1006"
    c = bookings.confirm(frozen, r["appointment_id"])
    assert c["ok"] and c["cancelled"] == ["APT1006"] and c["freed_slot_ids"] == ["SLOT-JAI-045"]
    new, old = appt(frozen, r["appointment_id"]), appt(frozen, "APT1006")
    assert (new["appointment_status"], new["is_current"]) == ("CONFIRMED", 1)
    assert (old["appointment_status"], old["is_current"]) == ("CANCELLED", 0)
    assert service.shipment_status(frozen, "SHP1006")["stage"] == "CONFIRMED"


def test_multi_slot_booking_claims_both_slots(frozen):
    q = quote(frozen, "SHP1014")
    h = holds.hold_option(frozen, q["quote_id"], opt_no(q, ["SLOT-JAI-019", "SLOT-JAI-020"]))
    r = bookings.request_booking(frozen, h["hold_id"])
    assert r["ok"] and sorted(r["slot_ids"]) == ["SLOT-JAI-019", "SLOT-JAI-020"]
    claims = frozen.execute("SELECT slot_id FROM appointment_slot_claims WHERE appointment_id = %s AND active = 1"
                            " ORDER BY slot_id", (r["appointment_id"],)).fetchall()
    assert [c["slot_id"] for c in claims] == ["SLOT-JAI-019", "SLOT-JAI-020"]
    c = bookings.confirm(frozen, r["appointment_id"])
    assert c["ok"] and c["cancelled"] == ["APT1014A"]  # the overweight pending D1 booking (A2) is replaced


def test_reject_frees_slot_and_hands_off(frozen):
    q = quote(frozen, "SHP1006")
    h = holds.hold_option(frozen, q["quote_id"], 1)
    r = bookings.request_booking(frozen, h["hold_id"])
    j = bookings.reject(frozen, r["appointment_id"], "Dock crew short at 12:00")
    assert j["ok"] and j["status"] == "REJECTED" and j["handoff_id"]
    assert appt(frozen, "APT1006")["is_current"] == 1  # driver still has the old slot
    hof = handoffs.list_handoffs(frozen)[0]
    assert hof["reason_code"] == "REQUEST_REJECTED" and hof["shipment_id"] == "SHP1006"
    assert any(set(o["slot_ids"]) == set(h["slot_ids"]) for o in quote(frozen, "SHP1012")["options"])


def test_slot_taken_by_another_hold_returns_fresh_options(frozen):
    a, b = add_synthetic_shipments(frozen, 2)
    qa, qb = quote(frozen, a), quote(frozen, b)
    target = ["SLOT-JAI-019"]
    assert holds.hold_option(frozen, qa["quote_id"], opt_no(qa, target))["ok"]
    r = holds.hold_option(frozen, qb["quote_id"], opt_no(qb, target))
    assert r["code"] == "SLOT_TAKEN" and r["fresh"]["options"]
    assert all(o["slot_ids"] != target for o in r["fresh"]["options"])


def test_stale_option_when_slot_changed_but_free(frozen):
    a, b = add_synthetic_shipments(frozen, 2)
    qb = quote(frozen, b)
    qa = quote(frozen, a)
    h = holds.hold_option(frozen, qa["quote_id"], opt_no(qa, ["SLOT-JAI-019"]))
    holds.release_hold(frozen, h["hold_id"])            # slot is free again, but its version moved
    r = holds.hold_option(frozen, qb["quote_id"], opt_no(qb, ["SLOT-JAI-019"]))
    assert r["code"] == "STALE" and r["fresh"]["options"]


def test_one_live_hold_per_shipment(frozen):
    q = quote(frozen, "SHP1006")
    h1 = holds.hold_option(frozen, q["quote_id"], 1)
    q2 = quote(frozen, "SHP1006")
    h2 = holds.hold_option(frozen, q2["quote_id"], 2)
    assert h1["ok"] and h2["ok"]
    states = {r["hold_id"]: r["state"] for r in frozen.execute("SELECT hold_id, state FROM slot_holds")}
    assert states == {h1["hold_id"]: "RELEASED", h2["hold_id"]: "ACTIVE"}


def test_hold_expires_and_request_refused(frozen):
    q = quote(frozen, "SHP1006")
    h = holds.hold_option(frozen, q["quote_id"], 1)
    set_now(frozen, "2026-08-04T10:06:00+05:30")
    res = sweeper.sweep(frozen)
    assert h["hold_id"] in res["expired_holds"] and res["freed"]
    assert bookings.request_booking(frozen, h["hold_id"])["code"] == "HOLD_EXPIRED"


def test_pending_lapses_and_escalates(frozen):
    q = quote(frozen, "SHP1006")
    h = holds.hold_option(frozen, q["quote_id"], 1)
    r = bookings.request_booking(frozen, h["hold_id"])
    assert r["expires_at"] == "2026-08-04T10:15:00+05:30"  # 15 min after request (slot is at 12:00)
    set_now(frozen, "2026-08-04T10:16:00+05:30")
    res = sweeper.sweep(frozen)
    assert r["appointment_id"] in res["lapsed_requests"]
    a = appt(frozen, r["appointment_id"])
    assert a["appointment_status"] == "CANCELLED"
    assert appt(frozen, "APT1006")["is_current"] == 1
    assert bookings.confirm(frozen, r["appointment_id"])["code"] == "INVALID_STATE"
    assert any(x["reason_code"] == "PENDING_LAPSED" for x in handoffs.list_handoffs(frozen))


def test_pending_cutoff_30_min_before_start(frozen):
    set_now(frozen, "2026-08-04T11:20:00+05:30")
    q = quote(frozen, "SHP1006")
    h = holds.hold_option(frozen, q["quote_id"], opt_no(q, ["SLOT-JAI-019"]))
    r = bookings.request_booking(frozen, h["hold_id"])
    assert r["expires_at"] == "2026-08-04T11:30:00+05:30"   # 12:00 start - 30 min beats 11:35


def test_idempotent_writes(frozen):
    q = quote(frozen, "SHP1006")
    h1 = holds.hold_option(frozen, q["quote_id"], 1, idem_key="k-hold-1")
    h2 = holds.hold_option(frozen, q["quote_id"], 1, idem_key="k-hold-1")
    assert h1["hold_id"] == h2["hold_id"] and h2["idempotent_replay"]
    r1 = bookings.request_booking(frozen, h1["hold_id"], idem_key="k-req-1")
    r2 = bookings.request_booking(frozen, h1["hold_id"], idem_key="k-req-1")
    assert r1["appointment_id"] == r2["appointment_id"]
    assert frozen.execute("SELECT COUNT(*) AS n FROM appointments WHERE shipment_id='SHP1006'"
                          " AND appointment_status='PENDING_CONFIRMATION'").fetchone()["n"] == 1
    r3 = bookings.request_booking(frozen, h1["hold_id"], idem_key="k-req-2")
    assert r3["code"] == "ALREADY_REQUESTED" and r3["appointment_id"] == r1["appointment_id"]


def test_low_confidence_eta_blocks_hold_until_confirmed(frozen):
    q = quote(frozen, "SHP1013")   # "late by one hour" -> LOW
    assert q["eta"]["confidence"] == "LOW" and q["options"]
    assert holds.hold_option(frozen, q["quote_id"], 1)["code"] == "ETA_UNCONFIRMED"
    e = service.record_eta(frozen, "SHP1013", "2026-08-04T11:00:00+05:30", "2026-08-04T11:10:00+05:30", "HIGH",
                           cause="TRAFFIC", source_text="Reaching gate around 11:00, latest 11:10")
    assert e["ok"] and not e["needs_clarification"]
    q2 = quote(frozen, "SHP1013")
    assert holds.hold_option(frozen, q2["quote_id"], 1)["ok"]


def test_new_eta_rechecks_current_slot_and_hold(frozen):
    q = quote(frozen, "SHP1006")
    h = holds.hold_option(frozen, q["quote_id"], opt_no(q, ["SLOT-JAI-019"]))  # D2 12:00
    e = service.record_eta(frozen, "SHP1006", "2026-08-04T12:10:00+05:30", "2026-08-04T12:20:00+05:30", "HIGH",
                           cause="TRAFFIC", source_text="Stuck again, 12:20 now")
    assert e["released_hold"] == h["hold_id"]
    assert e["current_appointment"]["still_fits"] is False   # APT1006 10:00-11:00 no longer works


def test_contradictory_driver_data_cannot_book(frozen):
    q = quote(frozen, "SHP1007")   # A8: DRV007 on two trucks
    assert q["options"] == [] and q["flags"][0]["code"] == "DRIVER_ON_MULTIPLE_TRUCKS"
    r = handoffs.escalate(frozen, "SHP1007", "CONTRADICTORY_DATA", "Driver on two trucks at once")
    assert r["ok"] and r["record"]["data_flags"]


def test_escalate_no_slot_shp1015(frozen):
    r = handoffs.escalate(frozen, "SHP1015", "NO_FEASIBLE_SLOT", "Reefer dock in maintenance after the new ETA")
    assert r["ok"]
    assert r["routed_to"]["contact_role"] == "SHIFT_MANAGER"
    assert r["channel_status"].startswith("console: QUEUED")
    assert r["record"]["feasible_options"] == []
    assert frozen.execute("SELECT state FROM handoffs WHERE handoff_id = %s", (r["handoff_id"],)).fetchone()["state"] == "OPEN"


def test_api_status_codes(db_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_url)
    from fastapi.testclient import TestClient

    from booking.api import app
    with TestClient(app) as client:
        client.post("/admin/clock", json={"sim_ts": "2026-08-04T10:00:00+05:30", "rate": 0})
        q = client.post("/quotes", json={"shipment_id": "SHP1006"}).json()
        h = client.post("/holds", json={"quote_id": q["quote_id"], "option_no": 1}, headers={"Idempotency-Key": "x1"})
        assert h.status_code == 200
        b = client.post("/bookings", json={"hold_id": h.json()["hold_id"]})
        assert b.status_code == 200 and b.json()["status"] == "PENDING_CONFIRMATION"
        assert b.json()["appointment_id"] in {x["appointment_id"] for x in client.get("/bookings").json()["bookings"]}
        again = client.post("/bookings", json={"hold_id": h.json()["hold_id"]})
        assert again.status_code == 409 and again.json()["code"] == "ALREADY_REQUESTED"
        assert client.get("/shipments/NOPE/status").status_code == 404
        c = client.post(f"/bookings/{b.json()['appointment_id']}/confirm", json={"actor": "neha"})
        assert c.json()["status"] == "CONFIRMED"
        assert client.get("/facilities/FAC-JAI-01/timeline").status_code == 200
