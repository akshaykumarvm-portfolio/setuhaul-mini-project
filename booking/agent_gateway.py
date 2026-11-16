"""Forward a verified driver's chat message to the conversation agent.

AGENT_INVOKE_MODE=local      POST {AGENT_URL}/invocations (python -m agent.app serve)
AGENT_INVOKE_MODE=agentcore  POST the AgentCore Runtime endpoint with the driver's own Cognito ID token;
                             AgentCore's JWT inbound auth checks it again (defence in depth)
The driver id in the payload comes from this service's token check, never from the browser.
"""
from __future__ import annotations

import os
from urllib.parse import quote

import httpx


def _session_id(driver_id: str, thread_id: str) -> str:
    sid = f"setuhaul-{driver_id}-{thread_id}"
    return sid if len(sid) >= 33 else sid.ljust(33, "0")     # AgentCore needs >= 33 characters


def invoke_agent(driver_id: str, text: str, thread_id: str, message_id: str | None, token: str | None,
                 scenario: str | None = None) -> dict:
    payload = {"driver_id": driver_id, "text": text, "thread_id": thread_id, "message_id": message_id,
               "scenario": scenario}
    mode = os.getenv("AGENT_INVOKE_MODE", "local")
    try:
        if mode == "agentcore":
            region = os.getenv("AWS_REGION", "us-east-1")
            arn = os.environ["AGENT_RUNTIME_ARN"]
            url = (f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{quote(arn, safe='')}"
                   f"/invocations?qualifier=DEFAULT")
            r = httpx.post(url, json=payload, timeout=90, headers={
                "Authorization": f"Bearer {token}",
                "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": _session_id(driver_id, thread_id)})
        else:
            r = httpx.post(f"{os.getenv('AGENT_URL', 'http://localhost:8080').rstrip('/')}/invocations",
                           json=payload, timeout=90)
    except httpx.HTTPError as e:
        return {"ok": False, "code": "AGENT_UNAVAILABLE", "reply": "I can't reach the assistant right now. "
                "Nothing has changed with your slot; please try again in a minute.", "detail": type(e).__name__}
    if r.status_code >= 400:
        return {"ok": False, "code": "AGENT_ERROR", "status": r.status_code, "detail": r.text[:300],
                "reply": "Something went wrong on my side. Nothing has changed with your slot; please try again."}
    body = r.json()
    return {"ok": True, **body} if isinstance(body, dict) else {"ok": True, "reply": str(body)}
