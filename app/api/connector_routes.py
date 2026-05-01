# api/connector_routes.py
# All connector-related HTTP endpoints.
#
# OAuth flow (Teams example):
#   1. GET  /connector/teams/oauth/start      → redirect to Microsoft login
#   2. GET  /connector/teams/oauth/callback   → exchange code, save token
#
# Sync:
#   3. POST /connector/teams/sync             → trigger delta or full sync
#   4. GET  /connector/teams/status           → last sync time, doc count
#   5. DELETE /connector/teams               → disconnect (delete token + state)
#
# The DEV_USER constant matches qa_routes.py — replace with real auth later.
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import RedirectResponse

from connectors.chat_indexer import ChatDocumentIndexer
from connectors.outlook import OutlookConnector
from connectors.teams import TeamsConnector
from connectors.token_store import (
    clear_platform_state,
    delete_token,
    get_state,
    has_token,
    set_state,
)

logger  = logging.getLogger(__name__)
router  = APIRouter(prefix="/connector", tags=["Connectors"])

DEV_USER = "dev-user-001"

# Injected at startup — set by init_connector_routes()
_chat_indexer:      Optional[ChatDocumentIndexer] = None
_teams_connector:   Optional[TeamsConnector]      = None
_outlook_connector: Optional[OutlookConnector]    = None


def init_connector_routes(
    chat_indexer:      ChatDocumentIndexer,
    teams_connector:   TeamsConnector,
    outlook_connector: OutlookConnector,
) -> None:
    global _chat_indexer, _teams_connector, _outlook_connector
    _chat_indexer      = chat_indexer
    _teams_connector   = teams_connector
    _outlook_connector = outlook_connector
    logger.info("✓ Connector routes initialised (teams + outlook)")


# ── OAuth ─────────────────────────────────────────────────────────────────────

@router.get("/teams/oauth/start")
async def teams_oauth_start():
    """
    Redirect the user to the Microsoft login page.
    After login, Microsoft redirects to /connector/teams/oauth/callback.
    """
    if not _teams_connector:
        raise HTTPException(503, "Teams connector not initialised")
    auth_url = _teams_connector.get_auth_url(state=DEV_USER)
    return RedirectResponse(auth_url)


@router.get("/connector/teams/oauth/callback", include_in_schema=False)
@router.get("/teams/oauth/callback")
async def teams_oauth_callback(
    code:  Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
):
    """
    Microsoft posts the auth code here.
    Exchanges it for access + refresh tokens and stores them.
    On success, triggers an initial full sync in the background.
    """
    if error:
        raise HTTPException(400, f"OAuth error: {error}")
    if not code:
        raise HTTPException(400, "Missing 'code' query parameter")
    if not _teams_connector:
        raise HTTPException(503, "Teams connector not initialised")

    user_id = state or DEV_USER
    ok = await _teams_connector.exchange_code(code, user_id)
    if not ok:
        raise HTTPException(500, "Token exchange failed — check server logs")

    # Kick off the initial full sync asynchronously
    set_state(user_id, "teams", "sync_status", "initial_sync_queued")
    asyncio.create_task(_run_sync(user_id=user_id, delta=False))

    return {
        "status":  "authenticated",
        "user_id": user_id,
        "message": "Teams connected. Initial sync started in background.",
        "next":    f"/connector/teams/status",
    }


# ── Sync ──────────────────────────────────────────────────────────────────────

@router.post("/teams/sync")
async def teams_sync(
    background_tasks: BackgroundTasks,
    delta: bool = Query(True,  description="True = only new messages; False = full re-sync"),
    wait:  bool = Query(False, description="True = block until sync complete (dev use)"),
):
    """
    Trigger a Teams sync.

    delta=True  (default) → fast; only pulls messages since the last run.
    delta=False           → full pull; use to repair a missed sync or first run.
    wait=True             → returns results synchronously (avoid in production).
    """
    if not has_token(DEV_USER, "teams"):
        raise HTTPException(
            401,
            detail={
                "error":    "not_authenticated",
                "message":  "Teams not connected.",
                "auth_url": "/connector/teams/oauth/start",
            },
        )

    if wait:
        result = await _run_sync(user_id=DEV_USER, delta=delta)
        return result

    background_tasks.add_task(_run_sync, user_id=DEV_USER, delta=delta)
    return {"status": "queued", "delta": delta}


@router.get("/teams/status")
async def teams_status():
    """Return current connection and sync status for Teams."""
    connected   = has_token(DEV_USER, "teams")
    last_sync   = get_state(DEV_USER, "teams", "last_sync")
    sync_status = get_state(DEV_USER, "teams", "sync_status", "idle")
    doc_count   = get_state(DEV_USER, "teams", "last_doc_count", 0)
    error       = get_state(DEV_USER, "teams", "last_sync_error")

    return {
        "platform":    "teams",
        "connected":   connected,
        "sync_status": sync_status,
        "last_sync":   last_sync,
        "docs_indexed": doc_count,
        "last_error":  error,
        "auth_url":    None if connected else "/connector/teams/oauth/start",
    }


