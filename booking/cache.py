"""Redis (Upstash over TLS). Never the source of truth for capacity.

If Redis is unreachable the service keeps working: it loses dedupe speed, caches
and live pushes, but the SQLite indexes still prevent double booking.
"""
from __future__ import annotations

import logging
from functools import lru_cache

import redis

from booking.config import get_settings

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_redis() -> redis.Redis | None:
    url = get_settings().redis_url
    if not url:
        return None
    return redis.Redis.from_url(url, decode_responses=True, socket_timeout=2,
                                socket_connect_timeout=2, health_check_interval=30)


def ping() -> bool:
    r = get_redis()
    if r is None:
        return False
    try:
        return bool(r.ping())
    except redis.RedisError as e:  # degrade, never fail a capacity decision
        log.warning("redis unavailable: %s", e)
        return False
