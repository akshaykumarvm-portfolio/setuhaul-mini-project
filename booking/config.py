"""Settings read from the environment (.env in local dev, task env vars on ECS)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env")


def _default_db_path() -> Path:
    # Keep the working copy out of OneDrive: sync clients lock files and break WAL.
    return Path.home() / ".setuhaul" / "setuhaul.db"


@dataclass(frozen=True)
class Settings:
    source_db: Path
    db_path: Path
    redis_url: str | None
    sim_start: str
    sim_rate: float
    busy_timeout_ms: int = 5000
    hold_minutes: int = 5
    gate_to_dock_min: int = 15
    pending_expiry_min: int = 15
    pending_cutoff_before_start_min: int = 30
    sweeper_interval_s: int = 30


def get_settings() -> Settings:
    src = os.getenv("SETUHAUL_SOURCE_DB") or "data/setuhaul_freight_operations.db"
    src_path = Path(src) if Path(src).is_absolute() else REPO_ROOT / src
    db = os.getenv("SETUHAUL_DB_PATH")
    return Settings(
        source_db=src_path,
        db_path=Path(db) if db else _default_db_path(),
        redis_url=os.getenv("REDIS_URL") or None,
        sim_start=os.getenv("SIM_START") or "2026-08-04T09:30:00+05:30",
        sim_rate=float(os.getenv("SIM_RATE") or 1.0),
    )
