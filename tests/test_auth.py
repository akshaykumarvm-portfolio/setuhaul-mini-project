"""AUTH_MODE=cognito: role boundaries enforced in code, with real RS256 tokens signed by a test key."""
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

ISS = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TEST"
CLIENT = "test-client"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(groups, *, driver_id=None, facility_id=None, user="u", aud=CLIENT, use="id", exp_in=3600):
    claims = {"iss": ISS, "aud": aud, "token_use": use, "iat": int(time.time()), "exp": int(time.time()) + exp_in,
              "cognito:username": user, "cognito:groups": groups}
    if driver_id:
        claims["custom:driver_id"] = driver_id
    if facility_id:
        claims["custom:facility_id"] = facility_id
    return jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": "test"})


def h(tok):
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture()
def api(db_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("AUTH_MODE", "cognito")
    monkeypatch.setenv("AGENT_API_KEY", "agent-secret")
    import booking.auth as auth
    fake_jwks = SimpleNamespace(get_signing_key_from_jwt=lambda t: SimpleNamespace(key=KEY.public_key()))
    monkeypatch.setattr(auth, "_cognito", lambda: (ISS, CLIENT, fake_jwks))
    from booking.api import app
    with TestClient(app) as c:
        yield c


def test_no_token_is_rejected(api):
    assert api.get("/me").status_code == 401
    assert api.post("/quotes", json={"shipment_id": "SHP1006"}).status_code == 401


def test_bad_tokens_rejected(api):
    assert api.get("/me", headers=h(token(["driver"], driver_id="DRV006", aud="other"))).status_code == 401
    assert api.get("/me", headers=h(token(["driver"], driver_id="DRV006", use="access"))).status_code == 401
    assert api.get("/me", headers=h(token(["driver"], driver_id="DRV006", exp_in=-10))).status_code == 401
    assert api.get("/me", headers={"Authorization": "Bearer not.a.jwt"}).status_code == 401
    assert api.get("/me", headers=h(token([], user="nogroup"))).status_code == 403


def test_driver_identity_comes_from_token_not_header(api):
    t = h(token(["driver"], driver_id="DRV006")) | {"X-Driver-Id": "DRV014"}   # spoof attempt
    me = api.get("/me", headers=t).json()
    assert me["driver_id"] == "DRV006" and {s["shipment_id"] for s in me["shipments"]} == {"SHP1006", "SHP1021"}
    assert api.post("/quotes", json={"shipment_id": "SHP1014"}, headers=t).status_code == 403   # DRV014's load
    assert api.post("/quotes", json={"shipment_id": "SHP1006"}, headers=t).status_code == 200


def test_driver_cannot_do_staff_things(api):
    t = h(token(["driver"], driver_id="DRV006"))
    assert api.get("/bookings", headers=t).status_code == 403
    assert api.get("/handoffs", headers=t).status_code == 403
    assert api.post("/bookings/APT1013A/confirm", headers=t).status_code == 403
    assert api.post("/admin/clock", json={"advance_min": 5}, headers=t).status_code == 403
    assert api.post("/threads/X/messages", json={"sender_type": "AGENT", "text": "x"}, headers=t).status_code == 403


def test_coordinator_limited_to_own_facility(api):
    jai = h(token(["coordinator"], facility_id="FAC-JAI-01", user="neha"))
    ggn = h(token(["coordinator"], facility_id="FAC-GGN-01", user="rohit"))
    assert api.get("/facilities/FAC-JAI-01/timeline", headers=jai).status_code == 200
    assert api.get("/facilities/FAC-JAI-01/timeline", headers=ggn).status_code == 403
    pend = api.get("/bookings", headers=jai).json()["bookings"]
    assert pend and all(b["facility_id"] == "FAC-JAI-01" for b in pend)
    assert api.post("/bookings/APT1013A/reject", json={"reason": "x"}, headers=ggn).status_code == 403
    assert api.post("/admin/clock", json={"advance_min": 1}, headers=jai).status_code == 403   # planner only


def test_coordinator_actions_are_attributed(api):
    jai = h(token(["coordinator"], facility_id="FAC-JAI-01", user="neha"))
    r = api.post("/bookings/APT1013A/reject", json={"reason": "crew short", "actor": "someone-else"}, headers=jai)
    assert r.status_code == 200
    from booking.db import connect
    import os
    c = connect(os.environ["DATABASE_URL"])
    actor = c.execute("SELECT actor FROM audit_log WHERE entity_id = 'APT1013A' ORDER BY seq DESC LIMIT 1").fetchone()
    c.close()
    assert actor["actor"] == "coordinator:neha"


def test_planner_runs_the_clock(api):
    p = h(token(["planner"], user="planner1"))
    assert api.post("/admin/clock", json={"sim_ts": "2026-08-04T10:00:00+05:30", "rate": 0}, headers=p).status_code == 200


def test_agent_key(api):
    good = {"X-Agent-Key": "agent-secret", "X-Driver-Id": "DRV006"}
    assert api.get("/drivers/DRV006/context", headers=good).status_code == 200
    assert api.get("/drivers/DRV014/context", headers=good).status_code == 403
    assert api.get("/drivers/DRV006/context", headers={"X-Agent-Key": "wrong", "X-Driver-Id": "DRV006"}).status_code == 401
    assert api.post("/quotes", json={"shipment_id": "SHP1006"}, headers={"X-Agent-Key": "agent-secret"}).status_code == 401


def test_chat_is_driver_only_and_uses_token_identity(api, monkeypatch):
    seen = {}

    def fake_invoke(driver_id, text, thread_id, message_id, token_, scenario=None):
        seen.update(driver_id=driver_id, token=token_)
        return {"ok": True, "reply": "hi", "thread_id": thread_id}
    monkeypatch.setattr("booking.api.invoke_agent", fake_invoke)
    tok = token(["driver"], driver_id="DRV013")
    r = api.post("/chat", json={"text": "late", "driver_id": "DRV006"}, headers=h(tok))    # body driver_id ignored
    assert r.status_code == 200 and seen["driver_id"] == "DRV013" and seen["token"] == tok
    assert api.post("/chat", json={"text": "x"}, headers=h(token(["coordinator"], facility_id="FAC-JAI-01"))).status_code == 403


def test_api_prefix_for_cloudfront(api):
    assert api.get("/api/health").json()["auth"] == "cognito"


def test_only_planner_can_reset(api):
    assert api.post("/admin/reset", headers=h(token(["coordinator"], facility_id="FAC-JAI-01"))).status_code == 403
    assert api.post("/admin/reset", headers=h(token(["driver"], driver_id="DRV006"))).status_code == 403
