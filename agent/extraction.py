"""Structured extraction (Decision 3): the model reads the message; CODE turns phrases into a time window.

Confidence rules (code, not model):
  * explicit arrival time ("reaching around 11:20")          -> HIGH (MEDIUM if hedged)
  * explicit range ("between 12:30 and 13:00")                -> MEDIUM, plan on the late edge
  * relative delay only ("late by one hour")                  -> LOW: ask for the arrival time
  * repair estimate ("tyre fixed in 30 min")                  -> LOW: a repair time is not an arrival
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from agent.timeutil import fmt_ts, parse_ts

Intent = Literal["REPORT_DELAY", "ASK_SLOT_OPTIONS", "CHECK_STATUS", "EARLY_ARRIVAL", "CHOOSE_OPTION", "CONFIRM",
                 "DECLINE", "CANCEL_HOLD", "GENERAL_QUESTION", "UNKNOWN"]


class DriverMessage(BaseModel):
    """Facts stated in the driver's latest message. Leave a field null if the driver did not say it."""
    intent: Intent = Field(description="What the driver wants in this message")
    shipment_reference: str | None = Field(None, description="Load / order / shipment reference if mentioned, e.g. SHP1004 or ORD-...")
    arrival_time: str | None = Field(None, description="Stated arrival time at the gate, 24h HH:MM, e.g. '11:20'")
    arrival_window_start: str | None = Field(None, description="Start of a stated arrival range, HH:MM")
    arrival_window_end: str | None = Field(None, description="End of a stated arrival range, HH:MM")
    delay_minutes: int | None = Field(None, description="Delay stated relative to the plan, in minutes ('late by one hour' = 60)")
    hedged: bool = Field(False, description="Driver sounds unsure (maybe, not sure, depends)")
    is_repair_estimate: bool = Field(False, description="The time given is for a repair/fix, not for arriving")
    cause: Literal["TRAFFIC", "BREAKDOWN", "WEATHER", "LATE_DEPARTURE", "LOADING_DELAY", "ROUTE_ISSUE", "OTHER"] | None = None
    cause_fixed: bool | None = Field(None, description="True if the cause is resolved (e.g. 'tyre repaired, moving again')")
    latest_finish: str | None = Field(None, description="Latest time unloading must finish / driver must leave, HH:MM")
    wants: Literal["see", "book"] | None = Field(None, description="'see' = just looking at options, 'book' = wants a slot")
    chosen_option: int | None = Field(None, description="Option number the driver picked, if any")
    says_yes: bool | None = Field(None, description="True if the driver agrees/confirms, False if they decline")


class EtaResolution(BaseModel):
    eta_lo: str
    eta_hi: str
    confidence: Literal["LOW", "MEDIUM", "HIGH"]
    basis: str
    needs_question: str | None = None


def _at(hhmm: str, ref: datetime) -> datetime | None:
    try:
        h, m = hhmm.strip().replace(".", ":").split(":")[:2]
        t = ref.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
    except (ValueError, AttributeError):
        return None
    return t


def resolve_eta(f: DriverMessage, msg_time: datetime, planned_eta: datetime | None) -> EtaResolution | None:
    """Code, not the model, decides the window and how much to trust it."""
    if f.arrival_window_start and f.arrival_window_end:
        lo, hi = _at(f.arrival_window_start, msg_time), _at(f.arrival_window_end, msg_time)
        if lo and hi and hi >= lo:
            return EtaResolution(eta_lo=fmt_ts(lo), eta_hi=fmt_ts(hi), confidence="MEDIUM",
                                 basis="stated range; planning on the late edge")
    if f.arrival_time and not f.is_repair_estimate:
        t = _at(f.arrival_time, msg_time)
        if t:
            if t < msg_time - timedelta(hours=1):     # "1:30" said in the afternoon means 13:30
                t += timedelta(hours=12)
            return EtaResolution(eta_lo=fmt_ts(t), eta_hi=fmt_ts(t), confidence="MEDIUM" if f.hedged else "HIGH",
                                 basis="stated arrival time")
    if f.is_repair_estimate:
        return EtaResolution(eta_lo=fmt_ts(msg_time), eta_hi=fmt_ts(msg_time), confidence="LOW",
                             basis="repair estimate, not an arrival time",
                             needs_question="Once it's fixed, what time do you expect to reach the gate?")
    if f.delay_minutes and planned_eta:
        t = planned_eta + timedelta(minutes=f.delay_minutes)
        return EtaResolution(eta_lo=fmt_ts(t), eta_hi=fmt_ts(t), confidence="LOW",
                             basis=f"plan {planned_eta:%H:%M} + {f.delay_minutes} min (relative delay only)",
                             needs_question=f"Does that mean you'll reach the gate around {t:%H:%M}, "
                                            "or could the delay keep growing?")
    return None


EXTRACTION_PROMPT = (
    "You extract facts from a truck driver's chat message to a warehouse dock-scheduling assistant. "
    "Use only what the driver actually said in their latest message; use the previous assistant message only to "
    "interpret short answers (e.g. '2' after a list of options, or '11:10' after 'what time will you arrive?'). "
    "Times are local (Asia/Kolkata), return them as 24h HH:MM. Never invent values."
)


def extract(model, text: str, last_agent_message: str | None) -> DriverMessage:
    structured = model.with_structured_output(DriverMessage)
    ctx = f"Previous assistant message: {last_agent_message}\n" if last_agent_message else ""
    return structured.invoke([("system", EXTRACTION_PROMPT), ("human", f"{ctx}Driver's latest message: {text}")])


def planned_eta_of(ctx_shipment: dict | None) -> datetime | None:
    if not ctx_shipment:
        return None
    appt = ctx_shipment.get("current_appointment")
    if appt and appt.get("start"):
        return parse_ts(appt["start"])
    eta = (ctx_shipment.get("eta") or {}).get("eta_hi")
    return parse_ts(eta) if eta else None
