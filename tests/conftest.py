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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from booking.db import connect  # noqa: E402
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
