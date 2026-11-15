"""Deterministic guardrails around the LLM (Handoff 3, "Agent design").

CaseContextPrompt  before every model call: load case state from the booking service into the prompt
WriteGuard         refuse shipment tools until a load is linked; refuse hold/request on a LOW ETA
ReplyPostCheck     block a final reply that claims a booking the booking service hasn't CONFIRMED,
                   or that names a dock no tool returned; send a neutral, true reply instead
TurnOutcome        count failed clarifications; after 2 hand off to a coordinator; if a call limit
                   cut the run short, reply with a fixed hand-off message instead of nothing
"""
from __future__ import annotations

import json
import logging
import re

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from agent.prompts import RULES, case_snapshot
from agent.tools import NEEDS_LINK, AgentContext, CaseState

log = logging.getLogger(__name__)

PROMISE = re.compile(
    r"\b(you(?:'re| are)\s+(?:now\s+)?(?:booked|confirmed|all set)"
    r"|(?:is|are|has been|have been|now)\s+(?:officially\s+)?(?:booked|confirmed)"
    r"|(?:booking|slot|appointment)\s+(?:is\s+)?confirmed"
    r"|confirmed\s*:)", re.I)
NEGATED = re.compile(r"\b(not|isn't|aren't|yet to be|until|once|when|after|waiting)\b[^.]{0,40}$", re.I)
DOCK = re.compile(r"\bD([1-9])\b")
HANDOFF_TALK = re.compile(r"\b(pass|passed|passing|hand|handed|handing|escalat\w*|forward\w*)\b[^.]{0,40}\b(coordinator|warehouse team|supervisor|shift manager)", re.I)
FALLBACK = ("I couldn't finish that safely, so I've passed your case to the warehouse coordinator. "
            "Keep your current slot for now; they will contact you.")


def _text(msg) -> str:
    c = msg.content
    if isinstance(c, list):
        return " ".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c)
    return c or ""


def _stage_reply(status: dict | None) -> str:
    if not status or not status.get("ok"):
        return "I'm checking with the warehouse and will update you shortly. Nothing has changed with your slot yet."
    cur, pend = status.get("current_appointment"), status.get("pending_request")
    if status["stage"] == "PENDING" and pend:
        return (f"Requested {pend['dock_code']} at {pend['start'][11:16]}, waiting for the warehouse to confirm. "
                f"Keep your current slot until I confirm.")
    if status["stage"] == "HELD":
        return "I'm holding a slot for you for a few minutes. It is not booked yet; say yes if you want me to request it."
    if cur:
        return (f"I'm checking with the warehouse. Your appointment on record is {cur['dock_code']} at "
                f"{cur['start'][11:16]} ({cur['status'].lower().replace('_', ' ')}).")
    return "I'm checking with the warehouse. You don't have a confirmed slot yet."


class CaseContextPrompt(AgentMiddleware[CaseState, AgentContext]):
    state_schema = CaseState

    def wrap_model_call(self, request: ModelRequest, handler):
        st, ctx = request.state, request.runtime.context
        status = ctx.client.status(st["shipment_id"]) if st.get("shipment_id") else None
        prompt = RULES + "\n\n" + case_snapshot(st, status, ctx.sim_now, st.get("facts"), st.get("eta_hint"))
        return handler(request.override(system_message=SystemMessage(content=prompt)))


class WriteGuard(AgentMiddleware[CaseState, AgentContext]):
    state_schema = CaseState

    def wrap_tool_call(self, request, handler):
        name = request.tool_call["name"]
        st = request.state
        refuse = None
        if name in NEEDS_LINK and not st.get("shipment_id"):
            refuse = {"ok": False, "code": "LINK_REQUIRED",
                      "message": "No load linked yet: call get_driver_context, confirm which load, then link_shipment."}
        elif name in ("hold_option", "request_booking") and st.get("eta_confidence") == "LOW":
            refuse = {"ok": False, "code": "ETA_UNCONFIRMED",
                      "message": "The ETA is vague. Ask the driver for the time they expect to reach the gate, then record_eta."}
        if refuse:
            return ToolMessage(content=json.dumps(refuse), tool_call_id=request.tool_call["id"], name=name)
        return handler(request)


