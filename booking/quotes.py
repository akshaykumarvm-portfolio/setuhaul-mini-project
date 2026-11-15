"""Read-only slot options (Decision 5). Showing an option reserves nothing.

Each quote stores the options and the version of every slot they use, so a later
hold can tell whether anything changed in between (STALE).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from booking.clock import fmt_ts
from booking.common import dumps, err, new_id, ok, slot_versions
from booking.feasibility import find_options, load_state

QUOTE_TTL_MIN = 10


def build_quote(conn, shipment_id: str, now: datetime, *, thread_id: str | None = None,
                latest_finish: datetime | None = None, after: datetime | None = None) -> dict:
    st = load_state(conn, shipment_id, now)
    if st is None:
        return err("UNKNOWN_SHIPMENT", shipment_id)
    options, excluded = find_options(st, latest_finish=latest_finish, after=after)
    for i, o in enumerate(options, 1):
        o["option_no"] = i
    versions = slot_versions(conn, [s for o in options for s in o["slot_ids"]])
    quote_id = new_id("QTE")
    conn.execute("INSERT INTO slot_quotes (quote_id, thread_id, shipment_id, options_json, slot_versions_json,"
                 " created_at, expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                 (quote_id, thread_id, shipment_id, dumps(options), dumps(versions), fmt_ts(now),
                  fmt_ts(now + timedelta(minutes=QUOTE_TTL_MIN))))
    return ok(quote_id=quote_id, shipment_id=shipment_id, reserved=False, options=options, excluded_docks=excluded,
              eta=st.eta.as_json(), earliest_dock_start=fmt_ts(st.earliest_start),
              flags=[f.as_json() for f in st.flags],
              note="Options are not reserved until you pick one and it is held.")


def get_quote(conn, quote_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM slot_quotes WHERE quote_id = %s", (quote_id,)).fetchone()
    if r is None:
        return None
    return dict(r) | {"options": json.loads(r["options_json"]), "versions": json.loads(r["slot_versions_json"])}
