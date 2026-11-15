import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from booking.config import get_settings  # noqa: E402
from booking.db import connect  # noqa: E402
from booking.db.migrate import migrate  # noqa: E402


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture()
def db_path(tmp_path) -> Path:
    """A fresh migrated copy of the snapshot per test."""
    return migrate(tmp_path / "setuhaul.db")


@pytest.fixture()
def conn(db_path):
    c = connect(db_path)
    yield c
    c.close()


@pytest.fixture(scope="session")
def source_db() -> Path:
    return get_settings().source_db
