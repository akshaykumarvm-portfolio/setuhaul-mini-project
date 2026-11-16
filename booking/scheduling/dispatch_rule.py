"""Dispatch-rule baseline: highest priority first, then earliest dock start; each truck gets the earliest
compatible dock slot that doesn't overlap anything already placed. Approval-free docks are preferred; among
equal starts the tightest weight fit keeps big docks free. Frozen trucks keep their slot."""
from __future__ import annotations

from booking.scheduling.problem import Assignment, Problem, candidate_starts


def solve(p: Problem) -> dict:
    busy = {k: list(v) for k, v in p.fixed.items()}
    order = sorted(p.jobs, key=lambda j: (not j.frozen, -j.weight, j.release, j.shipment_id))   # weight: flagged = 1
    placed: dict[str, Assignment] = {}
    for j in order:
        best = None
        for k, approval in j.compat.items():
            for s in candidate_starts(p, j, k):
                if any(a < s + j.dur and s < b for a, b, _ in busy[k]):
                    continue
                after = p.last_start is not None and s > p.last_start
                key = (bool(approval) or after, s, p.docks[k]["max_kg"] - j.load_kg, p.docks[k]["code"])
                if best is None or key < best[0]:
                    best = (key, k, s)
                break  # starts are sorted: the first free one is this dock's earliest
        if best:
            _, k, s = best
            busy[k].append((s, s + j.dur, j.shipment_id))
            placed[j.shipment_id] = Assignment(j.shipment_id, k, s)
        else:
            placed[j.shipment_id] = Assignment(j.shipment_id, None, None)
    return {"status": "HEURISTIC", "assignments": [placed[j.shipment_id] for j in p.jobs]}
