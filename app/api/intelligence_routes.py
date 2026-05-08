# api/intelligence_routes.py
# REST endpoints for the three intelligence features.
# All endpoints are GET + query params so they can be called from a frontend
# widget, curl, or Postman without a request body.
#
# Routes:
#   GET /intelligence/outlook/summary      → email digest (last N hours)
#   GET /intelligence/teams/summary        → teams channel digest
#   GET /intelligence/outlook/priority     → priority-ranked emails (tool calling)
#   GET /intelligence/teams/questions      → unanswered question tracker
#
# The `_store` global is injected at startup via init_intelligence_routes().
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from intelligence.priority         import get_priority_emails
from intelligence.question_tracker import get_unanswered_questions
from intelligence.summariser       import summarise_outlook, summarise_teams

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/intelligence", tags=["Intelligence"])

_store = None   # injected at startup


def init_intelligence_routes(store) -> None:
    global _store
    _store = store
    logger.info("✓ Intelligence routes initialised")


def _require_store():
    if _store is None:
        raise HTTPException(503, "Intelligence module not initialised — check server startup logs.")


# ── Outlook summary ───────────────────────────────────────────────────────────

@router.get("/outlook/summary")
async def outlook_summary(
    hours: int = Query(500, ge=1, le=168, description="Look-back window in hours (max 7 days)")
):
    """
    Email digest: one summary per thread received in the last `hours`,
    plus a one-paragraph overall digest.

    Useful for: morning briefings, checking what landed while you were away.
    """
    _require_store()
    try:
        return await summarise_outlook(_store, hours=hours)
    except Exception as e:
        logger.error(f"outlook_summary failed: {e}", exc_info=True)
        raise HTTPException(500, f"Summary generation failed: {e}")


# ── Teams summary ─────────────────────────────────────────────────────────────

@router.get("/teams/summary")
async def teams_summary(
    hours:          Optional[int] = Query(None, ge=1, le=168, description="Optional time window"),
    channel_filter: Optional[str] = Query(None, description="Filter by channel name substring"),
):
    """
    Teams digest: one summary per channel (optionally filtered by name / time window),
    plus a one-paragraph overall digest.

    Useful for: catching up on all channels at once, daily standups.
    """
    _require_store()
    try:
        return await summarise_teams(_store, channel_filter=channel_filter, hours=hours)
    except Exception as e:
        logger.error(f"teams_summary failed: {e}", exc_info=True)
        raise HTTPException(500, f"Summary generation failed: {e}")


# ── Priority emails ───────────────────────────────────────────────────────────

@router.get("/outlook/priority")
async def outlook_priority(
    hours:        int           = Query(48,  ge=1,  le=168, description="Look-back window (default 48 hrs)"),
    top_n:        int           = Query(10,  ge=1,  le=30,  description="Max emails to return"),
    user_context: Optional[str] = Query(None, description="Your role/context to personalise scoring"),
):
    """
    Returns the top-N priority email threads from the last `hours`, ranked by
    an LLM that uses **tool calling** to produce a guaranteed structured schema:

    - priority_score (1–10)
    - priority_label (critical / high / medium / low)
    - reason (why it's priority)
    - urgency_signals (["ASAP", "EOD", ...])
    - has_deadline + deadline_text
    - action_required + extracted_tasks

    Useful for: "what needs my attention before EOD?" triage.
    """
    _require_store()
    try:
        return await get_priority_emails(
            _store,
            hours        = hours,
            top_n        = top_n,
            user_context = user_context,
        )
    except Exception as e:
        logger.error(f"outlook_priority failed: {e}", exc_info=True)
        raise HTTPException(500, f"Priority ranking failed: {e}")


# ── Teams question tracker ────────────────────────────────────────────────────

@router.get("/teams/questions")
async def teams_questions(
    hours:           Optional[int] = Query(None, ge=1, le=168, description="Optional time window"),
    channel_filter:  Optional[str] = Query(None, description="Filter by channel name substring"),
    unanswered_only: bool          = Query(True,  description="Return only unanswered questions"),
):
    """
    Surfaces questions asked in Teams channels that may not have received a reply.

    Uses a two-phase approach:
    1. Fast regex scan of indexed message turns for "?" patterns
    2. LLM **tool call** to classify: genuine vs rhetorical, answered vs open,
       topic tag, and urgency level

    Grouped by channel, sorted: high urgency first.

    Useful for: identifying blockers, ensuring no one's question gets lost in chat.
    """
    _require_store()
    try:
        return await get_unanswered_questions(
            _store,
            channel_filter  = channel_filter,
            hours           = hours,
            unanswered_only = unanswered_only,
        )
    except Exception as e:
        logger.error(f"teams_questions failed: {e}", exc_info=True)
        raise HTTPException(500, f"Question tracking failed: {e}")


# ── Combined briefing (bonus) ─────────────────────────────────────────────────

@router.get("/briefing")
async def daily_briefing(
    hours: int = Query(24, ge=1, le=164, description="Look-back window for briefing"),
):
    """
    Combined morning briefing:
      - Outlook summary (last N hours)
      - Top 5 priority emails
      - Teams summary
      - Unanswered Teams questions

    Returns all four payloads in a single response.
    Useful for: a single API call to power a dashboard widget.
    """
    _require_store()
    try:
        import asyncio
        outlook_sum, priority, teams_sum, questions = await asyncio.gather(
            summarise_outlook(_store, hours=hours),
            get_priority_emails(_store, hours=hours, top_n=5),
            summarise_teams(_store, hours=hours),
            get_unanswered_questions(_store, hours=hours),
            return_exceptions=True,
        )

        def _safe(result, label):
            if isinstance(result, Exception):
                logger.error(f"briefing/{label} failed: {result}")
                return {"error": str(result)}
            return result

        return {
            "generated_at":    __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ).isoformat(),
            "period_hours":    hours,
            "outlook_summary": _safe(outlook_sum, "outlook_summary"),
            "priority_emails": _safe(priority,    "priority"),
            "teams_summary":   _safe(teams_sum,   "teams_summary"),
            "open_questions":  _safe(questions,   "questions"),
        }
    except Exception as e:
        logger.error(f"daily_briefing failed: {e}", exc_info=True)
        raise HTTPException(500, f"Briefing generation failed: {e}")
