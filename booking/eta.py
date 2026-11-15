"""Effective ETA as a window + confidence (Decision 3, audit A12).

Precedence: gate-in (fact) > operations override > warehouse estimate >
latest driver update > original plan. `v_latest_eta` ranks by recency only and
ignores confidence; this module does not use it for decisions.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from booking.clock import fmt_ts, parse_ts

_TIME = re.compile(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b")
_PRECEDENCE = ["OPERATIONS_OVERRIDE", "WAREHOUSE_ESTIMATE", "DRIVER_DECLARED"]


@dataclass
class EtaWindow:
    eta_lo: datetime
    eta_hi: datetime
    confidence: str          # LOW | MEDIUM | HIGH
    source: str              # GATE_IN | OPERATIONS_OVERRIDE | WAREHOUSE_ESTIMATE | DRIVER_DECLARED | ORIGINAL_PLAN
    eta_update_id: str | None = None
    note: str | None = None
    at_facility: bool = False

    def as_json(self) -> dict:
        d = asdict(self)
        d["eta_lo"], d["eta_hi"] = fmt_ts(self.eta_lo), fmt_ts(self.eta_hi)
        return d


def _range_from_note(note: str | None, anchor: datetime) -> tuple[datetime, datetime] | None:
    """'between 12:30 and 13:00' -> (12:30, 13:00) on the anchor's date."""
    if not note:
        return None
    times = _TIME.findall(note)
    if len(times) < 2:
        return None
    (h1, m1), (h2, m2) = times[0], times[1]
    lo = anchor.replace(hour=int(h1), minute=int(m1), second=0, microsecond=0)
    hi = anchor.replace(hour=int(h2), minute=int(m2), second=0, microsecond=0)
    return (lo, hi) if hi >= lo else None


def effective_eta(conn, shipment_id: str, now: datetime | None = None) -> EtaWindow:
    """Records stamped after `now` have not happened yet on the simulated clock (A15) and are ignored."""
    if now is None:
        from booking.clock import now as sim_now
        now = sim_now(conn)
    now_ts = fmt_ts(now)
    gate = conn.execute("SELECT gate_in_ts FROM facility_checkins WHERE shipment_id = %s AND gate_in_ts IS NOT NULL"
                        " AND gate_in_ts <= %s", (shipment_id, now_ts)).fetchone()
    if gate:
        t = parse_ts(gate["gate_in_ts"])
        return EtaWindow(t, t, "HIGH", "GATE_IN", at_facility=True)

    rows = conn.execute(
        "SELECT e.eta_update_id, e.source_type, e.declared_eta_ts, e.confidence_code, e.note, e.created_at,"
        "       w.eta_lo, w.eta_hi, w.confidence AS w_conf"
        " FROM eta_updates e LEFT JOIN eta_windows w USING (eta_update_id)"
        " WHERE e.shipment_id = %s AND e.source_type <> 'ORIGINAL_PLAN' AND e.created_at <= %s"
        # newest first; on a tie, an update recorded through this system beats a seeded one
        " ORDER BY e.created_at DESC, (w.eta_update_id IS NOT NULL) DESC, e.eta_update_id DESC",
        (shipment_id, now_ts)).fetchall()
    for source in _PRECEDENCE:
        for r in rows:
            if r["source_type"] != source:
                continue
            declared = parse_ts(r["declared_eta_ts"])
            if r["eta_lo"]:
                return EtaWindow(parse_ts(r["eta_lo"]), parse_ts(r["eta_hi"]), r["w_conf"], source,
                                 r["eta_update_id"], r["note"])
            rng = _range_from_note(r["note"], declared)
            lo, hi = rng if rng else (declared, declared)
            # A stated range is bounded: plan on its late edge at MEDIUM (LOW is for unbounded phrases).
            conf = "MEDIUM" if rng else r["confidence_code"]
            return EtaWindow(lo, hi, conf, source, r["eta_update_id"], r["note"])

    s = conn.execute("SELECT original_eta_ts, latest_eta_ts FROM shipments WHERE shipment_id = %s",
                     (shipment_id,)).fetchone()
    if s["latest_eta_ts"] and s["latest_eta_ts"] != s["original_eta_ts"]:
        # A revised ETA on the shipment with no update row behind it: use it, at MEDIUM confidence.
        t = parse_ts(s["latest_eta_ts"])
        return EtaWindow(t, t, "MEDIUM", "SHIPMENT_RECORD")
    t = parse_ts(s["original_eta_ts"])
    return EtaWindow(t, t, "MEDIUM", "ORIGINAL_PLAN")


def earliest_dock_start(eta: EtaWindow, now: datetime, gate_to_dock_min: int) -> datetime:
    """Plan on the window's late edge plus gate-to-dock time, never before now."""
    return max(eta.eta_hi + timedelta(minutes=gate_to_dock_min), now)
