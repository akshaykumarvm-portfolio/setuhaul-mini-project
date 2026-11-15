"""Decision 4 feasibility on the seeded edge cases. Clock frozen at 10:00, 4 Aug 2026."""
from booking import clock, quotes
from booking.db import write_tx
from booking.feasibility import check_feasibility, find_options, load_state
from booking.handoffs import build_record
from tests.conftest import set_now


def codes(v):
    return [r.code for r in v.reasons]


def options(conn, shipment_id, **kw):
    st = load_state(conn, shipment_id, clock.now(conn))
    return find_options(st, limit=50, **kw)


def test_shp1010_reefer_only_d5(frozen):
    opts, excluded = options(frozen, "SHP1010")
    assert opts and {o["dock_code"] for o in opts} == {"D5"}
    assert {e["dock_code"]: e["code"] for e in excluded} == {d: "DOCK_TYPE" for d in ["D1", "D2", "D3", "D4", "D6"]}
    # A standard slot is rejected outright for a reefer load
    v = check_feasibility(frozen, "SHP1010", ["SLOT-JAI-019"], clock.now(frozen))
    assert "DOCK_TYPE" in codes(v)


def test_shp1014_21t_d2_d4_only_d1_rejected(frozen):
    opts, excluded = options(frozen, "SHP1014")
    plain = {o["dock_code"] for o in opts if not o["needs_approval"]}
    assert plain == {"D2", "D4"}
    assert {o["dock_code"] for o in opts if o["needs_approval"]} <= {"D6"}  # heavy dock: approval, ranked last
    ex = {e["dock_code"]: e["code"] for e in excluded}
    assert ex["D1"] == "OVERWEIGHT" and ex["D3"] == "OVERWEIGHT" and ex["D5"] == "DOCK_TYPE"
    # A2: the existing pending booking APT1014A on D1 is infeasible
    v = check_feasibility(frozen, "SHP1014", ["SLOT-JAI-004"], clock.now(frozen), ignore_appointments=("APT1014A",))
    assert "OVERWEIGHT" in codes(v)
    # A6: 75-min unload needs two back-to-back 60-min slots; one slot is too short
    assert all(len(o["slot_ids"]) == 2 for o in opts if o["dock_code"] in ("D2", "D4"))
    assert "SLOT_TOO_SHORT" in codes(check_feasibility(frozen, "SHP1014", ["SLOT-JAI-019"], clock.now(frozen)))


def test_shp1016_heavy_only_d6_and_a8_flag(frozen):
    st = load_state(frozen, "SHP1016", clock.now(frozen))
    _, excluded = find_options(st)
    assert {e["dock_code"] for e in excluded if e["code"] == "DOCK_TYPE"} == {"D1", "D2", "D3", "D4", "D5"}
    # D6 12:30 (90-min slot) passes every slot rule; only the A8 contradiction blocks it
    v = check_feasibility(frozen, "SHP1016", ["SLOT-JAI-074"], clock.now(frozen))
    assert codes(v) == ["DRIVER_ON_MULTIPLE_TRUCKS"]
    # D6 11:00 is SHP1011's booked slot, and before SHP1016's ETA + 15 min
    assert {"SLOT_TAKEN", "BEFORE_ARRIVAL"} <= set(codes(check_feasibility(frozen, "SHP1016", ["SLOT-JAI-073"],
                                                                           clock.now(frozen))))


def test_breakdown_overlap_blocks_open_slot(frozen):
    # A3: SLOT-JAI-030 (D3 09:00-10:00) is OPEN but DEVT001 starts 09:15
    set_now(frozen, "2026-08-04T08:00:00+05:30")
    v = check_feasibility(frozen, "SHP1012", ["SLOT-JAI-030"], clock.now(frozen))
    assert "DOCK_EVENT" in codes(v)


def test_capacity_reduction_blocks_d2_early(frozen):
    # A4: DEVT003 on D2 08:00-09:20; SLOT-JAI-016 (D2 09:00) overlaps it
    set_now(frozen, "2026-08-04T07:00:00+05:30")
    v = check_feasibility(frozen, "SHP1003", ["SLOT-JAI-016"], clock.now(frozen))
    assert "DOCK_EVENT" in codes(v)


def test_booking_that_overruns_its_slot_occupies_the_dock(frozen):
    # APT1009 (SHP1009, 75 min) is on D4 11:00; it unloads until 12:15, so D4 12:00 is not free
    v = check_feasibility(frozen, "SHP1006", ["SLOT-JAI-047"], clock.now(frozen))
    assert "DOCK_OCCUPIED" in codes(v)


