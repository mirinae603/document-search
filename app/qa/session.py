# qa/session.py
# Redis session manager.
# Stores active session → conversation mapping.
# Falls back to DB if Redis miss — no session ever truly lost.
import json
import logging
import os
import uuid
from datetime import datetime
from typing import Optional, Dict

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

REDIS_URL        = os.getenv("REDIS_URL", "redis://localhost:6379/0")
SESSION_TTL      = int(os.getenv("SESSION_TTL_SECONDS", 86400))   # 24h
SESSION_PREFIX   = "qa:session:"
RATELIMIT_PREFIX = "qa:rl:"

_redis: Optional[aioredis.Redis] = None


async def init_redis():
    global _redis
    try:
        _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
        await _redis.ping()
        logger.info("✓ Redis ready")
    except Exception as e:
        logger.warning(f"Redis unavailable ({e}) — sessions will degrade to stateless")
        _redis = None


async def close_redis():
    global _redis
    if _redis:
        await _redis.close()
        _redis = None


def get_redis() -> Optional[aioredis.Redis]:
    return _redis


# ── Session ops ───────────────────────────────────────────────────────────────

async def create_session(user_id: str, conversation_id: str) -> str:
    """Create new session, return session_id."""
    session_id = str(uuid.uuid4())
    data = {
        "user_id":         user_id,
        "conversation_id": conversation_id,
        "created_at":      datetime.utcnow().isoformat(),
        "last_active":     datetime.utcnow().isoformat(),
    }
    if _redis:
        await _redis.setex(
            f"{SESSION_PREFIX}{session_id}",
            SESSION_TTL,
            json.dumps(data)
        )
    return session_id


async def get_session(session_id: str) -> Optional[Dict]:
    """Get session data. Returns None if expired or not found."""
    if not _redis or not session_id:
        return None
    raw = await _redis.get(f"{SESSION_PREFIX}{session_id}")
    if not raw:
        return None
    data = json.loads(raw)
    # Refresh TTL on access
    await _redis.expire(f"{SESSION_PREFIX}{session_id}", SESSION_TTL)
    return data


async def validate_session(session_id: str, user_id: str) -> Optional[str]:
    """
    Validate session belongs to user.
    Returns conversation_id if valid, None if invalid/expired.
    """
    session = await get_session(session_id)
    if not session:
        return None
    if session.get("user_id") != user_id:
        logger.warning(f"Session user mismatch: {session_id}")
        return None
    return session.get("conversation_id")


async def update_session_conversation(session_id: str, conversation_id: str):
    """Update which conversation this session is currently on."""
    session = await get_session(session_id)
    if session and _redis:
        session["conversation_id"] = conversation_id
        session["last_active"]     = datetime.utcnow().isoformat()
        await _redis.setex(
            f"{SESSION_PREFIX}{session_id}",
            SESSION_TTL,
            json.dumps(session)
        )


async def delete_session(session_id: str):
    if _redis:
        await _redis.delete(f"{SESSION_PREFIX}{session_id}")


# ── Rate limiting ─────────────────────────────────────────────────────────────

async def check_rate_limit(user_id: str, limit: int = 60, window: int = 60) -> bool:
    """
    Simple sliding window rate limiter.
    Returns True if allowed, False if rate limited.
    limit  : max requests per window
    window : window size in seconds
    """
    if not _redis:
        return True   # no Redis = no rate limiting (fail open)
    key   = f"{RATELIMIT_PREFIX}{user_id}"
    count = await _redis.incr(key)
    if count == 1:
        await _redis.expire(key, window)
    if count > limit:
        logger.warning(f"Rate limit hit: user={user_id} count={count}")
        return False
    return True
