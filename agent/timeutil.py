"""Time helpers for the agent (same format as the booking service, no DB dependency)."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def parse_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=IST)


def fmt_ts(dt: datetime) -> str:
    return dt.astimezone(IST).replace(microsecond=0).isoformat()


def to_iso(value: str | None, ref: datetime) -> str | None:
    """'11:20' (on the reference date) or a full ISO timestamp -> ISO with +05:30."""
    if not value:
        return None
    value = value.strip()
    if "T" in value:
        return fmt_ts(parse_ts(value))
    h, m = value.replace(".", ":").split(":")[:2]
    return fmt_ts(ref.astimezone(IST).replace(hour=int(h), minute=int(m), second=0, microsecond=0))
