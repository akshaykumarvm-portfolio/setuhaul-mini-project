"""Agent tools (Handoff 3 table). Each one is a thin call to the booking service.

Identity (driver_id, thread_id) comes from the runtime context set by intake from the
verified session; the model cannot pass someone else's id. Write tools get an idempotency
key derived from the thread and tool-call id, so a retried call never double-books.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, NotRequired

from langchain.agents import AgentState
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from agent.booking_client import BookingClient
from agent.extraction import has_explicit_time
from agent.timeutil import parse_ts, to_iso


@dataclass
class AgentContext:
    driver_id: str
    thread_id: str
    client: BookingClient = field(repr=False, default=None)  # type: ignore[assignment]
    sim_now: str | None = None


class CaseState(AgentState):
    shipment_id: NotRequired[str | None]
    quote_id: NotRequired[str | None]
    hold_id: NotRequired[str | None]
    appointment_id: NotRequired[str | None]
    eta_confidence: NotRequired[str | None]
    facts: NotRequired[dict | None]
    eta_hint: NotRequired[dict | None]
    clarify_count: NotRequired[int]
    escalated: NotRequired[bool]
    turn_sig: NotRequired[list | None]
    options: NotRequired[list | None]


def _ref(rt: ToolRuntime) -> Any:
    now = rt.context.sim_now
    return parse_ts(now) if now else None


def _out(rt: ToolRuntime, result: dict, **update) -> Command:
    trimmed = {k: v for k, v in result.items() if k not in ("record",)}
    msg = ToolMessage(content=json.dumps(trimmed, default=str), tool_call_id=rt.tool_call_id)
    return Command(update={"messages": [msg], **update})


def _idem(rt: ToolRuntime, name: str) -> str:
    return f"{rt.context.thread_id}:{name}:{rt.tool_call_id}"


@tool
def get_driver_context(runtime: ToolRuntime[AgentContext, CaseState]) -> Command:
    """Look up the driver's loads today: live shipments, current appointments, ETA on record, check-in, flags."""
    ctx = runtime.context
    r = ctx.client.driver_context(ctx.driver_id)
    if r.get("ok"):
        r["shipments"] = [{k: s[k] for k in ("shipment_id", "order_reference", "facility_id", "status", "live",
                                             "priority", "dock_type", "load_weight_kg", "current_appointment", "eta",
                                             "checkin", "flags")} for s in r["shipments"]]
    return _out(runtime, r)


@tool
def link_shipment(shipment_ref: str, runtime: ToolRuntime[AgentContext, CaseState]) -> Command:
    """Link this conversation to one of the driver's loads (shipment id like SHP1004 or the order reference).
    Only call once you are sure which load the driver means."""
    ctx = runtime.context
    r = ctx.client.link(ctx.thread_id, ctx.driver_id, shipment_ref.strip())
    if r.get("ok"):
        return _out(runtime, r, shipment_id=r["shipment_id"], quote_id=None, hold_id=None)
    return _out(runtime, r)


