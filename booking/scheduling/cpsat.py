"""CP-SAT facility schedule (OR-Tools). Proposes only; never commits.

Variables per (truck j, compatible dock k): x[j,k] (truck uses dock), s[j,k] in {open slot starts on k
the truck can reach}, and an optional fixed-size interval [s, s + unload). Each truck takes exactly one
dock or stays unscheduled (heavily penalised, so one impossible truck never makes the model infeasible).
AddNoOverlap per dock over trucks + fixed intervals (in-progress unloads, dock events, blocked slots).

Objective (lexicographic by weights, as Decision 8):
    100 * w_j * lateness_j   (minutes past the due time: the promised start, or the earliest
                              dock start for a truck with no promise; priority weight w)
  +       waiting_j          (minutes from earliest dock start to proposed start)
  +  30 * moved_j            (promise changed: different dock or time)
  + approval penalties       (start after 21:00 / standard load on the heavy dock)
  + 100000 * w_j if unscheduled
"""
from __future__ import annotations

import os
import time

from ortools.sat.python import cp_model

from booking.scheduling.problem import Assignment, Problem, candidate_starts

W_LATE, W_MOVED, W_UNSCHEDULED = 100, 30, 100_000
# Approval-needing choices are last resorts (project default): ~50 min of NORMAL lateness each.
P_AFTER_LAST, P_APPROVAL_DOCK = 10_000, 10_000


def merge(blocks: list[tuple[int, int, str]]) -> list[tuple[int, int]]:
    """Union of fixed blocks (a breakdown and the slots it blocks overlap; NoOverlap needs them disjoint)."""
    out: list[list[int]] = []
    for a, b, _ in sorted(blocks):
        if b <= a:
            continue
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def solve(p: Problem, *, time_limit_s: float = 5.0, workers: int | None = None, seed: int = 7,
          hint: list[Assignment] | None = None) -> dict:
    """hint: a known valid plan (the dispatch-rule baseline). CP-SAT starts from it, so within the time
    limit it can only match or beat it."""
    m = cp_model.CpModel()
    intervals: dict[str, list] = {k: [] for k in p.docks}
    choice: dict[tuple[str, str], tuple] = {}
    cost_terms = []
    unscheduled = {}
    for j in p.jobs:
        lits = []
        for k, approval in j.compat.items():
            starts = candidate_starts(p, j, k)
            if not starts:
                continue
            x = m.NewBoolVar(f"x_{j.shipment_id}_{k}")
            s = m.NewIntVarFromDomain(cp_model.Domain.FromValues(starts), f"s_{j.shipment_id}_{k}")
            intervals[k].append(m.NewOptionalFixedSizeIntervalVar(s, j.dur, x, f"iv_{j.shipment_id}_{k}"))
            lits.append(x)
            choice[(j.shipment_id, k)] = (x, s)
            hi = max(starts)
            cost = m.NewIntVar(0, 10_000_000, f"c_{j.shipment_id}_{k}")
            expr = s - max(j.release, p.now)                                  # waiting
            due = j.promised_start if j.promised_start is not None else max(j.release, p.now)
            late = m.NewIntVar(0, max(0, hi - due), f"late_{j.shipment_id}_{k}")
            m.Add(late >= s - due)
            expr += W_LATE * j.weight * late
            if j.promised_start is not None:
                if k == j.promised_dock:
                    same = m.NewBoolVar(f"same_{j.shipment_id}_{k}")
                    m.Add(s == j.promised_start).OnlyEnforceIf(same)
                    m.Add(s != j.promised_start).OnlyEnforceIf(same.Not())
                    expr += W_MOVED * (1 - same)
                else:
                    expr += W_MOVED
            if p.last_start is not None and hi > p.last_start:
                after = m.NewBoolVar(f"after_{j.shipment_id}_{k}")
                m.Add(s > p.last_start).OnlyEnforceIf(after)
                m.Add(s <= p.last_start).OnlyEnforceIf(after.Not())
                expr += P_AFTER_LAST * after
            if approval:
                expr += P_APPROVAL_DOCK
            m.Add(cost >= expr).OnlyEnforceIf(x)
            cost_terms.append(cost)
        u = m.NewBoolVar(f"u_{j.shipment_id}")
        unscheduled[j.shipment_id] = u
        m.AddExactlyOne(lits + [u])
        cost_terms.append(W_UNSCHEDULED * j.weight * u)
    for k, ivs in intervals.items():
        fixed = [m.NewFixedSizeIntervalVar(a, b - a, f"fx_{k}_{i}") for i, (a, b) in enumerate(merge(p.fixed[k]))]
        m.AddNoOverlap(ivs + fixed)
    m.Minimize(sum(cost_terms))
    if hint:
        placed = {a.shipment_id: a for a in hint}
        for j in p.jobs:
            a = placed.get(j.shipment_id)
            for k in j.compat:
                if (j.shipment_id, k) not in choice:
                    continue
                x, s = choice[(j.shipment_id, k)]
                use = a is not None and a.dock_id == k
                m.AddHint(x, use)
                if use:
                    m.AddHint(s, a.start)
            m.AddHint(unscheduled[j.shipment_id], a is None or not a.scheduled)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.num_search_workers = workers or max(2, min(8, os.cpu_count() or 2))  # t3.micro has 2 vCPUs
    solver.parameters.random_seed = seed
    t0 = time.perf_counter()
    status = solver.Solve(m)
    elapsed = time.perf_counter() - t0
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {"status": solver.StatusName(status), "assignments": [], "solve_s": round(elapsed, 3)}
    out = []
    for j in p.jobs:
        a = Assignment(j.shipment_id, None, None)
        for k in j.compat:
            if (j.shipment_id, k) in choice and solver.Value(choice[(j.shipment_id, k)][0]):
                a = Assignment(j.shipment_id, k, solver.Value(choice[(j.shipment_id, k)][1]))
        out.append(a)
    return {"status": solver.StatusName(status), "objective": solver.ObjectiveValue(),
            "best_bound": solver.BestObjectiveBound(), "solve_s": round(elapsed, 3), "assignments": out,
            "proven_optimal": status == cp_model.OPTIMAL}
