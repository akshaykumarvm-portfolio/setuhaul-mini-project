"""HTTP client for the booking service. The only way the agent touches capacity.

Every call carries X-Driver-Id from the verified session (never from the model).
Network failure -> {"ok": false, "code": "UNAVAILABLE"} so the agent promises nothing.
Writes retry once with the same Idempotency-Key; BUSY retries once.
"""
from __future__ import annotations

import httpx

from agent.config import get_agent_settings


class BookingClient:
    def __init__(self, driver_id: str | None = None, base_url: str | None = None, transport: httpx.BaseTransport | None = None):
        s = get_agent_settings()
        self.s = s
        headers = {"X-Driver-Id": driver_id} if driver_id else {}
        if s.agent_api_key:   # service identity for the booking API when it runs with AUTH_MODE=cognito
            headers["X-Agent-Key"] = s.agent_api_key
        self.http = httpx.Client(base_url=base_url or s.booking_api_url, headers=headers, transport=transport,
                                 timeout=httpx.Timeout(s.read_timeout_s))

    def _call(self, method: str, path: str, *, json=None, params=None, idem: str | None = None, write: bool = False) -> dict:
        headers = {"Idempotency-Key": idem} if idem else {}
        timeout = self.s.write_timeout_s if write else self.s.read_timeout_s
        attempts = 2 if (write and idem) or not write else 1
        last = None
        for _ in range(attempts):
            try:
                r = self.http.request(method, path, json=json, params=params, headers=headers, timeout=timeout)
            except httpx.HTTPError as e:
                last = {"ok": False, "code": "UNAVAILABLE", "message": f"booking service unreachable: {type(e).__name__}"}
                continue
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code == 503 and body.get("code") == "BUSY":
                last = body
                continue
            if isinstance(body, dict) and "ok" not in body:
                body = {"ok": r.status_code < 400, **body}
            return body
        return last or {"ok": False, "code": "UNAVAILABLE"}

    # reads
    def clock(self) -> dict: return self._call("GET", "/admin/clock")
    def driver_context(self, driver_id: str) -> dict: return self._call("GET", f"/drivers/{driver_id}/context")
    def status(self, shipment_id: str) -> dict: return self._call("GET", f"/shipments/{shipment_id}/status")
    def messages(self, thread_id: str) -> dict: return self._call("GET", f"/threads/{thread_id}/messages")

    # writes
    def open_thread(self, driver_id: str, thread_id: str) -> dict:
        return self._call("POST", "/threads", json={"driver_id": driver_id, "thread_id": thread_id}, write=True)

    def add_message(self, thread_id: str, sender_type: str, text: str, **kw) -> dict:
        return self._call("POST", f"/threads/{thread_id}/messages", json={"sender_type": sender_type, "text": text, **kw},
                          write=True)

    def link(self, thread_id: str, driver_id: str, shipment_id: str) -> dict:
        return self._call("POST", f"/threads/{thread_id}/link", json={"driver_id": driver_id, "shipment_id": shipment_id},
                          write=True)

    def record_eta(self, **body) -> dict: return self._call("POST", "/eta", json=body, write=True)

    def quote(self, shipment_id: str, thread_id: str, latest_finish: str | None, after: str | None) -> dict:
        return self._call("POST", "/quotes", json={"shipment_id": shipment_id, "thread_id": thread_id,
                                                   "latest_finish": latest_finish, "after": after}, write=True)

    def hold(self, quote_id: str, option_no: int, idem: str) -> dict:
        return self._call("POST", "/holds", json={"quote_id": quote_id, "option_no": option_no}, idem=idem, write=True)

    def release(self, hold_id: str) -> dict: return self._call("DELETE", f"/holds/{hold_id}", write=True)

    def request(self, hold_id: str, idem: str) -> dict:
        return self._call("POST", "/bookings", json={"hold_id": hold_id}, idem=idem, write=True)

    def escalate(self, shipment_id: str, reason_code: str, summary: str, thread_id: str,
                 latest_finish: str | None = None) -> dict:
        return self._call("POST", "/handoffs", json={"shipment_id": shipment_id, "reason_code": reason_code,
                                                     "summary": summary, "thread_id": thread_id,
                                                     "latest_finish": latest_finish}, write=True)
