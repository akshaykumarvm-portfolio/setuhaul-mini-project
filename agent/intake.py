"""Intake workflow (fixed steps, runs before the agent loop) - Decisions 2 and 9.

1. Idempotency: Redis SET NX on the channel message id AND on driver + text + 2-minute bucket
   (THR009 repeats THR001's text under a different message id). A repeat returns the first reply.
2. Rate limit: 20 messages per driver per minute.
3. Identity: driver_id comes from the verified session (the caller), never from the text.
4. Store the driver message in the transcript (booking service; unique index on message id).
5. Extraction: the model fills DriverMessage; code turns phrases into an ETA window.
6. Run the agent with the case state; store and return its reply.
"""
from __future__ import annotations

import hashlib
import logging
import time
import uuid

import redis
from langchain_core.messages import AIMessage, HumanMessage
from langsmith import trace as ls_trace

from agent.booking_client import BookingClient
from agent.config import get_agent_settings
from agent.extraction import extract, planned_eta_of, resolve_eta
from agent.timeutil import parse_ts
from agent.tools import AgentContext

log = logging.getLogger(__name__)
ETA_FIELDS = ("arrival_time", "arrival_window_start", "arrival_window_end", "delay_minutes", "hedged",
              "is_repair_estimate", "cause", "cause_fixed", "latest_finish")
FIXED_HANDOFF = ("I've passed this to the warehouse coordinator, who will contact you. "
                 "Keep your current slot until you hear from us.")


def _text(m) -> str:
    c = m.content
    return " ".join(p.get("text", "") for p in c if isinstance(p, dict)) if isinstance(c, list) else (c or "")