# ── Disconnect ────────────────────────────────────────────────────────────────

@router.delete("/teams")
async def teams_disconnect():
    """
    Disconnect Teams — deletes stored tokens and all sync state.
    Indexed conversation chunks are NOT removed from LanceDB.
    """
    delete_token(DEV_USER, "teams")
    clear_platform_state(DEV_USER, "teams")
    return {"status": "disconnected", "platform": "teams"}


# ── Internal sync runner ──────────────────────────────────────────────────────

async def _run_sync(user_id: str, delta: bool) -> dict:
    """
    Core sync logic — called both directly (wait=True) and as a background task.
    Fetches conversations from Teams, indexes each one via ChatDocumentIndexer.
    Updates connector_state with progress so /status stays accurate.
    """
    set_state(user_id, "teams", "sync_status", "running")
    set_state(user_id, "teams", "last_sync_error", None)

    indexed = 0
    failed  = 0
    errors  = []

    try:
        conversations = await _teams_connector.fetch_conversations(
            delta=delta, user_id=user_id
        )
        logger.info(f"Sync: {len(conversations)} conversations to index")

        for doc in conversations:
            try:
                await _chat_indexer.index_conversation(doc)
                indexed += 1
            except Exception as e:
                failed += 1
                err = f"{doc.chat_id}: {e}"
                errors.append(err)
                logger.error(f"Index failed — {err}", exc_info=True)

        result = {
            "status":       "complete",
            "delta":        delta,
            "total":        len(conversations),
            "indexed":      indexed,
            "failed":       failed,
            "errors":       errors[:10],   # cap error list for readability
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }

        set_state(user_id, "teams", "sync_status",    "idle")
        set_state(user_id, "teams", "last_sync",      result["completed_at"])
        set_state(user_id, "teams", "last_doc_count", indexed)
        if errors:
            set_state(user_id, "teams", "last_sync_error", errors[0])

        logger.info(
            f"Sync complete: indexed={indexed} failed={failed} "
            f"(delta={delta}, user={user_id})"
        )
        return result

    except Exception as e:
        msg = str(e)
        logger.error(f"Sync aborted: {msg}", exc_info=True)
        set_state(user_id, "teams", "sync_status",   "error")
        set_state(user_id, "teams", "last_sync_error", msg)
        return {
            "status":  "error",
            "message": msg,
            "indexed": indexed,
            "failed":  failed,
        }


# ══════════════════════════════════════════════════════════════════════════════
# OUTLOOK ROUTES
# Mirror of the Teams routes — same pattern, different connector + platform key.
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/outlook/oauth/start")
async def outlook_oauth_start():
    """Redirect the user to Microsoft login for Outlook (Mail.Read scope)."""
    if not _outlook_connector:
        raise HTTPException(503, "Outlook connector not initialised")
    auth_url = _outlook_connector.get_auth_url(state=DEV_USER)
    return RedirectResponse(auth_url)


@router.get("/connector/outlook/oauth/callback", include_in_schema=False)
@router.get("/outlook/oauth/callback")
async def outlook_oauth_callback(
    code:  Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
):
    """
    Microsoft posts the Outlook auth code here.
    Exchanges it for tokens, stores them, kicks off initial sync.
    """
    if error:
        raise HTTPException(400, f"OAuth error: {error}")
    if not code:
        raise HTTPException(400, "Missing 'code' query parameter")
    if not _outlook_connector:
        raise HTTPException(503, "Outlook connector not initialised")

    user_id = state or DEV_USER
    ok = await _outlook_connector.exchange_code(code, user_id)
    if not ok:
        raise HTTPException(500, "Token exchange failed — check server logs")

    set_state(user_id, "outlook", "sync_status", "initial_sync_queued")
    asyncio.create_task(_run_outlook_sync(user_id=user_id, delta=False))

    return {
        "status":  "authenticated",
        "user_id": user_id,
        "message": "Outlook connected. Initial email sync started in background.",
        "next":    "/connector/outlook/status",
    }


