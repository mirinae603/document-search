# connectors/graph_users.py
# Internal Microsoft Graph user-lookup helpers used by intelligence/meeting_prep.
#
# Two read-only directory lookups:
#   resolve_emails_to_uids() — email → Graph user object id (for the structured
#       participant filter, since Teams chunks store Graph UIDs, not emails)
#   get_signed_in_user_email() — the signed-in user's primary address, so the
#       organizer's own email can be stripped before the participant filter is
#       built (otherwise it matches almost every ingested chunk).
#
# These are NOT exposed on the public API. They reuse the exact MSAL token path
# that connectors/calendar.py uses (the shared "outlook" Microsoft Graph token);
# no second token store is introduced. Every Graph error degrades gracefully —
# the helpers log at INFO and return None; they never raise and never retry.
from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional

import httpx

from connectors.calendar import CalendarConnector

logger = logging.getLogger(__name__)

GRAPH        = "https://graph.microsoft.com/v1.0"
_CONCURRENCY = 5    # bounded Graph fan-out
_TIMEOUT     = 30   # seconds

# Lazily-built singleton — construction does no network I/O (mirrors calendar.py),
# but can raise if MSAL config is absent, so all construction is guarded.
_connector: Optional[CalendarConnector] = None

# /me memoization keyed by access token (a new token ⇒ a fresh lookup).
_ME_CACHE: Dict[str, Optional[str]] = {}


def _get_connector() -> CalendarConnector:
    global _connector
    if _connector is None:
        _connector = CalendarConnector()
    return _connector


async def _get_token(user_id: str = "default") -> Optional[str]:
    """Acquire the shared Outlook Graph token via calendar.py's MSAL flow.

    Never raises — a missing/invalid MSAL config or absent token returns None.
    """
    try:
        return await _get_connector().authenticate(user_id)
    except Exception as e:
        logger.info(f"graph_users: token acquisition failed: {e}")
        return None


# ── Public helpers ──────────────────────────────────────────────────────────

async def get_signed_in_user_email(user_id: str = "default") -> Optional[str]:
    """
    Return the signed-in user's primary email (GET /me → mail or
    userPrincipalName), lowercased. Memoized per access token. Covered by the
    existing User.Read scope. Returns None on any failure; never raises.
    """
    token = await _get_token(user_id)
    if not token:
        return None
    if token in _ME_CACHE:
        return _ME_CACHE[token]

    headers = {"Authorization": f"Bearer {token}"}
    email: Optional[str] = None
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{GRAPH}/me?$select=mail,userPrincipalName", headers=headers
            )
        if resp.status_code == 200:
            data  = resp.json()
            email = (data.get("mail") or data.get("userPrincipalName") or "").strip().lower() or None
        else:
            logger.info(f"graph_users: /me returned {resp.status_code}")
    except Exception as e:
        logger.info(f"graph_users: /me failed: {e}")
        return None   # transient — don't poison the per-token cache

    _ME_CACHE[token] = email
    return email


async def resolve_emails_to_uids(
    emails: List[str],
    *,
    cache:   Optional[Dict[str, Optional[str]]] = None,
    user_id: str = "default",
) -> Dict[str, Optional[str]]:
    """
    Resolve each email → Graph user id (GET /users/{email}). Unresolvable /
    erroring addresses map to None.

    Concurrency is bounded by Semaphore(5). Requires the User.ReadBasic.All
    scope. If `cache` is supplied, it is consulted before any Graph call and
    written back for BOTH successes and Nones, so known-unresolvable addresses
    are not re-fetched within the request.
    """
    # Normalise + dedupe input.
    result: Dict[str, Optional[str]] = {}
    norm: List[str] = []
    seen = set()
    for e in emails:
        el = (e or "").strip().lower()
        if el and el not in seen:
            seen.add(el)
            norm.append(el)
    if not norm:
        return result

    # Serve from cache where possible; collect the rest.
    to_fetch: List[str] = []
    for e in norm:
        if cache is not None and e in cache:
            result[e] = cache[e]
        else:
            to_fetch.append(e)

    if not to_fetch:
        return result

    token = await _get_token(user_id)
    if not token:
        # Cannot resolve anything — every uncached email degrades to None.
        for e in to_fetch:
            result[e] = None
            if cache is not None:
                cache[e] = None
        return result

    headers = {"Authorization": f"Bearer {token}"}
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _one(email: str) -> None:
        async with sem:
            uid = await _resolve_one(email, headers)
        result[email] = uid
        if cache is not None:
            cache[email] = uid

    await asyncio.gather(*[_one(e) for e in to_fetch])
    return result


# ── Internal ────────────────────────────────────────────────────────────────

async def _resolve_one(email: str, headers: dict) -> Optional[str]:
    """One GET /users/{email}. 404/403/any error → None (logged, not raised)."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{GRAPH}/users/{email}?$select=id", headers=headers
            )
        if resp.status_code == 200:
            return resp.json().get("id")
        logger.info(f"graph_users: /users/{email} returned {resp.status_code}")
        return None
    except Exception as e:
        logger.info(f"graph_users: /users/{email} failed: {e}")
        return None
