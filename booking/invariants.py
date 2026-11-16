"""Invariant queries (Decision 13). Each returns the violating rows; all must be empty.
Used by the integrity tests and by GET /admin/invariants (the scenario run's rubric check)."""

ACTIVE = "('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')"

INVARIANTS = {
    # 1. at most one active booking claim per slot (single- and multi-slot bookings)
    "one_active_claim_per_slot": f"""
        SELECT c.slot_id, COUNT(*) AS n FROM appointment_slot_claims c JOIN appointments a USING (appointment_id)
        WHERE c.active = 1 AND a.appointment_status IN {ACTIVE} GROUP BY c.slot_id HAVING COUNT(*) > 1""",
    "one_active_appointment_per_slot": f"""
        SELECT slot_id, COUNT(*) AS n FROM appointments WHERE appointment_status IN {ACTIVE}
        GROUP BY slot_id HAVING COUNT(*) > 1""",
    # 2. no booking made by this system overlaps any other booking's real unload interval on its dock
    "no_overlapping_intervals_per_dock": f"""
        WITH iv AS (
          SELECT a.appointment_id, MIN(sl.dock_id) AS dock_id,
                 MIN(sl.slot_start_ts)::timestamptz AS s,
                 GREATEST(MAX(sl.slot_end_ts)::timestamptz,
                          MIN(sl.slot_start_ts)::timestamptz + make_interval(mins => sh.expected_unload_min)) AS e
          FROM appointments a JOIN shipments sh USING (shipment_id)
          JOIN appointment_slot_claims c ON c.appointment_id = a.appointment_id AND c.active = 1
          JOIN appointment_slots sl ON sl.slot_id = c.slot_id
          WHERE a.appointment_status IN {ACTIVE}
          GROUP BY a.appointment_id, sh.expected_unload_min)
        SELECT x.appointment_id AS a, y.appointment_id AS b, x.dock_id FROM iv x JOIN iv y
          ON x.dock_id = y.dock_id AND x.appointment_id < y.appointment_id AND x.s < y.e AND y.s < x.e
        WHERE x.appointment_id LIKE 'APT-%%' OR y.appointment_id LIKE 'APT-%%'""",
    # 3. at most one current active appointment per shipment
    "one_current_appointment_per_shipment": f"""
        SELECT shipment_id, COUNT(*) AS n FROM appointments WHERE is_current = 1 AND appointment_status IN {ACTIVE}
        GROUP BY shipment_id HAVING COUNT(*) > 1""",
    # 4. no live hold on a booked slot
    "no_live_hold_on_booked_slot": f"""
        SELECT hc.slot_id, h.hold_id, a.appointment_id FROM slot_hold_claims hc
        JOIN slot_holds h ON h.hold_id = hc.hold_id AND h.state = 'ACTIVE'
        JOIN appointment_slot_claims c ON c.slot_id = hc.slot_id AND c.active = 1
        JOIN appointments a ON a.appointment_id = c.appointment_id AND a.appointment_status IN {ACTIVE}
        WHERE hc.active = 1""",
    # 5. at most one active hold per slot and per shipment
    "one_active_hold_per_slot": """
        SELECT hc.slot_id, COUNT(*) AS n FROM slot_hold_claims hc JOIN slot_holds h USING (hold_id)
        WHERE hc.active = 1 AND h.state = 'ACTIVE' GROUP BY hc.slot_id HAVING COUNT(*) > 1""",
    # 6. claim rows agree with appointment status
    "claims_match_status": f"""
        SELECT a.appointment_id, a.appointment_status FROM appointments a
        WHERE (a.appointment_status IN {ACTIVE}) <>
              EXISTS (SELECT 1 FROM appointment_slot_claims c WHERE c.appointment_id = a.appointment_id AND c.active = 1)""",
}


def check_all(conn) -> dict[str, list]:
    return {name: [dict(r) for r in conn.execute(q).fetchall()] for name, q in INVARIANTS.items()}