@tool
def record_eta(runtime: ToolRuntime[AgentContext, CaseState], eta_lo: str | None = None, eta_hi: str | None = None,
               confidence: str | None = None, cause: str | None = None, cause_fixed: bool | None = None,
               source_text: str = "") -> Command:
    """Record the driver's revised arrival for the linked load. Times as HH:MM. If omitted, the window that code
    resolved from the driver's latest message is used. Returns whether the current appointment still fits."""
    st, ctx = runtime.state, runtime.context
    if not st.get("shipment_id"):
        return _out(runtime, {"ok": False, "code": "LINK_REQUIRED", "message": "Link the load first."})
    hint = st.get("eta_hint") or {}
    ref = _ref(runtime)
    lo = to_iso(eta_lo, ref) if eta_lo else hint.get("eta_lo")
    hi = to_iso(eta_hi, ref) if eta_hi else hint.get("eta_hi") or lo
    if not lo:
        return _out(runtime, {"ok": False, "code": "NO_ETA", "message": "Ask the driver what time they expect to reach the gate."})
    order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    conf = (confidence or hint.get("confidence") or "MEDIUM").upper()
    # The model may not claim more certainty than code resolved. With no code-resolved window (extraction
    # unavailable, e.g. a provider 429), fail safe: above LOW only if the driver's latest message states a clock time.
    latest = next((m.content for m in reversed(st["messages"]) if getattr(m, "type", "") == "human"), "")
    ceiling = hint.get("confidence") or ("MEDIUM" if has_explicit_time(str(latest)) else "LOW")
    if order.get(conf, 1) > order[ceiling]:
        conf = ceiling
    r = ctx.client.record_eta(shipment_id=st["shipment_id"], eta_lo=lo, eta_hi=hi, confidence=conf, cause=cause,
                              cause_fixed=cause_fixed, source_text=source_text or (st.get("facts") or {}).get("_text", ""),
                              thread_id=ctx.thread_id)
    update = {"eta_confidence": conf if r.get("ok") else st.get("eta_confidence")}
    cur = r.get("current_appointment") if r.get("ok") else None
    if r.get("ok") and conf != "LOW" and (cur is None or not cur.get("still_fits")):
        # the slot no longer works and the ETA is usable: show options now, don't spend a turn asking
        q = ctx.client.quote(st["shipment_id"], ctx.thread_id, None, None)
        if q.get("ok"):
            opts = [{k: o[k] for k in ("option_no", "dock_code", "start", "end", "needs_approval")} for o in q["options"]]
            r["new_options"] = {"quote_id": q["quote_id"], "options": opts, "reserved": False,
                                "note": "Show these to the driver now (not reserved)."}
            update |= {"quote_id": q["quote_id"], "options": opts}
    return _out(runtime, r, **update)


@tool
def find_slot_options(runtime: ToolRuntime[AgentContext, CaseState], latest_finish: str | None = None,
                      after: str | None = None) -> Command:
    """Get up to 3 feasible dock options for the linked load (read-only, nothing is reserved).
    latest_finish / after are optional HH:MM limits from the driver."""
    st, ctx = runtime.state, runtime.context
    if not st.get("shipment_id"):
        return _out(runtime, {"ok": False, "code": "LINK_REQUIRED", "message": "Link the load first."})
    ref = _ref(runtime)
    r = ctx.client.quote(st["shipment_id"], ctx.thread_id, to_iso(latest_finish, ref), to_iso(after, ref))
    if r.get("ok"):
        r["options"] = [{k: o[k] for k in ("option_no", "dock_code", "start", "end", "needs_approval")}
                        for o in r["options"]]
        update = {"quote_id": r["quote_id"], "eta_confidence": r["eta"]["confidence"], "options": r["options"]}
        if not r["options"] and not st.get("escalated"):
            # Decision 10: no feasible slot -> CODE creates the hand-off; the model only explains it
            h = ctx.client.escalate(st["shipment_id"], "NO_FEASIBLE_SLOT",
                                    "No feasible slot after the declared ETA (raised automatically).", ctx.thread_id)
            if h.get("ok"):
                r["handoff"] = {"handoff_id": h["handoff_id"],
                                "routed_to": (h.get("routed_to") or {}).get("contact_role"),
                                "note": "Already handed to a coordinator. Tell the driver what was checked and that "
                                        "a coordinator will contact them."}
                update["escalated"] = True
        return _out(runtime, r, **update)
    return _out(runtime, r)


@tool
def hold_option(option_no: int, runtime: ToolRuntime[AgentContext, CaseState]) -> Command:
    """Hold the option the driver picked (from the last options shown) for 5 minutes. Not a booking."""
    st, ctx = runtime.state, runtime.context
    if not st.get("quote_id"):
        return _out(runtime, {"ok": False, "code": "NO_OPTIONS", "message": "Show options first."})
    r = ctx.client.hold(st["quote_id"], option_no, _idem(runtime, "hold"))
    if r.get("ok"):
        return _out(runtime, r, hold_id=r["hold_id"])
    if r.get("fresh", {}).get("quote_id"):
        f = r["fresh"]
        f["options"] = [{k: o[k] for k in ("option_no", "dock_code", "start", "end", "needs_approval")} for o in f["options"]]
        r["fresh"] = {k: f[k] for k in ("quote_id", "options", "excluded_docks") if k in f}
        return _out(runtime, r, quote_id=f["quote_id"], options=f["options"])
    return _out(runtime, r)


