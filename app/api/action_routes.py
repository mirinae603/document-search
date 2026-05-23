# api/action_routes.py
# REST endpoints for the "actions" capability — Phase 1: PROPOSE ONLY.
#
# Nothing in this file performs an external side effect. There are no Microsoft
# Graph writes, no email sending, no Teams posting. Every route reads or writes
# only the local `action_proposals` table (and, for proposing, reads LanceDB
# and calls the LLM).
#
# PHASE 2 EXTENSION POINT — execute_proposal():
#   The body of execute_proposal() is the single place where real execution
#   gets wired in. In Phase 1 it is a dry run: it flips status to `executed`,
#   stamps executed_at, and records a {"dry_run": true} audit entry. In Phase 2
#   a dispatcher call keyed on ActionProposal.type plugs in there (before the
#   status/audit writes) — routing, validation, audit, and storage in this file
#   stay exactly as they are.
#
# DI: follows intelligence_routes.py — a module-level `_store` global injected
# at startup by init_action_routes(), called from main.py's lifespan.
from __future__ import annotations

import logging
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, model_validator

from actions.models import (
    ActionStatus,
    ActionType,
    make_audit_entry,
    now_iso,
)
from actions.proposer import (
    ConversationNotFoundError,
    ThreadNotFoundError,
    propose_draft_email,
    propose_teams_reply,
)
from actions.storage import (
    create_proposal,
    get_proposal,
    list_proposals,
    save_payload_edit,
    save_status_change,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/actions", tags=["Actions"])

_store = None   # injected at startup

# TODO(phase-2): replace with the authenticated user from request context.
# intelligence_routes.py carries no user identifier today, so audit actors and
# created_by are hardcoded for now — same placeholder used in proposer.py.
_ACTOR = "system"


def init_action_routes(store) -> None:
    global _store
    _store = store
    logger.info("✓ Action routes initialised")


def _require_store():
    if _store is None:
        raise HTTPException(503, "Actions module not initialised — check server startup logs.")


def _parse_status(value: Optional[str]) -> Optional[ActionStatus]:
    if value is None:
        return None
    try:
        return ActionStatus(value)
    except ValueError:
        raise HTTPException(400, f"Invalid status filter: '{value}'")


def _parse_type(value: Optional[str]) -> Optional[ActionType]:
    if value is None:
        return None
    try:
        return ActionType(value)
    except ValueError:
        raise HTTPException(400, f"Invalid type filter: '{value}'")


# ── Propose: draft email ──────────────────────────────────────────────────────

class DraftEmailProposeRequest(BaseModel):
    thread_id:    str
    instructions: Optional[str] = None
    tone:         Optional[str] = None


@router.post("/propose/draft-email")
async def propose_draft_email_route(req: DraftEmailProposeRequest):
    """
    Draft a reply to an indexed Outlook thread and persist it as an
    ActionProposal (status=proposed). Drafts only — nothing is sent.
    """
    _require_store()
    try:
        proposal = await propose_draft_email(
            _store,
            thread_id    = req.thread_id,
            instructions = req.instructions,
            tone         = req.tone,
            created_by   = _ACTOR,
        )
    except ThreadNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.error(f"propose_draft_email failed: {e}", exc_info=True)
        raise HTTPException(500, f"Draft proposal failed: {e}")

    create_proposal(proposal)
    return proposal.to_dict()


# ── Propose: teams reply ──────────────────────────────────────────────────────

class ProposeTeamsReplyRequest(BaseModel):
    conversation_kind: Literal["dm", "channel"]
    chat_id:      Optional[str] = None
    channel_id:   Optional[str] = None
    team_id:      Optional[str] = None
    instructions: Optional[str] = None
    tone:         Optional[str] = None

    @model_validator(mode="after")
    def _check_kind_id_combination(self) -> "ProposeTeamsReplyRequest":
        """
        Reject invalid conversation_kind / id combinations at request parsing —
        a ValueError here surfaces as HTTP 422, not a 500 from the proposer.
        """
        if self.conversation_kind == "dm":
            if not self.chat_id:
                raise ValueError("conversation_kind='dm' requires a non-empty chat_id.")
            if self.channel_id or self.team_id:
                raise ValueError(
                    "conversation_kind='dm' must not include channel_id or team_id."
                )
        else:  # "channel"
            if not self.channel_id or not self.team_id:
                raise ValueError(
                    "conversation_kind='channel' requires both channel_id and team_id."
                )
            if self.chat_id:
                raise ValueError("conversation_kind='channel' must not include chat_id.")
        return self


@router.post("/propose/teams-reply")
async def propose_teams_reply_route(req: ProposeTeamsReplyRequest):
    """
    Draft a reply to an indexed Teams conversation (DM or channel post) and
    persist it as an ActionProposal (status=proposed). Drafts only — nothing
    is sent.
    """
    _require_store()
    try:
        proposal = await propose_teams_reply(
            _store,
            conversation_kind = req.conversation_kind,
            chat_id           = req.chat_id,
            channel_id        = req.channel_id,
            team_id           = req.team_id,
            instructions      = req.instructions,
            tone              = req.tone,
            created_by        = _ACTOR,
        )
    except ConversationNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        # Defence in depth — ProposeTeamsReplyRequest already rejects bad
        # combinations with a 422, but the proposer enforces them too.
        raise HTTPException(422, str(e))
    except Exception as e:
        logger.error(f"propose_teams_reply failed: {e}", exc_info=True)
        raise HTTPException(500, f"Teams reply proposal failed: {e}")

    create_proposal(proposal)
    return proposal.to_dict()


# ── List ──────────────────────────────────────────────────────────────────────

@router.get("/proposals")
async def list_proposals_route(
    status: Optional[str] = Query(None, description="Filter by status"),
    type:   Optional[str] = Query(None, description="Filter by action type"),
    limit:  int           = Query(50, ge=1, le=200),
    offset: int           = Query(0,  ge=0),
):
    """List proposals, newest first, with optional status/type filters."""
    proposals = list_proposals(
        status = _parse_status(status),
        type_  = _parse_type(type),
        limit  = limit,
        offset = offset,
    )
    return {
        "count":     len(proposals),
        "limit":     limit,
        "offset":    offset,
        "proposals": [p.to_dict() for p in proposals],
    }


# ── Get one ───────────────────────────────────────────────────────────────────

@router.get("/proposals/{proposal_id}")
async def get_proposal_route(proposal_id: str):
    proposal = get_proposal(proposal_id)
    if not proposal:
        raise HTTPException(404, "Proposal not found")
    return proposal.to_dict()


# ── Patch (edit current_payload) ──────────────────────────────────────────────

@router.patch("/proposals/{proposal_id}")
async def patch_proposal_route(
    proposal_id: str,
    payload: Dict[str, Any] = Body(..., description="Partial current_payload"),
):
    """
    Apply a partial update to current_payload. proposed_payload is never
    touched. status flips proposed -> edited (and is left as-is otherwise).
    """
    proposal = get_proposal(proposal_id)
    if not proposal:
        raise HTTPException(404, "Proposal not found")
    if not isinstance(payload, dict) or not payload:
        raise HTTPException(400, "Request body must be a non-empty partial payload object.")

    new_current = {**proposal.current_payload, **payload}
    new_status  = (
        ActionStatus.EDITED
        if proposal.status == ActionStatus.PROPOSED
        else proposal.status
    )
    audit = proposal.audit_log + [make_audit_entry(
        "edited", _ACTOR, {"changed_keys": sorted(payload.keys())},
    )]

    save_payload_edit(proposal_id, new_current, new_status.value, audit)
    return get_proposal(proposal_id).to_dict()


# ── Delete (soft — dismiss) ───────────────────────────────────────────────────

@router.delete("/proposals/{proposal_id}")
async def dismiss_proposal_route(proposal_id: str):
    """Soft delete: set status=dismissed. The row is kept."""
    proposal = get_proposal(proposal_id)
    if not proposal:
        raise HTTPException(404, "Proposal not found")

    audit = proposal.audit_log + [make_audit_entry("dismissed", _ACTOR, {})]
    save_status_change(proposal_id, ActionStatus.DISMISSED.value, audit)
    return get_proposal(proposal_id).to_dict()


# ── Execute (Phase 1: dry run only) ───────────────────────────────────────────

@router.post("/proposals/{proposal_id}/execute")
async def execute_proposal_route(proposal_id: str):
    """
    Phase 1: DRY RUN ONLY. Flips status to `executed`, stamps executed_at, and
    records a {"dry_run": true} audit entry. Performs NO external HTTP call.

    Phase 2 plugs the real dispatcher in below, before the status/audit writes.
    """
    proposal = get_proposal(proposal_id)
    if not proposal:
        raise HTTPException(404, "Proposal not found")

    # ── PHASE 2 EXTENSION POINT ──────────────────────────────────────────────
    # The dispatcher call (keyed on proposal.type) goes here. In Phase 1 there
    # is no external side effect — execution is simulated.
    executed_at = now_iso()
    audit = proposal.audit_log + [make_audit_entry(
        "executed", _ACTOR, {"dry_run": True},
    )]

    save_status_change(
        proposal_id,
        ActionStatus.EXECUTED.value,
        audit,
        executed_at = executed_at,
    )
    return get_proposal(proposal_id).to_dict()
