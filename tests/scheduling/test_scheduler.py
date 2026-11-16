"""Phase 5: CP-SAT vs dispatch-rule baseline on the snapshot. Both plans must obey every rule;
CP-SAT (hinted with the baseline) is never worse and is strictly better where the greedy order errs."""
import pytest

from booking import clock
from booking.scheduling import cpsat, dispatch_rule, propose
from booking.scheduling.evaluate import metrics, objective, validate
from booking.scheduling.problem import build_problem
from tests.conftest import set_now

TIMES = ["09:00", "10:00", "11:00", "12:00"]


def problem(conn, t):
    set_now(conn, f"2026-08-04T{t}:00+05:30")
    return build_problem(conn, "FAC-JAI-01", clock.now(conn))


@pytest.mark.parametrize("t", TIMES)
def test_both_plans_valid_and_cpsat_never_worse(conn, t):
    p = problem(conn, t)
    base = dispatch_rule.solve(p)["assignments"]
    opt = cpsat.solve(p, time_limit_s=5, hint=base)
    assert opt["status"] in ("OPTIMAL", "FEASIBLE")
    assert validate(p, base) == []
    assert validate(p, opt["assignments"]) == []
    assert objective(p, opt["assignments"]) <= objective(p, base)


def test_cpsat_beats_greedy_at_0900(conn):
    p = problem(conn, "09:00")
    base = dispatch_rule.solve(p)["assignments"]
    opt = cpsat.solve(p, time_limit_s=10, hint=base)
    assert opt["status"] == "OPTIMAL"
    assert metrics(p, opt["assignments"])["weighted_lateness"] < metrics(p, base)["weighted_lateness"]


def test_dock_rules_respected(conn):
    p = problem(conn, "10:00")
    plan = {a.shipment_id: a for a in cpsat.solve(p, time_limit_s=5, hint=dispatch_rule.solve(p)["assignments"])["assignments"]}
    code = lambda sid: p.docks[plan[sid].dock_id]["code"] if plan[sid].dock_id else None
    assert code("SHP1010") == "D5"                    # reefer only on the reefer dock
    assert code("SHP1016") == "D6"                    # 31 t only on the heavy dock
    assert code("SHP1014") in ("D2", "D4")            # 21 t: never D1/D3 (20 t)
    assert code("SHP1015") is None                    # D5 in maintenance after its ETA -> hand-off, not a made-up slot
    d3 = next(k for k, v in p.docks.items() if v["code"] == "D3")
    for a in plan.values():                           # nothing inside the D3 breakdown (09:15-13:00)
        if a.dock_id == d3:
            assert a.start >= 13 * 60


def test_current_promises_flagged(conn):
    p = problem(conn, "10:00")
    from booking.scheduling.evaluate import current_plan
    v = validate(p, current_plan(p), strict_slots=False)
    assert any("SHP1014: dock D1 incompatible" in x for x in v)              # A2 overweight pending booking
    assert any("SHP1005" in x and "breakdown" in x for x in v)              # A3/A14 slot inside a breakdown


def test_frozen_horizon_keeps_reachable_confirmed_slot(conn):
    p = problem(conn, "10:30")                         # SHP1011 confirmed D6 11:00, ETA 10:45 -> reachable at 11:00
    j = next(j for j in p.jobs if j.shipment_id == "SHP1011")
    assert j.frozen
    plan = {a.shipment_id: a for a in dispatch_rule.solve(p)["assignments"]}
    assert (plan["SHP1011"].dock_id, plan["SHP1011"].start) == (j.promised_dock, j.promised_start)


def test_propose_is_read_only(conn):
    set_now(conn, "2026-08-04T10:00:00+05:30")
    before = conn.execute("SELECT COUNT(*) AS n, SUM(version) AS v FROM appointment_slots").fetchone()
    appts = conn.execute("SELECT COUNT(*) AS n FROM appointments").fetchone()["n"]
    r = propose(conn, "FAC-JAI-01", clock.now(conn), time_limit_s=3)
    assert r["recommendation"] in ("cpsat", "dispatch_rule") and r["plans"]["cpsat"]["violations"] == []
    assert conn.execute("SELECT COUNT(*) AS n, SUM(version) AS v FROM appointment_slots").fetchone() == before
    assert conn.execute("SELECT COUNT(*) AS n FROM appointments").fetchone()["n"] == appts


def test_proposal_endpoints_and_access(db_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_url)
    from fastapi.testclient import TestClient

    from booking.api import app
    with TestClient(app) as c:
        c.post("/admin/clock", json={"sim_ts": "2026-08-04T10:00:00+05:30", "rate": 0})
        r = c.get("/facilities/FAC-JAI-01/proposal?time_limit=2").json()
        assert set(r["plans"]) == {"current", "dispatch_rule", "cpsat"} and r["trucks"] == 16
        mine = c.get("/shipments/SHP1006/proposal", headers={"X-Driver-Id": "DRV006"}).json()
        assert mine["ok"] and mine["reserved"] is False and mine["dock"]
        assert c.get("/shipments/SHP1014/proposal", headers={"X-Driver-Id": "DRV006"}).status_code == 403