@router.post("/outlook/sync")
async def outlook_sync(
    background_tasks: BackgroundTasks,
    delta: bool = Query(True,  description="True = only new emails; False = full re-sync"),
    wait:  bool = Query(False, description="True = block until sync complete (dev use)"),
):
    """
    Trigger an Outlook email sync.

    delta=True  → fast; pulls only emails newer than the stored deltaLink.
    delta=False → full pull; use for first run or to repair a missed sync.
    """
    if not has_token(DEV_USER, "outlook"):
        raise HTTPException(
            401,
            detail={
                "error":    "not_authenticated",
                "message":  "Outlook not connected.",
                "auth_url": "/connector/outlook/oauth/start",
            },
        )

    if wait:
        result = await _run_outlook_sync(user_id=DEV_USER, delta=delta)
        return result

    background_tasks.add_task(_run_outlook_sync, user_id=DEV_USER, delta=delta)
    return {"status": "queued", "delta": delta}


@router.get("/outlook/status")
async def outlook_status():
    """Return current connection and sync status for Outlook."""
    connected   = has_token(DEV_USER, "outlook")
    last_sync   = get_state(DEV_USER, "outlook", "last_sync")
    sync_status = get_state(DEV_USER, "outlook", "sync_status", "idle")
    doc_count   = get_state(DEV_USER, "outlook", "last_doc_count", 0)
    error       = get_state(DEV_USER, "outlook", "last_sync_error")

    return {
        "platform":    "outlook",
        "connected":   connected,
        "sync_status": sync_status,
        "last_sync":   last_sync,
        "docs_indexed": doc_count,
        "last_error":  error,
        "auth_url":    None if connected else "/connector/outlook/oauth/start",
    }


@router.delete("/outlook")
async def outlook_disconnect():
    """
    Disconnect Outlook — deletes stored tokens and sync state.
    Already-indexed email chunks remain in LanceDB.
    """
    delete_token(DEV_USER, "outlook")
    clear_platform_state(DEV_USER, "outlook")
    return {"status": "disconnected", "platform": "outlook"}


# ── Convenience: status for all connectors at once ────────────────────────────

@router.get("/status")
async def all_connectors_status():
    """Return connection status for every registered connector."""
    return {
        "connectors": [
            {
                "platform":    "teams",
                "connected":   has_token(DEV_USER, "teams"),
                "sync_status": get_state(DEV_USER, "teams", "sync_status", "idle"),
                "last_sync":   get_state(DEV_USER, "teams", "last_sync"),
                "docs_indexed": get_state(DEV_USER, "teams", "last_doc_count", 0),
                "auth_url":    "/connector/teams/oauth/start",
            },
            {
                "platform":    "outlook",
                "connected":   has_token(DEV_USER, "outlook"),
                "sync_status": get_state(DEV_USER, "outlook", "sync_status", "idle"),
                "last_sync":   get_state(DEV_USER, "outlook", "last_sync"),
                "docs_indexed": get_state(DEV_USER, "outlook", "last_doc_count", 0),
                "auth_url":    "/connector/outlook/oauth/start",
            },
        ]
    }


# ── Internal Outlook sync runner ──────────────────────────────────────────────

async def _run_outlook_sync(user_id: str, delta: bool) -> dict:
    """
    Fetch email threads from Outlook and index each thread as a ConversationDoc.
    Mirrors _run_sync() for Teams but uses _outlook_connector and platform="outlook".
    """
    set_state(user_id, "outlook", "sync_status", "running")
    set_state(user_id, "outlook", "last_sync_error", None)

    indexed = 0
    failed  = 0
    errors  = []

    try:
        conversations = await _outlook_connector.fetch_conversations(
            delta=delta, user_id=user_id
        )
        logger.info(f"Outlook sync: {len(conversations)} email threads to index")

        for doc in conversations:
            try:
                await _chat_indexer.index_conversation(doc)
                indexed += 1
            except Exception as e:
                failed += 1
                err = f"{doc.chat_id}: {e}"
                errors.append(err)
                logger.error(f"Outlook index failed — {err}", exc_info=True)

        result = {
            "status":       "complete",
            "delta":        delta,
            "total":        len(conversations),
            "indexed":      indexed,
            "failed":       failed,
            "errors":       errors[:10],
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }

        set_state(user_id, "outlook", "sync_status",    "idle")
        set_state(user_id, "outlook", "last_sync",      result["completed_at"])
        set_state(user_id, "outlook", "last_doc_count", indexed)
        if errors:
            set_state(user_id, "outlook", "last_sync_error", errors[0])

        logger.info(
            f"Outlook sync complete: indexed={indexed} failed={failed} "
            f"(delta={delta}, user={user_id})"
        )
        return result

    except Exception as e:
        msg = str(e)
        logger.error(f"Outlook sync aborted: {msg}", exc_info=True)
        set_state(user_id, "outlook", "sync_status",    "error")
        set_state(user_id, "outlook", "last_sync_error", msg)
        return {
            "status":  "error",
            "message": msg,
            "indexed": indexed,
            "failed":  failed,
        }
