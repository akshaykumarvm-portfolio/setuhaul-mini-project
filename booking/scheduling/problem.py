"""Facility scheduling problem (Decision 8), built from structured state only (never chat text).

Mapping from the brief: truck = job, dock = machine, ETA window late edge (+ gate-to-dock) or
gate-in = release time, expected unload minutes = processing time, current appointment = promise
(due time), in-progress unload / dock events / frozen horizon = fixed intervals, priority = weight.
Times are minutes after midnight on the snapshot day (Asia/Kolkata).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from booking.bookings import claimed_slots, current_appointment
from booking.clock import parse_ts
from booking.feasibility import LIVE_STATUSES, dock_compatibility, load_state

PRIORITY_WEIGHT = {"CRITICAL": 8, "HIGH": 4, "NORMAL": 2, "LOW": 1}
FROZEN_HORIZON_MIN = 60
LAST_START_MIN = 21 * 60
DAY_END_MIN = 22 * 60


def mins(t: datetime) -> int:
    return t.hour * 60 + t.minute


def hhmm(m: int | None) -> str | None:
    return None if m is None else f"{m // 60:02d}:{m % 60:02d}"


@dataclass
class Job:
    shipment_id: str
    priority: str
    weight: int
    load_kg: int
    dur: int
    release: int                         # earliest dock start
    compat: dict[str, str | None]        # dock_id -> approval code (None = fine)
    promised_dock: str | None = None
    promised_start: int | None = None
    promised_appt: str | None = None
    promise_status: str | None = None
    frozen: bool = False
    flags: list[str] = field(default_factory=list)
    eta: str = ""


@dataclass
class Problem:
    facility_id: str
    now: int
    docks: dict[str, dict]                         # dock_id -> {code, type, max_kg}
    slot_starts: dict[str, list[int]]              # OPEN slot starts per dock
    fixed: dict[str, list[tuple[int, int, str]]]   # blocked time per dock: (start, end, why)
    jobs: list[Job]
    last_start: int | None = LAST_START_MIN
    day_end: int = DAY_END_MIN


def build_problem(conn, facility_id: str, now: datetime) -> Problem:
    now_m = mins(now)
    docks = {d["dock_id"]: {"code": d["dock_code"], "type": d["dock_type"], "max_kg": d["max_vehicle_weight_kg"]}
             for d in conn.execute("SELECT * FROM docks WHERE facility_id = %s ORDER BY dock_code", (facility_id,))}
    slot_starts: dict[str, list[int]] = {k: [] for k in docks}
    fixed: dict[str, list[tuple[int, int, str]]] = {k: [] for k in docks}
    for s in conn.execute("SELECT * FROM appointment_slots WHERE facility_id = %s ORDER BY slot_start_ts", (facility_id,)):
        a, b = mins(parse_ts(s["slot_start_ts"])), mins(parse_ts(s["slot_end_ts"]))
        if s["slot_status"] == "OPEN":
            slot_starts[s["dock_id"]].append(a)
        else:
            fixed[s["dock_id"]].append((a, b, f"slot {s['slot_status'].lower()}"))
    for e in conn.execute("SELECT e.* FROM dock_status_events e JOIN docks d USING (dock_id) WHERE d.facility_id = %s"
                          " AND e.event_type <> 'REOPENED'", (facility_id,)):
        end = mins(parse_ts(e["event_end_ts"])) if e["event_end_ts"] else DAY_END_MIN
        fixed[e["dock_id"]].append((mins(parse_ts(e["event_start_ts"])), end, f"{e['event_type'].lower()} {e['dock_event_id']}"))
    # trucks already unloading: their dock is busy until expected end (or now, if overrunning)
    for r in conn.execute(
            "SELECT c.actual_dock_id, c.unload_start_ts, c.dock_in_ts, s.expected_unload_min, s.shipment_id"
            " FROM facility_checkins c JOIN shipments s USING (shipment_id) WHERE c.facility_id = %s"
            " AND c.queue_state = 'IN_DOCK' AND c.unload_end_ts IS NULL", (facility_id,)):
        began = mins(parse_ts(r["unload_start_ts"] or r["dock_in_ts"]))
        fixed[r["actual_dock_id"]].append((began, max(began + r["expected_unload_min"], now_m + 1),
                                          f"{r['shipment_id']} unloading"))

    jobs: list[Job] = []
    ships = conn.execute(
        "SELECT s.shipment_id FROM shipments s LEFT JOIN facility_checkins c USING (shipment_id)"
        " WHERE s.destination_facility_id = %s AND s.current_status = ANY(%s)"
        " AND COALESCE(c.queue_state, '') NOT IN ('IN_DOCK', 'COMPLETED') ORDER BY s.shipment_id",
        (facility_id, list(LIVE_STATUSES))).fetchall()
    for row in ships:
        st = load_state(conn, row["shipment_id"], now)
        sh = st.shipment
        compat = {}
        for dock_id, dock in st.docks.items():
            ok, _, approval = dock_compatibility(st, dock)
            if ok:
                compat[dock_id] = approval.code if approval else None
        # contradictory data (A8) needs a person first: such a truck never displaces confirmed work in a plan
        weight = 1 if st.flags else PRIORITY_WEIGHT[sh["priority_code"]]
        job = Job(shipment_id=sh["shipment_id"], priority=sh["priority_code"],
                  weight=weight, load_kg=sh["load_weight_kg"],
                  dur=sh["expected_unload_min"], release=mins(st.earliest_start), compat=compat,
                  flags=[f.code for f in st.flags],
                  eta=f"{st.eta.eta_hi:%H:%M} {st.eta.confidence}")
        cur = current_appointment(conn, sh["shipment_id"])
        if cur:
            slots = claimed_slots(conn, cur["appointment_id"])
            first = conn.execute("SELECT dock_id, MIN(slot_start_ts) AS a FROM appointment_slots WHERE slot_id = ANY(%s)"
                                 " GROUP BY dock_id", (slots,)).fetchone()
            job.promised_dock, job.promised_start = first["dock_id"], mins(parse_ts(first["a"]))
            job.promised_appt, job.promise_status = cur["appointment_id"], cur["appointment_status"]
            # frozen horizon: a confirmed slot starting within the next hour that the truck can still make stays put
            if (cur["appointment_status"] == "CONFIRMED" and now_m <= job.promised_start < now_m + FROZEN_HORIZON_MIN
                    and job.release <= job.promised_start and job.promised_dock in compat
                    and not any(fa < job.promised_start + job.dur and job.promised_start < fb
                                for fa, fb, _ in fixed[job.promised_dock])):
                job.frozen = True
        jobs.append(job)
    return Problem(facility_id=facility_id, now=now_m, docks=docks, slot_starts=slot_starts, fixed=fixed, jobs=jobs)


def candidate_starts(p: Problem, job: Job, dock_id: str) -> list[int]:
    """Slot starts on this dock the truck could physically use (ignoring other trucks)."""
    if job.frozen:
        return [job.promised_start] if dock_id == job.promised_dock else []
    out = []
    for s in p.slot_starts.get(dock_id, []):
        if s < max(job.release, p.now) or s + job.dur > p.day_end:
            continue
        if any(a < s + job.dur and s < b for a, b, _ in p.fixed[dock_id]):
            continue
        out.append(s)
    return out


@dataclass
class Assignment:
    shipment_id: str
    dock_id: str | None
    start: int | None

    @property
    def scheduled(self) -> bool:
        return self.dock_id is not None


def now_plus(now: datetime, minutes: int) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=minutes)