@tool
def request_booking(runtime: ToolRuntime[AgentContext, CaseState]) -> Command:
    """Turn the active hold into a booking request for the warehouse to confirm. Only after the driver says yes."""
    st, ctx = runtime.state, runtime.context
    if not st.get("hold_id"):
        return _out(runtime, {"ok": False, "code": "NO_HOLD", "message": "Hold an option first."})
    r = ctx.client.request(st["hold_id"], _idem(runtime, "request"))
    if r.get("ok"):
        return _out(runtime, r, appointment_id=r["appointment_id"], hold_id=None)
    return _out(runtime, r)


@tool
def get_booking_status(runtime: ToolRuntime[AgentContext, CaseState]) -> Command:
    """Where the linked load stands: NONE, HELD, PENDING or CONFIRMED, with current and pending appointments."""
    st, ctx = runtime.state, runtime.context
    if not st.get("shipment_id"):
        return _out(runtime, {"ok": False, "code": "LINK_REQUIRED", "message": "Link the load first."})
    return _out(runtime, ctx.client.status(st["shipment_id"]))


@tool
def release_hold(runtime: ToolRuntime[AgentContext, CaseState]) -> Command:
    """Release the active hold (the driver changed their mind)."""
    st, ctx = runtime.state, runtime.context
    if not st.get("hold_id"):
        return _out(runtime, {"ok": True, "status": "NO_ACTIVE_HOLD"})
    return _out(runtime, ctx.client.release(st["hold_id"]), hold_id=None)


@tool
def escalate(reason_code: str, summary: str, runtime: ToolRuntime[AgentContext, CaseState]) -> Command:
    """Hand the case to a human coordinator with a full record. reason_code one of: NO_FEASIBLE_SLOT,
    CONTRADICTORY_DATA, SAFETY, CAUSE_NOT_FIXED, PENALTY_OR_CUSTOMER, DISPUTE, CLARIFICATION_FAILED,
    APPROVAL_NEEDED, DRIVER_REQUEST, OTHER."""
    st, ctx = runtime.state, runtime.context
    if not st.get("shipment_id"):
        return _out(runtime, {"ok": False, "code": "LINK_REQUIRED",
                              "message": "Link the load first, or tell the driver to call dispatch."})
    latest = (st.get("facts") or {}).get("latest_finish")
    r = ctx.client.escalate(st["shipment_id"], reason_code, summary, ctx.thread_id,
                            to_iso(latest, _ref(runtime)) if latest else None)
    if r.get("ok"):
        rec = r.get("record") or {}
        r = {"ok": True, "handoff_id": r["handoff_id"], "routed_to": (r.get("routed_to") or {}).get("contact_role"),
             "channel_status": r.get("channel_status"), "docks_checked": rec.get("docks_checked"),
             "nearest_out_of_policy": rec.get("nearest_out_of_policy")}
        return _out(runtime, r, escalated=True)
    return _out(runtime, r)


@tool
def propose_schedule(runtime: ToolRuntime[AgentContext, CaseState]) -> Command:
    """Where the facility-wide scheduler (CP-SAT vs dispatch rule) would place the linked load, considering every
    truck. Read-only and NOT reserved; use only to explain what is likely, then book via find_slot_options."""
    st, ctx = runtime.state, runtime.context
    if not st.get("shipment_id"):
        return _out(runtime, {"ok": False, "code": "LINK_REQUIRED", "message": "Link the load first."})
    return _out(runtime, ctx.client.proposal(st["shipment_id"]))


TOOLS = [get_driver_context, link_shipment, record_eta, find_slot_options, hold_option, request_booking,
         get_booking_status, release_hold, escalate, propose_schedule]
WRITE_TOOLS = {"link_shipment", "record_eta", "hold_option", "request_booking", "release_hold", "escalate"}
NEEDS_LINK = {"record_eta", "find_slot_options", "hold_option", "request_booking", "get_booking_status", "release_hold",
              "escalate", "propose_schedule"}
