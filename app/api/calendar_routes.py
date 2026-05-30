# api/calendar_routes.py
# REST endpoint for read-only calendar meeting prep.
#
# Route:
#   GET /calendar/prep → prep cards for upcoming Outlook meetings, each fusing
#                        live calendar data with related ingested email/Teams
#                        context retrieved from LanceDB.
#
# Mirrors api/intelligence_routes.py exactly: GET + query params, module-level
# globals injected at startup (NO FastAPI Depends), and the same exception
# handling (logger.error(..., exc_info=True) → HTTPException(500, ...)).
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from intelligence.calendar_priority import get_calendar_priority
from intelligence.meeting_prep import get_meeting_prep

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/calendar", tags=["Calendar"])

_store    = None   # injected at startup
_calendar = None   # injected at startup
_embedder = None   # injected at startup (optional — enables vector retrieval)


def init_calendar_routes(store, calendar, embedder=None) -> None:
    global _store, _calendar, _embedder
    _store    = store
    _calendar = calendar
    _embedder = embedder
    logger.info("✓ Calendar routes initialised")


def _require():
    if _store is None or _calendar is None:
        raise HTTPException(
            503, "Calendar module not initialised — check server startup logs."
        )


def _require_calendar():
    # /calendar/priority needs only the live calendar — it never touches the store.
    if _calendar is None:
        raise HTTPException(
            503, "Calendar module not initialised — check server startup logs."
        )


@router.get("/prep")
async def calendar_prep(
    hours_ahead:         int           = Query(24, ge=1, le=168, description="Look-ahead window in hours (max 7 days)"),
    top_n:               int           = Query(5,  ge=1, le=20,  description="Max meetings to return"),
    context_per_meeting: int           = Query(5,  ge=1, le=15,  description="Candidate context threads per meeting"),
    user_context:        Optional[str] = Query(None, description="Your role/context to personalise the prep"),
):
    """
    Prep cards for upcoming Outlook calendar meetings.

    For each meeting in the next `hours_ahead` hours (live Graph read), retrieves
    related prior email/Teams context from the ingested LanceDB corpus using a
    hybrid of participant-overlap filtering and vector search, then synthesises a
    structured prep card per meeting via an LLM tool call.

    Read-only: this endpoint creates, modifies, and sends nothing.
    """
    _require()
    try:
        return await get_meeting_prep(
            _store,
            _calendar,
            hours_ahead         = hours_ahead,
            top_n               = top_n,
            context_per_meeting = context_per_meeting,
            user_context        = user_context,
            embedder            = _embedder,
        )
    except Exception as e:
        logger.error(f"calendar_prep failed: {e}", exc_info=True)
        raise HTTPException(500, f"Meeting prep generation failed: {e}")


@router.get("/priority")
async def calendar_priority(
    hours_ahead:  int           = Query(24, ge=1, le=168, description="Look-ahead window in hours (max 7 days)"),
    top_n:        int           = Query(10, ge=1, le=30,  description="Max meetings to return"),
    user_context: Optional[str] = Query(None, description="Your role/context to personalise scoring"),
):
    """
    Fast triage ranking of upcoming Outlook calendar meetings.

    Ranks the meetings in the next `hours_ahead` hours by importance via a SINGLE
    LLM tool call, returning a priority_score, priority_label, reason and
    urgency_signals per meeting alongside deterministic signals (conflicts,
    externality, attendance-required, response-status) computed in code.

    Not corpus-aware — that is /calendar/prep. Read-only: creates, modifies, and
    sends nothing, and never touches LanceDB.
    """
    _require_calendar()
    try:
        return await get_calendar_priority(
            _calendar,
            hours_ahead  = hours_ahead,
            top_n        = top_n,
            user_context = user_context,
        )
    except Exception as e:
        logger.error(f"calendar_priority failed: {e}", exc_info=True)
        raise HTTPException(500, f"Calendar priority ranking failed: {e}")
