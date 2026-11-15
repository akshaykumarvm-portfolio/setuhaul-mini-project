"""Settings read from the environment (.env in local dev, task env vars on ECS)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    database_url: str          # the SetuHaul database (system of record)
    snapshot_uri: str          # s3://bucket/key.sql or a local path to the supplied seed SQL
    redis_url: str | None
    sim_start: str
    sim_rate: float
    aws_region: str
    lock_timeout_ms: int = 5000
    hold_minutes: int = 5
    gate_to_dock_min: int = 15
    pending_expiry_min: int = 15
    pending_cutoff_before_start_min: int = 30
    sweeper_interval_s: int = 30


def get_settings() -> Settings:
    return Settings(
        database_url=os.getenv("DATABASE_URL") or "postgresql://postgres@localhost:5432/setuhaul",
        snapshot_uri=os.getenv("SNAPSHOT_URI") or str(REPO_ROOT / "data" / "setuhaul_schema_and_seed.sql"),
        redis_url=os.getenv("REDIS_URL") or None,
        sim_start=os.getenv("SIM_START") or "2026-08-04T09:30:00+05:30",
        sim_rate=float(os.getenv("SIM_RATE") or 1.0),
        aws_region=os.getenv("AWS_REGION") or "us-east-1",
    )
