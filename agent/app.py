"""Entrypoints: local CLI and Amazon Bedrock AgentCore Runtime.

    python -m agent.app chat --driver DRV006            # interactive chat in the terminal
    python -m agent.app say --driver DRV013 "I am late by one hour"
    python -m agent.app serve                            # AgentCore contract on :8080 (/invocations, /ping)

AgentCore payload: {"driver_id": "...", "text": "...", "thread_id": "...", "message_id": "..."}.
Phase 4 takes driver_id from the validated Cognito token instead of the payload.
"""
from __future__ import annotations

import argparse
import logging
import uuid
from functools import lru_cache

from agent.checkpointer import RedisSaver
from agent.config import get_agent_settings
from agent.graph import build_agent, make_model
from agent.intake import Intake

log = logging.getLogger("setuhaul.agent")


@lru_cache(maxsize=1)
def get_intake() -> Intake:
    s = get_agent_settings()
    saver = None
    redis_client = None
    if s.redis_url:
        import redis
        saver = RedisSaver.from_url(s.redis_url, ttl=s.checkpoint_ttl_s)
        redis_client = redis.Redis.from_url(s.redis_url, decode_responses=True, socket_timeout=3)
    else:
        from langgraph.checkpoint.memory import InMemorySaver
        saver = InMemorySaver()
    agent = build_agent(checkpointer=saver)
    return Intake(agent, redis_client=redis_client, extraction_model=make_model(s.extraction_model),
                  extraction_fallback=make_model(s.fallback_model) if s.fallback_model else None)


# ---------------- AgentCore Runtime ----------------
try:
    from bedrock_agentcore.runtime import BedrockAgentCoreApp

    app = BedrockAgentCoreApp()

    @app.entrypoint
    def invoke(payload: dict, context=None) -> dict:
        driver_id = payload.get("driver_id")
        text = (payload.get("text") or payload.get("prompt") or "").strip()
        if not driver_id or not text:
            return {"error": "driver_id and text are required"}
        return get_intake().handle(driver_id, text, thread_id=payload.get("thread_id"),
                                   message_id=payload.get("message_id"), scenario=payload.get("scenario"))
except ImportError:  # pragma: no cover - local runs without the SDK
    app = None


def _print_turn(res: dict) -> None:
    tag = " (duplicate)" if res.get("duplicate") else ""
    print(f"\nagent{tag}> {res['reply']}")
    st = res.get("state") or {}
    hint = res.get("eta_hint")
    meta = {k: v for k, v in st.items() if v}
    if hint:
        meta["eta"] = f"{hint['eta_lo'][11:16]}-{hint['eta_hi'][11:16]} {hint['confidence']}"
    if meta:
        print(f"   [state] {meta}")


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    p = argparse.ArgumentParser(prog="agent.app")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("chat")
    c.add_argument("--driver", required=True)
    c.add_argument("--thread")
    s = sub.add_parser("say")
    s.add_argument("--driver", required=True)
    s.add_argument("--thread")
    s.add_argument("text")
    sub.add_parser("serve")
    a = p.parse_args()
    if a.cmd == "serve":
        app.run(port=8080)
        return
    thread = a.thread or f"CHAT-{a.driver}-{uuid.uuid4().hex[:8]}"
    intake = get_intake()
    if a.cmd == "say":
        _print_turn(intake.handle(a.driver, a.text, thread_id=thread, message_id=uuid.uuid4().hex))
        return
    print(f"Chatting as {a.driver} on thread {thread}. Ctrl+C to quit.")
    while True:
        try:
            text = input("\ndriver> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text:
            _print_turn(intake.handle(a.driver, text, thread_id=thread, message_id=uuid.uuid4().hex))


if __name__ == "__main__":
    main()
