"""Agent settings (env / .env). The agent never sees database credentials."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


@dataclass(frozen=True)
class AgentSettings:
    booking_api_url: str
    openrouter_api_key: str
    model: str
    fallback_model: str | None
    extraction_model: str
    redis_url: str | None
    checkpoint_ttl_s: int = 24 * 3600
    rate_limit_per_min: int = 20
    read_timeout_s: float = 8.0
    write_timeout_s: float = 12.0


def get_agent_settings() -> AgentSettings:
    model = os.getenv("OPEN_ROUTER_MODEL") or "anthropic/claude-sonnet-4.5"
    return AgentSettings(
        booking_api_url=(os.getenv("BOOKING_API_URL") or "http://localhost:8000").rstrip("/"),
        openrouter_api_key=os.getenv("OPEN_ROUTER_API_KEY") or os.getenv("OPENROUTER_API_KEY") or "",
        model=model,
        fallback_model=os.getenv("OPEN_ROUTER_FALLBACK_MODEL") or "openai/gpt-4.1-mini",
        extraction_model=os.getenv("OPEN_ROUTER_EXTRACTION_MODEL") or model,
        redis_url=os.getenv("REDIS_URL") or None,
    )
