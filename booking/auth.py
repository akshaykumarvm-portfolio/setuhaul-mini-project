"""Who is calling (Handoff 3, "Identity and security"). Authorisation is code; the LLM never decides access.

AUTH_MODE=off      local dev and tests: trust X-Driver-Id (phase 2/3 behaviour)
AUTH_MODE=cognito  every request needs one of:
  * Authorization: Bearer <Cognito ID token>  -> driver / coordinator / planner (from cognito:groups);
    a driver's id comes ONLY from the token's custom:driver_id, a coordinator's facility from custom:facility_id
  * X-Agent-Key: <secret from SSM>            -> the agent; X-Driver-Id is trusted because the agent got it
    from this service's own verified /chat call
"""
from __future__ import annotations

import hmac
import os
from dataclasses import dataclass, field
from functools import lru_cache

import jwt
from fastapi import HTTPException, Request

STAFF = ("coordinator", "planner")


@dataclass
class Principal:
    kind: str                      # dev | driver | coordinator | planner | agent
    driver_id: str | None = None
    facility_id: str | None = None
    username: str | None = None
    groups: list[str] = field(default_factory=list)
    token: str | None = None       # raw ID token, forwarded to AgentCore

    @property
    def is_staff(self) -> bool:
        return self.kind in STAFF or self.kind == "dev"

    @property
    def actor(self) -> str:
        return f"{self.kind}:{self.username or self.driver_id or 'local'}"


def auth_mode() -> str:
    return os.getenv("AUTH_MODE", "off").lower()


@lru_cache(maxsize=1)
def _cognito():
    region = os.getenv("COGNITO_REGION") or os.getenv("AWS_REGION") or "us-east-1"
    pool = os.environ["COGNITO_USER_POOL_ID"]
    issuer = f"https://cognito-idp.{region}.amazonaws.com/{pool}"
    return issuer, os.environ["COGNITO_CLIENT_ID"], jwt.PyJWKClient(f"{issuer}/.well-known/jwks.json", cache_keys=True)


def verify_id_token(token: str) -> dict:
    issuer, client_id, jwks = _cognito()
    try:
        key = jwks.get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=["RS256"], audience=client_id, issuer=issuer,
                            options={"require": ["exp", "iat", "token_use"]})
    except jwt.PyJWTError as e:
        raise HTTPException(401, f"invalid token: {e}") from e
    if claims.get("token_use") != "id":
        raise HTTPException(401, "an ID token is required")
    return claims


def principal_from_claims(claims: dict, token: str | None = None) -> Principal:
    groups = claims.get("cognito:groups", []) or []
    kind = "planner" if "planner" in groups else "coordinator" if "coordinator" in groups else \
        "driver" if "driver" in groups else None
    if kind is None:
        raise HTTPException(403, "user is not in a SetuHaul group")
    p = Principal(kind=kind, username=claims.get("cognito:username") or claims.get("email"), groups=groups,
                  driver_id=claims.get("custom:driver_id"), facility_id=claims.get("custom:facility_id"), token=token)
    if kind == "driver" and not p.driver_id:
        raise HTTPException(403, "driver account has no driver_id")
    return p


def get_principal(request: Request) -> Principal:
    if auth_mode() != "cognito":
        return Principal(kind="dev", driver_id=request.headers.get("X-Driver-Id"))
    agent_key = request.headers.get("X-Agent-Key")
    if agent_key:
        expected = os.getenv("AGENT_API_KEY", "")
        if not expected or not hmac.compare_digest(agent_key, expected):
            raise HTTPException(401, "bad agent key")
        return Principal(kind="agent", driver_id=request.headers.get("X-Driver-Id"), username="setuhaul-agent")
    authz = request.headers.get("Authorization", "")
    token = authz[7:] if authz.lower().startswith("bearer ") else request.query_params.get("token")  # SSE can't set headers
    if not token:
        raise HTTPException(401, "sign in required")
    return principal_from_claims(verify_id_token(token), token)


def require_staff(p: Principal) -> Principal:
    if not p.is_staff:
        raise HTTPException(403, "coordinator or planner only")
    return p


def require_planner(p: Principal) -> Principal:
    if p.kind not in ("planner", "dev"):
        raise HTTPException(403, "planner only")
    return p


def check_facility(p: Principal, facility_id: str | None) -> None:
    """A coordinator acts only on their own facility; planners and dev see all."""
    if p.kind == "coordinator" and p.facility_id and facility_id and p.facility_id != facility_id:
        raise HTTPException(403, f"you coordinate {p.facility_id}, not {facility_id}")