def test_eta_plus_gate_to_dock(frozen):
    # SHP1006 ETA 11:20 -> earliest 11:35, so 11:00 slots are out and 12:00 is the first
    opts, _ = options(frozen, "SHP1006")
    assert min(o["start"] for o in opts) == "2026-08-04T12:00:00+05:30"
    assert "BEFORE_ARRIVAL" in codes(check_feasibility(frozen, "SHP1006", ["SLOT-JAI-004"], clock.now(frozen)))


def test_low_confidence_range_plans_on_late_edge(frozen):
    # A12: SHP1017 "between 12:30 and 13:00" -> window 12:30-13:00, LOW, plan on 13:00 + 15
    st = load_state(frozen, "SHP1017", clock.now(frozen))
    assert st.eta.confidence == "LOW"
    assert clock.fmt_ts(st.eta.eta_lo)[11:16] == "12:30" and clock.fmt_ts(st.eta.eta_hi)[11:16] == "13:00"
    assert clock.fmt_ts(st.earliest_start)[11:16] == "13:15"


def test_latest_finish_constraint(frozen):
    finish = clock.parse_ts("2026-08-04T12:30:00+05:30")
    opts, _ = options(frozen, "SHP1006", latest_finish=finish)
    assert opts == []
    v = check_feasibility(frozen, "SHP1006", ["SLOT-JAI-019"], clock.now(frozen), latest_finish=finish)
    assert "AFTER_DRIVER_LATEST" in codes(v)


def test_last_new_start_rule(frozen):
    from datetime import time

    from booking.feasibility import evaluate
    set_now(frozen, "2026-08-04T20:30:00+05:30")
    # RULE005: no new start AFTER 21:00. A 21:00 start (D1 21:00-22:00) is allowed as-is.
    v = check_feasibility(frozen, "SHP1020", ["SLOT-JAI-014"], clock.now(frozen))
    assert v.ok and v.needs_approval == []
    # Past the limit it is allowed but flagged for coordinator approval, never silently rejected.
    st = load_state(frozen, "SHP1020", clock.now(frozen))
    st.last_new_start = time(20, 45)
    v = evaluate(st, ["SLOT-JAI-014"])
    assert v.ok and [r.code for r in v.needs_approval] == ["AFTER_LAST_START"]


def test_standard_on_heavy_dock_ranked_last_with_approval(frozen):
    opts, _ = options(frozen, "SHP1006")
    heavy = [i for i, o in enumerate(opts) if o["dock_code"] == "D6"]
    assert heavy and all(opts[i]["needs_approval"][0]["code"] == "STANDARD_ON_HEAVY_DOCK" for i in heavy)
    assert min(heavy) > max(i for i, o in enumerate(opts) if o["dock_code"] != "D6")


def test_cancelled_shipment_not_bookable(frozen):
    v = check_feasibility(frozen, "SHP1019", ["SLOT-JAI-019"], clock.now(frozen))
    assert codes(v) == ["SHIPMENT_NOT_ACTIVE"]


def test_wrong_facility(frozen):
    v = check_feasibility(frozen, "SHP1021", ["SLOT-JAI-019"], clock.now(frozen))  # Gurugram load, Jaipur slot
    assert codes(v) == ["WRONG_FACILITY"]


def test_quote_reserves_nothing(frozen):
    before = frozen.execute("SELECT SUM(version) AS v FROM appointment_slots").fetchone()["v"]
    with write_tx(frozen):
        q = quotes.build_quote(frozen, "SHP1006", clock.now(frozen))
    assert q["ok"] and q["reserved"] is False and q["options"]
    after = frozen.execute("SELECT SUM(version) AS v FROM appointment_slots").fetchone()["v"]
    assert before == after
    assert frozen.execute("SELECT COUNT(*) AS n FROM slot_holds").fetchone()["n"] == 0


def test_shp1015_no_feasible_slot_complete_handoff_record(frozen):
    # THR005: reefer, ETA 18:30, D5 in maintenance 18:00-22:00
    opts, _ = options(frozen, "SHP1015")
    assert opts == []
    rec = build_record(frozen, "SHP1015", clock.now(frozen), reason_code="NO_FEASIBLE_SLOT")
    for k in ["shipment", "driver", "eta", "driver_constraints", "docks_checked", "nearest_out_of_policy",
              "current_appointment"]:
        assert k in rec
    d5 = next(d for d in rec["docks_checked"] if d["dock_code"] == "D5")
    assert d5["code"] == "DOCK_EVENT" and "DEVT002" in d5["detail"]
    assert rec["eta"]["eta_hi"].startswith("2026-08-04T18:30")
