"""Phase 1: snapshot load, migrations and the simulated clock behave as the spec says."""
import sqlite3

import psycopg
import pytest

from booking import clock
from booking.config import REPO_ROOT
from booking.db import connect, write_tx
from booking.db.snapshot import plan, read_snapshot, split_statements

SNAPSHOT_DB = REPO_ROOT / "data" / "setuhaul_freight_operations.db"
SNAPSHOT_SQL = REPO_ROOT / "data" / "setuhaul_schema_and_seed.sql"
VIEWS = [("v_latest_eta", "shipment_id"), ("v_slot_availability", "slot_id"),
         ("v_inbound_operational_state", "shipment_id"), ("v_current_facility_queue", "shipment_id")]


def _sqlite():
    return sqlite3.connect(f"file:{SNAPSHOT_DB.as_posix()}?mode=ro", uri=True)


def sqlite_counts():
    src = _sqlite()
    tables = [r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    out = {t: src.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    src.close()
    return out


def test_snapshot_rows_match_supplied_db(template_url, conn):
    expected = sqlite_counts()
    assert len(expected) == 18
    _, out = template_url
    assert out["loaded_rows"] == expected
    for t, n in expected.items():
        assert conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"] == n, t


def test_supplied_columns_unchanged(conn):
    src = _sqlite()
    for t in sqlite_counts():
        original = [r[1] for r in src.execute(f'PRAGMA table_info("{t}")')]
        cols = conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = %s ORDER BY ordinal_position", (t,)).fetchall()
        migrated = [c["column_name"] for c in cols]
        assert migrated[: len(original)] == original, t
        assert migrated[len(original):] == (["version"] if t == "appointment_slots" else []), t
        ts_types = {c["data_type"] for c in cols if c["column_name"].endswith(("_ts", "_at"))}
        assert ts_types <= {"text"}, (t, ts_types)  # timestamps stay TEXT
    src.close()


def test_views_match_sqlite(conn):
    src = _sqlite()
    for view, key in VIEWS:
        cols = [r[1] for r in src.execute(f"PRAGMA table_info({view})")]
        q = f"SELECT {','.join(cols)} FROM {view} ORDER BY {key}, {cols[1]}"
        lite = [tuple(r) for r in src.execute(q)]
        pg = [tuple(r.values()) for r in conn.execute(q)]
        assert pg == lite, view
    src.close()


def test_schema_additions_present(conn):
    names = {r["n"] for r in conn.execute(
        "SELECT tablename AS n FROM pg_tables WHERE schemaname='public' UNION "
        "SELECT indexname FROM pg_indexes WHERE schemaname='public' UNION SELECT tgname FROM pg_trigger")}
    for n in ["slot_holds", "slot_hold_claims", "appointment_slot_claims", "slot_quotes", "handoffs",
              "idempotency_keys", "audit_log", "sim_clock", "ux_active_hold_per_slot", "ux_active_hold_per_shipment",
              "ux_active_hold_claim_per_slot", "ux_active_claim_per_slot", "ux_exception_dedupe",
              "trg_no_booking_on_closed_slot", "ux_active_appointment_per_slot",
              "ux_current_active_appointment_per_shipment"]:
        assert n in names, n
    row = conn.execute("SELECT datcollate AS c FROM pg_database WHERE datname = current_database()").fetchone()
    assert row["c"] == "C"


def _appt(conn, appt_id, shipment, slot):
    conn.execute(
        "INSERT INTO appointments (appointment_id, shipment_id, slot_id, appointment_status, booking_source,"
        " is_current, booked_at, updated_at) VALUES (%s,%s,%s,'PENDING_CONFIRMATION','DRIVER_CHAT',0,"
        " '2026-08-04T09:30:00+05:30','2026-08-04T09:30:00+05:30')", (appt_id, shipment, slot))


def test_trigger_blocks_booking_on_non_open_slot(conn):
    slot = conn.execute("SELECT slot_id FROM appointment_slots WHERE slot_status <> 'OPEN' LIMIT 1").fetchone()["slot_id"]
    with pytest.raises(psycopg.errors.CheckViolation, match="SLOT_NOT_OPEN"):
        _appt(conn, "T-1", "SHP1012", slot)


def test_unique_index_blocks_second_active_claim(conn):
    slot = conn.execute(
        "SELECT slot_id FROM v_slot_availability WHERE availability_status='AVAILABLE' LIMIT 1").fetchone()["slot_id"]
    _appt(conn, "T-1", "SHP1012", slot)
    with pytest.raises(psycopg.errors.UniqueViolation):
        _appt(conn, "T-2", "SHP1018", slot)


def test_duplicate_exception_index(conn):
    # EXC001 (live) and EXC009 (DUPLICATE) share a key: allowed. A second live one is not.
    row = dict(conn.execute("SELECT * FROM driver_exceptions WHERE exception_id='EXC001'").fetchone())
    row |= {"exception_id": "EXC-NEW", "exception_status": "OPEN"}
    placeholders = ",".join(["%s"] * len(row))
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(f"INSERT INTO driver_exceptions ({','.join(row)}) VALUES ({placeholders})", list(row.values()))


def test_text_timestamps_order_like_time(conn):
    # C collation: ISO-8601 text with one fixed offset sorts exactly like time.
    got = [r["slot_start_ts"] for r in conn.execute(
        "SELECT slot_start_ts FROM appointment_slots WHERE dock_id='DOCK-JAI-D1' ORDER BY slot_start_ts")]
    assert got == sorted(got, key=clock.parse_ts)


def test_simulated_clock(conn):
    t0 = clock.now(conn)
    assert t0.date().isoformat() == "2026-08-04" and t0.utcoffset().total_seconds() == 5.5 * 3600
    with write_tx(conn):
        clock.set_clock(conn, "2026-08-04T11:00:00+05:30", rate=0)  # frozen
    assert clock.fmt_ts(clock.now(conn)) == "2026-08-04T11:00:00+05:30"
    with write_tx(conn):
        clock.advance(conn, 7)
    assert clock.fmt_ts(clock.now(conn)) == "2026-08-04T11:07:00+05:30"


def test_clock_shared_across_connections(db_url):
    a, b = connect(db_url), connect(db_url)
    with write_tx(a):
        clock.set_clock(a, "2026-08-04T12:00:00+05:30", rate=0)
    assert clock.fmt_ts(clock.now(b)) == "2026-08-04T12:00:00+05:30"
    a.close()
    b.close()


def test_splitter_keeps_semicolons_inside_strings():
    creates, inserts, other = plan(split_statements(read_snapshot(str(SNAPSHOT_SQL))))
    assert len(creates) == 18
    assert sum(map(len, inserts.values())) == 275
    assert len(other) == 11  # 7 indexes + 4 views


def test_health_endpoint(db_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_url)
    from fastapi.testclient import TestClient

    from booking.api import app
    with TestClient(app) as client:
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["migrations"] == ["000_snapshot", "001_schema_additions.sql", "002_sim_clock.sql"]
        r = client.post("/admin/clock", json={"sim_ts": "2026-08-04T10:00:00+05:30", "rate": 0}).json()
        assert r["sim_now"] == "2026-08-04T10:00:00+05:30"
