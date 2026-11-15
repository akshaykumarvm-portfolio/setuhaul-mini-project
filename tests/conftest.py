"""Test fixtures: one migrated template DB per session, a fresh clone per test.

Uses TEST_DATABASE_URL (default: local Postgres). Never points at the real DATABASE_URL.
"""
import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

os.environ["SWEEPER_ENABLED"] = "0"  # tests drive the sweeper explicitly
os.environ["LANGSMITH_TRACING"] = "false"   # keep test traffic out of the LangSmith project
os.environ["REDIS_URL"] = ""        # never publish test traffic to the shared Upstash instance
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from booking import clock  # noqa: E402
from booking.db import connect, write_tx  # noqa: E402
from booking.db.migrate import create_database, migrate  # noqa: E402

BASE_URL = os.getenv("TEST_DATABASE_URL") or "postgresql://postgres@localhost:5432/postgres"
TEMPLATE = "setuhaul_test_template"


def url_for(dbname: str) -> str:
    return make_conninfo(BASE_URL, dbname=dbname)


@pytest.fixture(scope="session")
def template_url():
    url = url_for(TEMPLATE)
    out = migrate(url, reset=True)
    yield url, out


@pytest.fixture()
def db_url(template_url):
    name = f"setuhaul_t_{uuid.uuid4().hex[:10]}"
    create_database(url_for(name), template=TEMPLATE)
    yield url_for(name)
    with psycopg.connect(url_for("postgres"), autocommit=True) as admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture()
def conn(db_url):
    c = connect(db_url)
    yield c
    c.close()


NOW = "2026-08-04T10:00:00+05:30"


@pytest.fixture()
def frozen(conn):
    """Freeze the simulated clock at 10:00 so results are deterministic."""
    with write_tx(conn):
        clock.set_clock(conn, NOW, rate=0)
    return conn


def set_now(conn, ts: str):
    with write_tx(conn):
        clock.set_clock(conn, ts, rate=0)


def add_synthetic_shipments(conn, n: int, *, template: str = "SHP1012", prefix: str = "SHP9") -> list[str]:
    """Clone a live standard load n times, each with its own driver and truck (no A8 conflicts)."""
    ids = []
    with write_tx(conn):
        for i in range(1, n + 1):
            sid, drv, veh = f"{prefix}{i:03d}", f"DRV9{i:03d}", f"VEH9{i:03d}"
            conn.execute("INSERT INTO drivers (driver_id, carrier_id, driver_name, phone, licence_number, home_base_city,"
                         " driver_status) SELECT %s, carrier_id, %s, %s, %s, home_base_city, 'ACTIVE' FROM drivers"
                         " WHERE driver_id = 'DRV012'", (drv, f"Test Driver {i}", f"+91-90000{i:05d}", f"TEST-LIC-{i}"))
            conn.execute("INSERT INTO vehicles (vehicle_id, carrier_id, vehicle_type_code, registration_number, capacity_kg,"
                         " refrigeration_capable, active_flag) SELECT %s, carrier_id, vehicle_type_code, %s, capacity_kg,"
                         " refrigeration_capable, 1 FROM vehicles WHERE vehicle_id = 'VEH012'", (veh, f"TEST-REG-{i}"))
            cols = [r["column_name"] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'shipments' ORDER BY ordinal_position")]
            vals = {"shipment_id": sid, "order_reference": f"TEST-ORD-{prefix}{i}", "driver_id": drv, "vehicle_id": veh}
            sel = ", ".join(f"%({c})s" if c in vals else c for c in cols)
            conn.execute(f"INSERT INTO shipments ({', '.join(cols)}) SELECT {sel} FROM shipments WHERE shipment_id = %(t)s",
                         vals | {"t": template})
            ids.append(sid)
    return ids