class Intake:
    def __init__(self, agent, *, redis_client: redis.Redis | None = None, extraction_model=None,
                 client_factory=BookingClient):
        self.agent = agent
        self.r = redis_client
        self.extraction_model = extraction_model
        self.client_factory = client_factory
        self.s = get_agent_settings()

    # ---- Redis helpers (best effort: Redis down only loses speed, never correctness) ----
    def _nx(self, key: str) -> bool | None:
        if self.r is None:
            return None
        try:
            return bool(self.r.set(key, "pending", nx=True, ex=86400))
        except redis.RedisError as e:
            log.warning("redis: %s", e)
            return None

    def _get(self, key: str) -> str | None:
        try:
            return self.r.get(key) if self.r else None
        except redis.RedisError:
            return None

    def _set(self, key: str, val: str, ex: int = 86400) -> None:
        try:
            if self.r:
                self.r.set(key, val, ex=ex)
        except redis.RedisError:
            pass

    def _rate_limited(self, driver_id: str) -> bool:
        try:
            if not self.r:
                return False
            k = f"rl:{driver_id}"
            n = self.r.incr(k)
            if n == 1:
                self.r.expire(k, 60)
            return n > self.s.rate_limit_per_min
        except redis.RedisError:
            return False

    @staticmethod
    def text_key(driver_id: str, text: str) -> str:
        bucket = int(time.time() // 120)
        norm = " ".join(text.lower().split())
        return "idem:txt:" + hashlib.sha256(f"{driver_id}|{norm}|{bucket}".encode()).hexdigest()[:32]

    # ---- the workflow ----
    def handle(self, driver_id: str, text: str, *, thread_id: str | None = None, message_id: str | None = None,
               scenario: str | None = None) -> dict:
        """One driver message, traced as a single LangSmith run (extraction + agent loop nest under it)."""
        thread_id = thread_id or f"CHAT-{driver_id}-{uuid.uuid4().hex[:8]}"
        md = {"thread_id": thread_id, "driver_id": driver_id, "scenario_id": scenario, "message_id": message_id}
        tags = ["setuhaul", "driver-message"] + ([f"scenario:{scenario}"] if scenario else [])
        with ls_trace("driver_message", run_type="chain", inputs={"text": text, "driver_id": driver_id},
                      metadata=md, tags=tags) as run:
            out = self._handle(driver_id, text, thread_id, message_id, scenario)
            run.metadata["shipment_id"] = (out.get("state") or {}).get("shipment_id")
            run.end(outputs={"reply": out["reply"], "duplicate": out.get("duplicate"), "state": out.get("state")})
        return out

    def _handle(self, driver_id: str, text: str, thread_id: str, message_id: str | None, scenario: str | None) -> dict:
        keys = [f"idem:msg:{message_id}"] if message_id else []
        keys.append(self.text_key(driver_id, text))
        for k in keys:
            if self._nx(k) is False:
                prior = self._get(f"{k}:reply")
                return {"thread_id": thread_id, "duplicate": True,
                        "reply": prior or "Got it, I'm already working on that message."}
        if self._rate_limited(driver_id):
            return {"thread_id": thread_id, "duplicate": False, "reply": "You're sending messages very fast. "
                    "Give me a moment to catch up."}

        client = self.client_factory(driver_id)
        sim_now = client.clock().get("sim_now")
        opened = client.open_thread(driver_id, thread_id)
        if not opened.get("ok"):
            return {"thread_id": thread_id, "duplicate": False, "error": opened.get("code"),
                    "reply": "I can't open this conversation for your account. Please contact dispatch."}
        stored = client.add_message(thread_id, "DRIVER", text, external_message_id=message_id, sender_reference=driver_id)
        if stored.get("code") == "DUPLICATE_MESSAGE":
            return {"thread_id": thread_id, "duplicate": True, "reply": "Got it, I already have that message."}

        config = {"configurable": {"thread_id": thread_id}, "run_name": "agent_loop",
                  "tags": ["setuhaul", "driver-chat"] + ([f"scenario:{scenario}"] if scenario else []),
                  "metadata": {"thread_id": thread_id, "driver_id": driver_id, "scenario_id": scenario,
                               "sim_now": sim_now}}
        prior_state = {}
        try:
            prior_state = self.agent.get_state(config).values or {}
        except Exception:  # noqa: BLE001 - checkpoint store unavailable: start fresh
            log.warning("checkpoint read failed", exc_info=True)
        config["metadata"]["shipment_id"] = prior_state.get("shipment_id")

        facts, hint = self._extract(client, driver_id, text, prior_state, sim_now)
        ctx = AgentContext(driver_id=driver_id, thread_id=thread_id, client=client, sim_now=sim_now)
        try:
            out = self.agent.invoke({"messages": [HumanMessage(content=text)], "facts": facts, "eta_hint": hint},
                                    config=config, context=ctx)
            last = out["messages"][-1]
            reply = _text(last) if isinstance(last, AIMessage) else FIXED_HANDOFF
            state = {k: out.get(k) for k in ("shipment_id", "quote_id", "hold_id", "appointment_id", "escalated")}
        except Exception:  # noqa: BLE001 - model/gateway outage: fixed reply + hand-off (Interfaces table)
            log.exception("agent run failed")
            reply = FIXED_HANDOFF
            state = {"shipment_id": prior_state.get("shipment_id")}
            if state["shipment_id"]:
                client.escalate(state["shipment_id"], "OTHER", "Agent unavailable (model or gateway error).", thread_id)
        client.add_message(thread_id, "AGENT", reply, sender_reference="setuhaul-agent")
        for k in keys:
            self._set(f"{k}:reply", reply)
        return {"thread_id": thread_id, "duplicate": False, "reply": reply, "state": state, "facts": facts,
                "eta_hint": hint}

    def _extract(self, client, driver_id, text, prior_state, sim_now):
        if self.extraction_model is None or not sim_now:
            return None, None
        last_agent = next((_text(m) for m in reversed(prior_state.get("messages", [])) if isinstance(m, AIMessage)
                           and not m.tool_calls), None)
        try:
            f = extract(self.extraction_model, text, last_agent)
        except Exception:  # noqa: BLE001 - extraction is a helper; the agent still reads the raw text
            log.warning("extraction failed", exc_info=True)
            return None, None
        ctx = client.driver_context(driver_id)
        ships = [s for s in ctx.get("shipments", []) if s.get("live")]
        ref = (f.shipment_reference or "").strip().upper()
        named = next((s for s in ships if ref and ref in (s["shipment_id"].upper(), s["order_reference"].upper())), None)
        linked = prior_state.get("shipment_id")
        target = (named or next((s for s in ships if s["shipment_id"] == linked), None)
                  or (ships[0] if len(ships) == 1 else None))
        # ETA facts said before the load was known ("late by 45 min", then "the evening one") carry forward
        prev = prior_state.get("facts") or {}
        if not any([f.arrival_time, f.arrival_window_start, f.delay_minutes, f.is_repair_estimate]):
            carried = {k: prev.get(k) for k in ETA_FIELDS if prev.get(k) not in (None, False)}
            if carried:
                f = f.model_copy(update=carried)
        hint = resolve_eta(f, parse_ts(sim_now), planned_eta_of(target))
        facts = f.model_dump() | {"_text": text}
        return facts, hint.model_dump() if hint else None
