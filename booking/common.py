"""Shared helpers for write paths: ids, results, audit, idempotency, slot versions, events."""
from __future__ import annotations

import json
import logging
import secrets
import time
from datetime import datetime

from booking import cache
from booking.clock import fmt_ts

log = logging.getLogger(__name__)


_last_ms = [0]


def new_id(prefix: str) -> str:
    """Time-sortable id: 11 hex digits of wall-clock milliseconds + 4 random hex digits."""
    ms = max(int(time.time() * 1000), _last_ms[0] + 1)   # strictly increasing within this process
    _last_ms[0] = ms
    return f"{prefix}-{ms:011X}{secrets.token_hex(2).upper()}"


def ok(**data) -> dict:
    return {"ok": True, **data}


def err(code: str, message: str = "", **data) -> dict:
    return {"ok": False, "code": code, "message": message, **data}


def _default(o):
    if isinstance(o, datetime):
        return fmt_ts(o)
    raise TypeError(type(o))


def dumps(o) -> str:
    return json.dumps(o, default=_default, sort_keys=True)


def audit(conn, now: datetime, actor: str, action: str, entity_id: str, before=None, after=None) -> None:
    conn.execute("INSERT INTO audit_log (ts, actor, action, entity_id, before_json, after_json) VALUES (%s,%s,%s,%s,%s,%s)",
                 (fmt_ts(now), actor, action, entity_id,
                  dumps(before) if before is not None else None, dumps(after) if after is not None else None))


def idem_begin(conn, idem_key: str | None) -> dict | None:
    """Serialise retries of one key (advisory xact lock) and return the earlier result, if any."""
    if not idem_key:
        return None
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (idem_key,))
    row = conn.execute("SELECT result_json FROM idempotency_keys WHERE idem_key = %s", (idem_key,)).fetchone()
    return json.loads(row["result_json"]) | {"idempotent_replay": True} if row else None


def idem_save(conn, idem_key: str | None, result: dict, now: datetime) -> None:
    if idem_key:
        conn.execute("INSERT INTO idempotency_keys (idem_key, result_json, created_at) VALUES (%s,%s,%s)",
                     (idem_key, dumps(result), fmt_ts(now)))


def lock_slots(conn, slot_ids: list[str]) -> None:
    """Row-lock slots in a fixed order so concurrent multi-slot writers never deadlock."""
    conn.execute("SELECT slot_id FROM appointment_slots WHERE slot_id = ANY(%s) ORDER BY slot_id FOR UPDATE",
                 (sorted(slot_ids),))


def bump_versions(conn, slot_ids: list[str]) -> None:
    conn.execute("UPDATE appointment_slots SET version = version + 1 WHERE slot_id = ANY(%s)", (list(slot_ids),))


def slot_versions(conn, slot_ids: list[str]) -> dict[str, int]:
    return {r["slot_id"]: r["version"] for r in
            conn.execute("SELECT slot_id, version FROM appointment_slots WHERE slot_id = ANY(%s)", (list(slot_ids),))}


def publish(facility_id: str, event: dict) -> None:
    """Best effort: Redis down never affects a capacity decision."""
    r = cache.get_redis()
    if r is None:
        return
    try:
        r.publish(f"facility:{facility_id}", dumps(event))
        r.delete(f"cache:avail:{facility_id}")
    except Exception as e:  # noqa: BLE001
        log.warning("publish failed: %s", e)
