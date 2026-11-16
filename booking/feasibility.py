"""Slot feasibility (Decision 4). Deterministic; the LLM never decides any of this.

A set of slots is feasible for a shipment only if ALL of these hold:
  * the shipment is live and has no contradictory data (A8)
  * slots exist, are on one dock, are back-to-back, and belong to the destination facility
  * every slot is OPEN (A3/A4: an OPEN row alone is not enough, see dock events)
  * dock type matches: reefer -> REEFER dock; > heavy threshold -> HEAVY dock; standard
    loads use STANDARD docks, or the HEAVY dock as a last-ranked option needing approval
  * load weight <= dock max weight (A1/A2)
  * the slots together cover expected_unload_min (A6)
  * start >= ETA window late edge + gate-to-dock minutes, and start >= now
  * no dock event (breakdown, maintenance, capacity reduction, manual block) overlaps
    the slots, and no truck still unloading on that dock past the start (overrun)
  * finish <= the driver's latest finish, if given
  * start after the facility's last-new-start time -> allowed but needs approval (RULE005)
  * no active appointment claim and no unexpired hold (other than the caller's own) on any slot
`docks.dock_status` is never read (A5).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from booking.clock import fmt_ts, parse_ts
from booking.config import get_settings
from booking.eta import EtaWindow, earliest_dock_start, effective_eta

LIVE_STATUSES = ("PLANNED", "ASSIGNED", "IN_TRANSIT", "AT_GATE", "WAITING")
ACTIVE_APPT = ("PENDING_CONFIRMATION", "CONFIRMED", "IN_PROGRESS")
# Which reason to show first when a dock has no window (the root cause, not its symptom).
_DISPLAY_ORDER = {"DRIVER_ON_MULTIPLE_TRUCKS": 0, "SHIPMENT_NOT_ACTIVE": 0, "DOCK_EVENT": 1, "DOCK_BUSY_OVERRUN": 2,
                  "SLOT_TAKEN": 3, "DOCK_OCCUPIED": 3, "SLOT_HELD": 4, "AFTER_DRIVER_LATEST": 5, "SLOT_NOT_OPEN": 6}
DEFAULT_HEAVY_KG = 25000  # RULE004; applied by dock type at every facility (project default)


@dataclass
class Reason:
    code: str
    detail: str

    def as_json(self):
        return {"code": self.code, "detail": self.detail}


@dataclass
class Verdict:
    ok: bool
    reasons: list[Reason] = field(default_factory=list)
    needs_approval: list[Reason] = field(default_factory=list)
    start: datetime | None = None
    end: datetime | None = None          # unload end (start + expected_unload_min)
    dock_id: str | None = None

    def as_json(self):
        return {"ok": self.ok, "reasons": [r.as_json() for r in self.reasons],
                "needs_approval": [r.as_json() for r in self.needs_approval],
                "start": fmt_ts(self.start) if self.start else None,
                "end": fmt_ts(self.end) if self.end else None, "dock_id": self.dock_id}


@dataclass
class FacilityState:
    """Everything feasibility needs for one facility at one instant (read in one go)."""
    now: datetime
    shipment: dict
    eta: EtaWindow
    earliest_start: datetime
    docks: dict[str, dict]
    slots: dict[str, dict]
    slots_by_dock: dict[str, list[dict]]
    events: list[dict]
    busy_until: dict[str, tuple[datetime, str]]     # dock_id -> (free from, shipment unloading)
    claimed: dict[str, str]                          # slot_id -> appointment_id
    occupied: dict[str, list[tuple[datetime, datetime, str]]]  # dock_id -> real unload intervals of bookings
    held: dict[str, tuple[str, str]]                 # slot_id -> (hold_id, shipment_id)
    heavy_kg: int
    last_new_start: time | None
    flags: list[Reason]


def shipment_flags(conn, shipment: dict) -> list[Reason]:
    """Contradictory data that must go to a person before booking (A8), plus warnings (A9)."""
    flags: list[Reason] = []
    others = conn.execute(
        "SELECT shipment_id, vehicle_id, destination_facility_id, latest_eta_ts, current_status FROM shipments"
        " WHERE driver_id = %s AND shipment_id <> %s AND current_status IN ('IN_TRANSIT','AT_GATE','WAITING','IN_DOCK')",
        (shipment["driver_id"], shipment["shipment_id"])).fetchall()
    if shipment["current_status"] in ("IN_TRANSIT", "AT_GATE", "WAITING"):
        for o in others:
            if o["vehicle_id"] != shipment["vehicle_id"]:
                flags.append(Reason("DRIVER_ON_MULTIPLE_TRUCKS",
                                    f"Driver {shipment['driver_id']} is also on {o['shipment_id']} with "
                                    f"{o['vehicle_id']} ({o['current_status']}); needs human review"))
    return flags


def _rule(conn, facility_id: str, rule_type: str) -> str | None:
    r = conn.execute("SELECT rule_value FROM facility_rules WHERE facility_id = %s AND rule_type = %s AND active_flag = 1",
                     (facility_id, rule_type)).fetchone()
    return r["rule_value"] if r else None


def load_state(conn, shipment_id: str, now: datetime, *, ignore_hold: str | None = None,
               ignore_appointments: tuple[str, ...] = ()) -> FacilityState | None:
    s = get_settings()
    shipment = conn.execute("SELECT * FROM shipments WHERE shipment_id = %s", (shipment_id,)).fetchone()
    if shipment is None:
        return None
    fac = shipment["destination_facility_id"]
    eta = effective_eta(conn, shipment_id, now)
    docks = {d["dock_id"]: d for d in conn.execute("SELECT * FROM docks WHERE facility_id = %s", (fac,))}
    slots: dict[str, dict] = {}
    by_dock: dict[str, list[dict]] = {}
    for r in conn.execute("SELECT * FROM appointment_slots WHERE facility_id = %s ORDER BY dock_id, slot_start_ts", (fac,)):
        r = dict(r) | {"start": parse_ts(r["slot_start_ts"]), "end": parse_ts(r["slot_end_ts"])}
        slots[r["slot_id"]] = r
        by_dock.setdefault(r["dock_id"], []).append(r)
    events = [dict(e) | {"start": parse_ts(e["event_start_ts"]),
                         "end": parse_ts(e["event_end_ts"]) if e["event_end_ts"] else None}
              for e in conn.execute(
                  "SELECT e.* FROM dock_status_events e JOIN docks d USING (dock_id)"
                  " WHERE d.facility_id = %s AND e.event_type <> 'REOPENED'", (fac,))]
    busy: dict[str, tuple[datetime, str]] = {}
    for r in conn.execute(
            "SELECT c.actual_dock_id, c.unload_start_ts, c.dock_in_ts, s.expected_unload_min, s.shipment_id"
            " FROM facility_checkins c JOIN shipments s USING (shipment_id)"
            " WHERE c.facility_id = %s AND c.queue_state = 'IN_DOCK' AND c.unload_end_ts IS NULL", (fac,)):
        began = parse_ts(r["unload_start_ts"] or r["dock_in_ts"])
        # Overrun: still unloading past the expected end means the dock is busy at least until now.
        busy[r["actual_dock_id"]] = (max(began + timedelta(minutes=r["expected_unload_min"]), now), r["shipment_id"])
    claimed = {}
    for r in conn.execute(
            "SELECT c.slot_id, a.appointment_id FROM appointment_slot_claims c JOIN appointments a USING (appointment_id)"
            " WHERE c.active = 1 AND a.appointment_status = ANY(%s) AND a.appointment_id <> ALL(%s)"
            " UNION SELECT a.slot_id, a.appointment_id FROM appointments a"
            " WHERE a.appointment_status = ANY(%s) AND a.appointment_id <> ALL(%s)",
            (list(ACTIVE_APPT), list(ignore_appointments), list(ACTIVE_APPT), list(ignore_appointments))):
        claimed[r["slot_id"]] = r["appointment_id"]
    # A booking occupies its dock for its real unload time, which can run past its slot (A6).
    occupied: dict[str, list[tuple[datetime, datetime, str]]] = {}
    for r in conn.execute(
            "SELECT a.appointment_id, sh.expected_unload_min, MIN(sl.slot_start_ts) AS a_start,"
            " MAX(sl.slot_end_ts) AS a_end, MIN(sl.dock_id) AS dock_id"
            " FROM appointments a JOIN shipments sh USING (shipment_id)"
            " JOIN appointment_slot_claims c ON c.appointment_id = a.appointment_id AND c.active = 1"
            " JOIN appointment_slots sl ON sl.slot_id = c.slot_id"
            " WHERE a.appointment_status = ANY(%s) AND a.appointment_id <> ALL(%s) AND sl.facility_id = %s"
            " GROUP BY a.appointment_id, sh.expected_unload_min",
            (list(ACTIVE_APPT), list(ignore_appointments), fac)):
        a_start = parse_ts(r["a_start"])
        a_end = max(parse_ts(r["a_end"]), a_start + timedelta(minutes=r["expected_unload_min"]))
        occupied.setdefault(r["dock_id"], []).append((a_start, a_end, r["appointment_id"]))
    held = {}
    for r in conn.execute(
            "SELECT c.slot_id, h.hold_id, h.shipment_id FROM slot_hold_claims c JOIN slot_holds h USING (hold_id)"
            " WHERE c.active = 1 AND h.state = 'ACTIVE' AND h.expires_at > %s AND h.hold_id IS DISTINCT FROM %s",
            (fmt_ts(now), ignore_hold)):
        held[r["slot_id"]] = (r["hold_id"], r["shipment_id"])
    heavy = _rule(conn, fac, "HEAVY_DOCK_REQUIRED_KG")
    last = _rule(conn, fac, "LAST_NEW_START_TIME")
    return FacilityState(
        now=now, shipment=dict(shipment), eta=eta,
        earliest_start=earliest_dock_start(eta, now, s.gate_to_dock_min),
        docks=docks, slots=slots, slots_by_dock=by_dock, events=events, busy_until=busy,
        claimed=claimed, occupied=occupied, held=held, heavy_kg=int(heavy) if heavy else DEFAULT_HEAVY_KG,
        last_new_start=time.fromisoformat(last) if last else None, flags=shipment_flags(conn, shipment))


def dock_compatibility(st: FacilityState, dock: dict) -> tuple[bool, Reason | None, Reason | None]:
    """-> (allowed, rejection reason, approval needed)."""
    sh = st.shipment
    needs_reefer = sh["temperature_control_required"] == 1 or sh["required_dock_type"] == "REEFER"
    needs_heavy = sh["load_weight_kg"] > st.heavy_kg or sh["required_dock_type"] == "HEAVY"
    code = dock["dock_code"]
    if needs_reefer:
        if dock["dock_type"] != "REEFER" or dock["supports_refrigerated"] != 1:
            return False, Reason("DOCK_TYPE", f"{code}: temperature-controlled load needs the reefer dock"), None
    elif needs_heavy:
        if dock["dock_type"] != "HEAVY":
            return False, Reason("DOCK_TYPE", f"{code}: load of {sh['load_weight_kg']:,} kg needs the heavy dock "
                                              f"(> {st.heavy_kg:,} kg)"), None
    elif dock["dock_type"] == "REEFER":
        return False, Reason("DOCK_TYPE", f"{code}: reefer dock is reserved for temperature-controlled loads"), None
    if sh["load_weight_kg"] > dock["max_vehicle_weight_kg"]:
        return False, Reason("OVERWEIGHT", f"{code}: max {dock['max_vehicle_weight_kg']:,} kg < load "
                                           f"{sh['load_weight_kg']:,} kg"), None
    if not needs_reefer and not needs_heavy and dock["dock_type"] == "HEAVY":
        return True, None, Reason("STANDARD_ON_HEAVY_DOCK", f"{code}: standard load on the heavy dock needs "
                                                            "coordinator approval")
    return True, None, None


def evaluate(st: FacilityState, slot_ids: list[str], latest_finish: datetime | None = None) -> Verdict:
    sh = st.shipment
    v = Verdict(ok=False)
    if sh["current_status"] not in LIVE_STATUSES:
        v.reasons.append(Reason("SHIPMENT_NOT_ACTIVE", f"{sh['shipment_id']} is {sh['current_status']}"))
        return v
    v.reasons.extend(st.flags)
    if not slot_ids:
        v.reasons.append(Reason("NO_SLOTS", "no slots given"))
        return v
    missing = [x for x in slot_ids if x not in st.slots]
    if missing:
        v.reasons.append(Reason("WRONG_FACILITY", f"slots {missing} are not at {sh['destination_facility_id']}"))
        return v
    run = sorted((st.slots[x] for x in slot_ids), key=lambda r: r["start"])
    dock_id = run[0]["dock_id"]
    v.dock_id = dock_id
    dock = st.docks[dock_id]
    if any(r["dock_id"] != dock_id for r in run):
        v.reasons.append(Reason("NOT_ONE_DOCK", "slots span more than one dock"))
        return v
    if any(a["end"] != b["start"] for a, b in zip(run, run[1:])):
        v.reasons.append(Reason("NOT_CONSECUTIVE", "slots are not back-to-back"))
    start, span_end = run[0]["start"], run[-1]["end"]
    v.start = start
    v.end = start + timedelta(minutes=sh["expected_unload_min"])

    for r in run:
        if r["slot_status"] != "OPEN":
            v.reasons.append(Reason("SLOT_NOT_OPEN", f"{r['slot_id']} is {r['slot_status']}"
                                    + (f" ({r['block_reason']})" if r["block_reason"] else "")))
    allowed, why, approval = dock_compatibility(st, dock)
    if not allowed:
        v.reasons.append(why)
    if approval:
        v.needs_approval.append(approval)
    if v.end > span_end:
        v.reasons.append(Reason("SLOT_TOO_SHORT", f"{sh['expected_unload_min']} min unload ends "
                                f"{v.end:%H:%M}, after the slots end at {span_end:%H:%M}"))
    if start < st.now:
        v.reasons.append(Reason("SLOT_IN_PAST", f"starts {start:%H:%M}, before now ({st.now:%H:%M})"))
    elif start < st.earliest_start:
        v.reasons.append(Reason("BEFORE_ARRIVAL", f"starts {start:%H:%M}; earliest dock start is "
                                f"{st.earliest_start:%H:%M} (ETA {st.eta.eta_hi:%H:%M} + gate-to-dock)"))
    for e in st.events:
        if e["dock_id"] == dock_id and e["start"] < span_end and (e["end"] is None or e["end"] > start):
            until = f"{e['end']:%H:%M}" if e["end"] else "further notice"
            v.reasons.append(Reason("DOCK_EVENT", f"{dock['dock_code']}: {e['event_type'].lower().replace('_', ' ')} "
                                    f"{e['start']:%H:%M}-{until} ({e['dock_event_id']}: {e['reason']})"))
    if dock_id in st.busy_until and st.busy_until[dock_id][0] > start:
        free, who = st.busy_until[dock_id]
        v.reasons.append(Reason("DOCK_BUSY_OVERRUN", f"{dock['dock_code']}: {who} still unloading, free about {free:%H:%M}"))
    if latest_finish and v.end > latest_finish:
        v.reasons.append(Reason("AFTER_DRIVER_LATEST", f"unload ends {v.end:%H:%M}, after the driver's latest "
                                f"{latest_finish:%H:%M}"))
    if st.last_new_start and start.timetz().replace(tzinfo=None) > st.last_new_start:
        v.needs_approval.append(Reason("AFTER_LAST_START", f"starts {start:%H:%M}, after the {st.last_new_start:%H:%M} "
                                       "last new start (RULE005)"))
    taken_ids = {st.claimed[r["slot_id"]] for r in run if r["slot_id"] in st.claimed}
    for a_start, a_end, appt_id in st.occupied.get(dock_id, []):
        if appt_id not in taken_ids and a_start < v.end and a_end > start:
            v.reasons.append(Reason("DOCK_OCCUPIED", f"{dock['dock_code']}: booking {appt_id} occupies the dock "
                                    f"{a_start:%H:%M}-{a_end:%H:%M}, overlapping this unload"))
    for r in run:
        if r["slot_id"] in st.claimed:
            v.reasons.append(Reason("SLOT_TAKEN", f"{r['slot_id']} is booked ({st.claimed[r['slot_id']]})"))
        elif r["slot_id"] in st.held and st.held[r["slot_id"]][1] != sh["shipment_id"]:
            v.reasons.append(Reason("SLOT_HELD", f"{r['slot_id']} is held by another truck"))
    v.ok = not v.reasons
    return v


def check_feasibility(conn, shipment_id: str, slot_ids: list[str], now: datetime, *,
                      latest_finish: datetime | None = None, ignore_hold: str | None = None,
                      ignore_appointments: tuple[str, ...] = ()) -> Verdict:
    st = load_state(conn, shipment_id, now, ignore_hold=ignore_hold, ignore_appointments=ignore_appointments)
    if st is None:
        return Verdict(False, [Reason("UNKNOWN_SHIPMENT", shipment_id)])
    return evaluate(st, slot_ids, latest_finish)


def candidate_runs(st: FacilityState, dock_id: str) -> list[list[str]]:
    """Every minimal back-to-back run of slots on one dock that covers the unload time."""
    need = timedelta(minutes=st.shipment["expected_unload_min"])
    seq = st.slots_by_dock.get(dock_id, [])
    runs = []
    for i, first in enumerate(seq):
        run = [first]
        j = i
        while run[-1]["end"] - first["start"] < need and j + 1 < len(seq) and seq[j + 1]["start"] == run[-1]["end"]:
            j += 1
            run.append(seq[j])
        if run[-1]["end"] - first["start"] >= need:
            runs.append([r["slot_id"] for r in run])
    return runs


def find_options(st: FacilityState, *, latest_finish: datetime | None = None, after: datetime | None = None,
                 limit: int = 3) -> tuple[list[dict], list[dict]]:
    """-> (ranked feasible options, per-dock exclusion reasons). Read-only: reserves nothing."""
    options, excluded = [], []
    for dock_id, dock in sorted(st.docks.items(), key=lambda kv: kv[1]["dock_code"]):
        allowed, why, approval = dock_compatibility(st, dock)
        if not allowed:
            excluded.append({"dock_code": dock["dock_code"], **why.as_json()})
            continue
        found, first_block = 0, None
        for run in candidate_runs(st, dock_id):
            v = evaluate(st, run, latest_finish)
            if after and v.start < after:
                continue
            if v.ok:
                found += 1
                options.append({"dock_id": dock_id, "dock_code": dock["dock_code"], "slot_ids": run,
                                "start": v.start, "end": v.end,
                                "needs_approval": [r.as_json() for r in v.needs_approval],
                                "spare_kg": dock["max_vehicle_weight_kg"] - st.shipment["load_weight_kg"]})
            elif first_block is None and v.start >= st.earliest_start:
                first_block = min(v.reasons, key=lambda r: _DISPLAY_ORDER.get(r.code, 50))
        if not found:
            r = first_block or Reason("NO_WINDOW", f"{dock['dock_code']}: no slot after {st.earliest_start:%H:%M}")
            excluded.append({"dock_code": dock["dock_code"], **r.as_json()})
    # Approval-free first, then earliest start, then tightest weight fit (keep big docks free), then dock code.
    options.sort(key=lambda o: (bool(o["needs_approval"]), o["start"], o["spare_kg"], o["dock_code"]))
    picked, seen = [], set()
    for o in options:
        key = (o["dock_id"], o["start"])
        if key not in seen:
            seen.add(key)
            picked.append(o)
        if len(picked) == limit:
            break
    for o in picked:
        o["start"], o["end"] = fmt_ts(o["start"]), fmt_ts(o["end"])
        o.pop("spare_kg")
    return picked, excluded
