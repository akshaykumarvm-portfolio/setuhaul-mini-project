"""Phase 1: the working copy, migrations and simulated clock behave as the spec says."""
import sqlite3

import pytest

from booking import clock
from booking.db import begin_immediate, connect
from booking.db.migrate import migrate
from tests.conftest import sha256

SUPPLIED_TABLES = 18


def test_source_snapshot_never_modified(tmp_path, source_db):
    before = sha256(source_db)
    migrate(tmp_path / "a.db")
    migrate(tmp_path / "a.db")  # idempotent second run
    assert sha256(source_db) == before


def test_supplied_columns_unchanged(conn, source_db):
    src = sqlite3.connect(f"file:{source_db.as_posix()}?mode=ro", uri=True)
    tables = [r[0] for r in src.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    assert len(tables) == SUPPLIED_TABLES
    for t in tables:
        original = src.execute(f"PRAGMA table_info({t})").fetchall()
        migrated = [tuple(r) for r in conn.execute(f"PRAGMA table_info({t})")]
        assert migrated[: len(original)] == original, t  # existing columns identical
        extra = [r[1] for r in migrated[len(original):]]
        assert extra == (["version"] if t == "appointment_slots" else []), t
    src.close()


def test_schema_additions_present(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    for n in ["slot_holds", "appointment_slot_claims", "slot_quotes", "handoffs", "idempotency_keys",
              "audit_log", "sim_clock", "ux_active_hold_per_slot", "ux_active_hold_per_shipment",
              "ux_active_claim_per_slot", "ux_exception_dedupe", "trg_no_booking_on_closed_slot",
              "ux_active_appointment_per_slot", "ux_current_active_appointment_per_shipment"]:
        assert n in names, n
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def _appt(conn, appt_id, shipment, slot, status="PENDING_CONFIRMATION", current=0):
    conn.execute(
        "INSERT INTO appointments (appointment_id, shipment_id, slot_id, appointment_status, booking_source,"
        " is_current, booked_at, updated_at) VALUES (?,?,?,?,'DRIVER_CHAT',?,'2026-08-04T09:30:00+05:30',"
        "'2026-08-04T09:30:00+05:30')", (appt_id, shipment, slot, status, current))


def test_trigger_blocks_booking_on_non_open_slot(conn):
    slot = conn.execute("SELECT slot_id FROM appointment_slots WHERE slot_status <> 'OPEN' LIMIT 1").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="SLOT_NOT_OPEN"):
        _appt(conn, "T-1", "SHP1012", slot)


def test_unique_index_blocks_second_active_claim(conn):
    slot = conn.execute(
        "SELECT slot_id FROM v_slot_availability WHERE availability_status='AVAILABLE' LIMIT 1").fetchone()[0]
    _appt(conn, "T-1", "SHP1012", slot)
    with pytest.raises(sqlite3.IntegrityError):
        _appt(conn, "T-2", "SHP1018", slot)


def test_duplicate_exception_index(conn):
    # EXC001 (live) and EXC009 (DUPLICATE) share a key: allowed. A second live one is not.
    row = conn.execute("SELECT * FROM driver_exceptions WHERE exception_id='EXC001'").fetchone()
    cols = row.keys()
    vals = dict(row) | {"exception_id": "EXC-NEW", "exception_status": "OPEN"}
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"INSERT INTO driver_exceptions ({','.join(cols)}) VALUES ({','.join('?'*len(cols))})",
                     [vals[c] for c in cols])


def test_simulated_clock(conn):
    t0 = clock.now(conn)
    assert t0.date().isoformat() == "2026-08-04" and t0.utcoffset().total_seconds() == 5.5 * 3600
    with begin_immediate(conn):
        clock.set_clock(conn, "2026-08-04T11:00:00+05:30", rate=0)  # frozen
    assert clock.fmt_ts(clock.now(conn)) == "2026-08-04T11:00:00+05:30"
    with begin_immediate(conn):
        clock.advance(conn, 7)
    assert clock.fmt_ts(clock.now(conn)) == "2026-08-04T11:07:00+05:30"


def test_clock_shared_across_connections(db_path):
    a, b = connect(db_path), connect(db_path)
    with begin_immediate(a):
        clock.set_clock(a, "2026-08-04T12:00:00+05:30", rate=0)
    assert clock.fmt_ts(clock.now(b)) == "2026-08-04T12:00:00+05:30"
    a.close(); b.close()


def test_health_endpoint(db_path, monkeypatch):
    monkeypatch.setenv("SETUHAUL_DB_PATH", str(db_path))
    from fastapi.testclient import TestClient
    from booking.api import app
    with TestClient(app) as client:
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["migrations"] == ["001_schema_additions.sql", "002_sim_clock.sql"]
        r = client.post("/admin/clock", json={"sim_ts": "2026-08-04T10:00:00+05:30", "rate": 0}).json()
        assert r["sim_now"] == "2026-08-04T10:00:00+05:30"