class ReplyPostCheck(AgentMiddleware[CaseState, AgentContext]):
    state_schema = CaseState

    def after_model(self, state, runtime):
        msgs = state["messages"]
        last = msgs[-1]
        if not isinstance(last, AIMessage) or last.tool_calls:
            return None
        text = _text(last)
        problems = []
        status = None
        promise = [m for m in PROMISE.finditer(text) if not NEGATED.search(text[max(0, m.start() - 60):m.start()])]
        if promise:
            status = runtime.context.client.status(state["shipment_id"]) if state.get("shipment_id") else None
            if not (status and status.get("ok") and status["stage"] == "CONFIRMED"):
                problems.append(f"claims a booking but stage is {status.get('stage') if status else 'unknown'}")
        tool_text = " ".join(_text(m) for m in msgs if isinstance(m, ToolMessage))
        for d in sorted(set(DOCK.findall(text))):
            if f'"D{d}"' not in tool_text and f"D{d}:" not in tool_text and f"D{d} " not in tool_text:
                problems.append(f"names dock D{d} that no tool returned")
        if HANDOFF_TALK.search(text) and not state.get("escalated") and state.get("shipment_id"):
            # the reply promises a hand-off: make it true in code rather than trusting the model to have called it
            ctx = runtime.context
            h = ctx.client.escalate(state["shipment_id"], "OTHER", f"Agent told the driver: {text[:300]}", ctx.thread_id)
            log.warning("post-check created hand-off %s for a promised escalation", h.get("handoff_id"))
            if not problems:
                return {"escalated": True}
        if not problems:
            return None
        log.warning("reply blocked: %s | %r", problems, text[:200])
        if status is None and state.get("shipment_id"):
            status = runtime.context.client.status(state["shipment_id"])
        safe = AIMessage(content=_stage_reply(status), id=last.id,
                         response_metadata={"post_check_blocked": problems, "original": text})
        return {"messages": [safe]}


class TurnOutcome(AgentMiddleware[CaseState, AgentContext]):
    """Runs once at the end of each driver turn."""
    state_schema = CaseState
    MAX_FAILED_CLARIFICATIONS = 2

    def before_agent(self, state, runtime):
        return {"turn_sig": self._progress(state)}   # kept in state: safe across concurrent conversations

    @staticmethod
    def _progress(state) -> list:
        return [state.get("shipment_id"), state.get("eta_confidence"), state.get("quote_id"), state.get("hold_id"),
                state.get("appointment_id"), bool(state.get("escalated"))]

    def after_agent(self, state, runtime):
        msgs = state["messages"]
        last = msgs[-1]
        update: dict = {}
        if not isinstance(last, AIMessage) or last.tool_calls or not _text(last).strip():
            # a call limit ended the run mid-way: never leave the driver without an answer
            self._handoff(state, runtime, "CLARIFICATION_FAILED", "Agent stopped by call limits before finishing.")
            return {"messages": [AIMessage(content=FALLBACK)], "escalated": True}
        moved = self._progress(state) != state.get("turn_sig")
        asked = _text(last).rstrip().endswith("?")
        # consecutive turns that end in a question without moving the case forward; the first ask counts as 1
        count = 0 if not asked else (1 if moved else state.get("clarify_count", 0) + 1)
        update["clarify_count"] = count
        if count > self.MAX_FAILED_CLARIFICATIONS and not state.get("escalated"):
            self._handoff(state, runtime, "CLARIFICATION_FAILED", "Two clarification attempts did not resolve the case.")
            update["messages"] = [AIMessage(content=FALLBACK, id=last.id)]
            update["escalated"] = True
            update["clarify_count"] = 0
        return update

    @staticmethod
    def _handoff(state, runtime, code, summary):
        if state.get("shipment_id") and not state.get("escalated"):
            ctx = runtime.context
            ctx.client.escalate(state["shipment_id"], code, summary, ctx.thread_id)
