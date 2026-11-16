"""Facility-level scheduling (Decision 8). Proposals only: nothing here writes a booking.

    propose(conn, "FAC-JAI-01", now) -> current plan vs dispatch-rule baseline vs CP-SAT, same metrics,
                                        each plan validated against the same rules.
CLI: python -m booking.scheduling --facility FAC-JAI-01 --at 10:00
"""
from __future__ import annotations

from datetime import datetime

from booking.scheduling import cpsat, dispatch_rule
from booking.scheduling.evaluate import current_plan, describe, metrics, objective, validate
from booking.scheduling.problem import build_problem, hhmm


def propose(conn, facility_id: str, now: datetime, *, time_limit_s: float = 5.0) -> dict:
    p = build_problem(conn, facility_id, now)
    cur = current_plan(p)
    base = dispatch_rule.solve(p)
    opt = cpsat.solve(p, time_limit_s=time_limit_s, hint=base["assignments"])   # never worse than the baseline
    # Decision 8 fallback: if CP-SAT found nothing usable, the baseline is the proposal.
    opt_plan = opt["assignments"] or base["assignments"]
    plans = {
        "current": {"label": "Current promises", "assignments": cur,
                    "violations": validate(p, cur, strict_slots=False)},
        "dispatch_rule": {"label": "Dispatch rule (priority, then earliest)", "assignments": base["assignments"],
                          "violations": validate(p, base["assignments"])},
        "cpsat": {"label": "CP-SAT (OR-Tools)", "assignments": opt_plan, "violations": validate(p, opt_plan),
                  "status": opt["status"], "solve_s": opt.get("solve_s"), "proven_optimal": opt.get("proven_optimal"),
                  "objective": opt.get("objective"), "best_bound": opt.get("best_bound")},
    }
    for key, v in plans.items():
        v["metrics"] = metrics(p, v["assignments"])
        if key != "current":
            v["metrics"]["objective"] = objective(p, v["assignments"])
        v["rows"] = describe(p, v.pop("assignments"))
    # CP-SAT only when it is strictly better on the shared objective; a tie keeps the simpler rule (Decision 8)
    better = plans["cpsat"]["metrics"]["objective"] < plans["dispatch_rule"]["metrics"]["objective"]
    return {
        "ok": True, "facility_id": facility_id, "sim_now": hhmm(p.now), "trucks": len(p.jobs),
        "docks": {k: v["code"] for k, v in p.docks.items()},
        "fixed": {p.docks[k]["code"]: [{"start": hhmm(a), "end": hhmm(b), "why": w} for a, b, w in v]
                  for k, v in p.fixed.items()},
        "plans": plans,
        "recommendation": "cpsat" if better else "dispatch_rule",
        "objective_weights": {"lateness_per_min_x_priority": 100, "waiting_per_min": 1, "promise_changed": 30,
                              "after_21_00": 10000, "standard_on_heavy_dock": 10000, "unscheduled_x_priority": 100000},
        "note": "Proposal only. Nothing is booked; each change still goes through hold -> request -> confirm.",
    }


def proposal_for(conn, shipment_id: str, now: datetime) -> dict:
    """One truck's line from the recommended plan (what a driver may be told: not reserved)."""
    row = conn.execute("SELECT destination_facility_id AS f FROM shipments WHERE shipment_id = %s", (shipment_id,)).fetchone()
    if row is None:
        return {"ok": False, "code": "UNKNOWN_SHIPMENT"}
    r = propose(conn, row["f"], now, time_limit_s=3.0)
    plan = r["plans"][r["recommendation"]]
    mine = next((x for x in plan["rows"] if x["shipment_id"] == shipment_id), None)
    if mine is None:
        return {"ok": False, "code": "NOT_IN_PLAN", "message": "This load isn't waiting for a dock."}
    return {"ok": True, "shipment_id": shipment_id, "method": plan["label"], "dock": mine["dock"],
            "start": mine["start"], "end": mine["end"], "promised": mine["promised"], "reserved": False,
            "note": "Facility-wide suggestion only; not reserved. Use find_slot_options/hold_option to book."}
