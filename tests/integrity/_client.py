"""HTTP client workers. Kept in their own module so separate processes can import them."""
import random
import time

import httpx


def wait_until(start_at: float, jitter_ms: float) -> None:
    delay = start_at - time.time()
    if delay > 0:
        time.sleep(delay)
    time.sleep(random.uniform(0, jitter_ms) / 1000)


def post_hold(args) -> tuple[int, str, str | None]:
    base, quote_id, option_no, start_at, jitter_ms = args
    wait_until(start_at, jitter_ms)
    r = httpx.post(f"{base}/holds", json={"quote_id": quote_id, "option_no": option_no}, timeout=30)
    body = r.json()
    return r.status_code, body.get("code", "OK") if not body.get("ok") else "OK", body.get("hold_id")


def post_booking(args) -> tuple[int, str, str | None]:
    base, hold_id, idem, start_at, jitter_ms = args
    wait_until(start_at, jitter_ms)
    r = httpx.post(f"{base}/bookings", json={"hold_id": hold_id}, headers={"Idempotency-Key": idem}, timeout=30)
    body = r.json()
    return r.status_code, body.get("code", "OK") if not body.get("ok") else "OK", body.get("appointment_id")
