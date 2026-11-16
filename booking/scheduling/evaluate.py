"""Score and validate plans on the same metrics: current plan vs dispatch rule vs CP-SAT."""
from __future__ import annotations

from booking.scheduling.problem import Assignment, Problem, hhmm


def current_plan(p: Problem) -> list[Assignment]:
    return [Assignment(j.shipment_id, j.promised_dock, j.promised_start) for j in p.jobs]


def validate(p: Problem, plan: list[Assignment], *, strict_slots: bool = True) -> list[str]:
    """Every rule a proposal must respect. Returns violations (empty = valid)."""
    jobs = {j.shipment_id: j for j in p.jobs}
    out = []
    by_dock: dict[str, list[tuple[int, int, str]]] = {}
    for a in plan:
        if not a.scheduled:
            continue
        j = jobs[a.shipment_id]
        if a.dock_id not in j.compat:
            out.append(f"{a.shipment_id}: dock {p.docks[a.dock_id]['code']} incompatible (type/weight)")
        if a.start < max(j.release, p.now) and not j.frozen:
            out.append(f"{a.shipment_id}: starts {hhmm(a.start)} before it can be at the dock ({hhmm(j.release)})")
        if strict_slots and a.start not in p.slot_starts.get(a.dock_id, []):
            out.append(f"{a.shipment_id}: {hhmm(a.start)} is not an open slot start on {p.docks[a.dock_id]['code']}")
        for fa, fb, why in p.fixed.get(a.dock_id, []):
            if fa < a.start + j.dur and a.start < fb:
                out.append(f"{a.shipment_id}: overlaps {why} on {p.docks[a.dock_id]['code']}")
        by_dock.setdefault(a.dock_id, []).append((a.start, a.start + j.dur, a.shipment_id))
    for k, ivs in by_dock.items():
        ivs.sort()
        for (s1, e1, j1), (s2, e2, j2) in zip(ivs, ivs[1:]):
            if s2 < e1:
                out.append(f"{j1} and {j2} overlap on {p.docks[k]['code']}")
    return out


def metrics(p: Problem, plan: list[Assignment]) -> dict:
    jobs = {j.shipment_id: j for j in p.jobs}
    wl = wait = moved = unsched = approvals = busy = 0
    late_trucks = []
    for a in plan:
        j = jobs[a.shipment_id]
        if not a.scheduled:
            unsched += 1
            continue
        busy += j.dur
        wait += max(0, a.start - max(j.release, p.now))
        due = j.promised_start if j.promised_start is not None else max(j.release, p.now)
        late = max(0, a.start - due)
        wl += j.weight * late
        if late:
            late_trucks.append(a.shipment_id)
        if j.promised_start is not None and (a.dock_id, a.start) != (j.promised_dock, j.promised_start):
            moved += 1
        if j.compat.get(a.dock_id) or (p.last_start is not None and a.start > p.last_start):
            approvals += 1
    open_min = 0
    for k, starts in p.slot_starts.items():
        lo = p.now
        blocked = sum(max(0, min(b, p.day_end) - max(a, lo)) for a, b, _ in p.fixed[k])
        open_min += max(0, p.day_end - lo - blocked) if starts else 0
    return {"weighted_lateness": wl, "waiting_min": wait, "promises_changed": moved, "unscheduled": unsched,
            "needs_approval": approvals, "late_trucks": len(late_trucks),
            "utilisation_pct": round(100 * busy / open_min, 1) if open_min else 0.0}


def describe(p: Problem, plan: list[Assignment]) -> list[dict]:
    jobs = {j.shipment_id: j for j in p.jobs}
    rows = []
    for a in plan:
        j = jobs[a.shipment_id]
        code = p.docks[a.dock_id]["code"] if a.dock_id else None
        pcode = p.docks[j.promised_dock]["code"] if j.promised_dock else None
        rows.append({
            "shipment_id": j.shipment_id, "priority": j.priority, "eta": j.eta, "earliest": hhmm(j.release),
            "unload_min": j.dur, "promised": f"{pcode} {hhmm(j.promised_start)}" if pcode else None,
            "promise_status": j.promise_status, "dock": code, "start": hhmm(a.start),
            "end": hhmm(a.start + j.dur) if a.scheduled else None,
            "late_min": (max(0, a.start - (j.promised_start if j.promised_start is not None else max(j.release, p.now)))
                         if a.scheduled else None),
            "changed": a.scheduled and j.promised_start is not None and (a.dock_id, a.start) != (j.promised_dock, j.promised_start),
            "needs_approval": bool(a.scheduled and (j.compat.get(a.dock_id) or (p.last_start and a.start > p.last_start))),
            "frozen": j.frozen, "flags": j.flags,
        })
    return rows


def objective(p: Problem, plan: list[Assignment]) -> int:
    """The CP-SAT objective, computed for any plan, so the baseline is scored on exactly the same terms."""
    from booking.scheduling.cpsat import P_AFTER_LAST, P_APPROVAL_DOCK, W_LATE, W_MOVED, W_UNSCHEDULED
    jobs = {j.shipment_id: j for j in p.jobs}
    total = 0
    for a in plan:
        j = jobs[a.shipment_id]
        if not a.scheduled:
            total += W_UNSCHEDULED * j.weight
            continue
        due = j.promised_start if j.promised_start is not None else max(j.release, p.now)
        total += (a.start - max(j.release, p.now)) + W_LATE * j.weight * max(0, a.start - due)
        if j.promised_start is not None and (a.dock_id, a.start) != (j.promised_dock, j.promised_start):
            total += W_MOVED
        if p.last_start is not None and a.start > p.last_start:
            total += P_AFTER_LAST
        if j.compat.get(a.dock_id):
            total += P_APPROVAL_DOCK
    return total
