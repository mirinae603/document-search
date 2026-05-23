# actions/proposer.py
# Builds ActionProposal objects from indexed content.
#
# Phase 1: propose_draft_email() only.
#
# Flow (mirrors intelligence/summariser.py's "breadth, not relevance" reasoning
# — a draft reply needs the whole thread, so we do a filtered scan, not a
# vector search):
#   1. Scan LanceDB for all 'outlook' chunks with the given thread_id
#   2. Sort chunks chronologically by sent_at
#   3. Build a context string + record every chunk in source_refs
#   4. Call llm_tool_call() with draft_email_tool (tool_choice="required")
#   5. Assemble and return an ActionProposal (status=proposed)
#
# This function persists nothing and performs no external side effect — it
# only reads LanceDB and calls the LLM. Persistence is the route's job.
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Literal, Optional

from config import QA_MODEL

from actions.models import (
    ActionProposal,
    ActionStatus,
    ActionType,
    make_audit_entry,
    now_iso,
)
from actions.registry import get_tool
from intelligence.llm import llm_tool_call

logger = logging.getLogger(__name__)

# Cap on chunks pulled for one thread — a thread should never approach this.
_MAX_THREAD_CHUNKS = 500

# TODO(phase-2): replace with the authenticated user from request context.
_DEFAULT_CREATED_BY = "system"


class ThreadNotFoundError(Exception):
    """Raised when no indexed chunks exist for the requested Outlook thread."""


class ConversationNotFoundError(Exception):
    """Raised when no indexed chunks exist for the requested Teams conversation."""


# ── Public API ───────────────────────────────────────────────────────────────

async def propose_draft_email(
    store,
    thread_id:    str,
    instructions: Optional[str] = None,
    tone:         Optional[str] = None,
    created_by:   str = _DEFAULT_CREATED_BY,
) -> ActionProposal:
    """
    Build a draft-email reply proposal for an indexed Outlook thread.

    Raises ThreadNotFoundError if the thread has no indexed chunks (the route
    maps this to a 404 — we never proceed with empty context).
    Raises RuntimeError if the LLM returns no tool call.
    """
    rows = _scan_outlook_thread(store, thread_id)
    if not rows:
        raise ThreadNotFoundError(
            f"No indexed Outlook thread found for thread_id '{thread_id}'."
        )

    context     = _build_context(rows)
    source_refs = _build_source_refs(rows)
    if not source_refs:
        # Defensive: rows existed, so this cannot happen — but source_refs is
        # non-negotiable, so we refuse to build a proposal without it.
        raise ThreadNotFoundError(
            f"Outlook thread '{thread_id}' produced no usable source references."
        )

    result = await llm_tool_call(
        messages    = [{"role": "user", "content": _build_user_message(context, instructions)}],
        tools       = [get_tool(ActionType.DRAFT_EMAIL)],
        tool_choice = "required",
        system      = _build_system_prompt(tone),
        model       = QA_MODEL,
    )
    if not result:
        raise RuntimeError("LLM did not return a draft_email_reply tool call.")

    proposed_payload: Dict[str, Any] = {
        "to":                     list(result.get("to") or []),
        "cc":                     list(result.get("cc") or []),
        "subject":                result.get("subject", "") or "",
        "body":                   result.get("body", "") or "",
        "in_reply_to_thread_id":  thread_id,
        # Chunk rows do not carry a per-message Graph id, so this stays null in
        # Phase 1. Phase 2 execution can resolve it from the SeaweedFS raw JSON.
        "in_reply_to_message_id": None,
    }

    audit = [make_audit_entry(
        "created",
        created_by,
        {"thread_id": thread_id, "source_chunk_count": len(source_refs)},
    )]

    return ActionProposal(
        id               = str(uuid.uuid4()),
        type             = ActionType.DRAFT_EMAIL,
        status           = ActionStatus.PROPOSED,
        proposed_payload = proposed_payload,
        # current_payload starts equal (by value) to proposed_payload — deep
        # copy via a JSON round-trip so the two never share nested references.
        current_payload  = json.loads(json.dumps(proposed_payload)),
        source_refs      = source_refs,
        created_at       = now_iso(),
        created_by       = created_by,
        model_used       = QA_MODEL,
        executed_at      = None,
        audit_log        = audit,
    )


async def propose_teams_reply(
    store,
    *,
    conversation_kind: Literal["dm", "channel"],
    chat_id:      Optional[str] = None,
    channel_id:   Optional[str] = None,
    team_id:      Optional[str] = None,
    instructions: Optional[str] = None,
    tone:         Optional[str] = None,
    created_by:   str = _DEFAULT_CREATED_BY,
) -> ActionProposal:
    """
    Build a reply-message proposal for an indexed Teams conversation — either a
    1:1/group DM or a channel post.

    LanceDB column reality (see connectors/chat_indexer.py + connectors/teams.py):
    Teams chunks do NOT have separate chat_id / channel_id / team_id columns.
    chat_indexer writes the conversation identifier into the single `thread_id`
    column for every Teams conversation. For a DM that identifier is the raw
    Graph chat id; for a channel it is the composite `f"{team_id}_{channel_id}"`
    assembled in teams.py. So this proposer scans by `thread_id` in both cases,
    reconstructing the channel composite from the supplied team_id + channel_id.

    Raises ValueError on an invalid conversation_kind / id combination.
    Raises ConversationNotFoundError if the conversation has no indexed chunks
    (the route maps this to a 404).
    Raises RuntimeError if the LLM returns no tool call.
    """
    _validate_teams_target(conversation_kind, chat_id, channel_id, team_id)

    rows = _scan_teams_conversation(
        store, conversation_kind, chat_id, channel_id, team_id
    )
    if not rows:
        raise ConversationNotFoundError(
            f"No indexed Teams {conversation_kind} conversation found."
        )

    context     = _build_teams_context(rows)
    source_refs = _build_source_refs(rows)
    if not source_refs:
        # Defensive: rows existed, so this cannot happen — but source_refs is
        # non-negotiable, so we refuse to build a proposal without it.
        raise ConversationNotFoundError(
            f"Teams {conversation_kind} conversation produced no usable "
            f"source references."
        )

    result = await llm_tool_call(
        messages    = [{"role": "user", "content": _build_teams_user_message(context, instructions)}],
        tools       = [get_tool(ActionType.TEAMS_REPLY)],
        tool_choice = "required",
        system      = _build_teams_system_prompt(conversation_kind, tone),
        model       = QA_MODEL,
    )
    if not result:
        raise RuntimeError("LLM did not return a draft_teams_reply tool call.")

    proposed_payload: Dict[str, Any] = {
        "conversation_kind": conversation_kind,
        "chat_id":           chat_id,
        "channel_id":        channel_id,
        "team_id":           team_id,
        "body":              result.get("body", "") or "",
        "mentions":          list(result.get("mentions") or []),
        # Chunk rows do not carry a per-message Graph id, so this stays null in
        # Phase 1 — same limitation as propose_draft_email. Phase 2 execution
        # can resolve it from the SeaweedFS raw JSON.
        "in_reply_to_message_id": None,
    }

    audit = [make_audit_entry(
        "created",
        created_by,
        {"conversation_kind": conversation_kind, "source_chunk_count": len(source_refs)},
    )]

    return ActionProposal(
        id               = str(uuid.uuid4()),
        type             = ActionType.TEAMS_REPLY,
        status           = ActionStatus.PROPOSED,
        proposed_payload = proposed_payload,
        # current_payload starts equal (by value) to proposed_payload — deep
        # copy via a JSON round-trip so the two never share nested references.
        current_payload  = json.loads(json.dumps(proposed_payload)),
        source_refs      = source_refs,
        created_at       = now_iso(),
        created_by       = created_by,
        model_used       = QA_MODEL,
        executed_at      = None,
        audit_log        = audit,
    )


def _validate_teams_target(
    conversation_kind: str,
    chat_id:    Optional[str],
    channel_id: Optional[str],
    team_id:    Optional[str],
) -> None:
    """
    Enforce the conversation_kind / id cross-field rules. Raises ValueError.
    The route layer validates the same rules (returning 422); this is the
    defence-in-depth copy so the proposer is safe to call directly.
    """
    if conversation_kind == "dm":
        if not chat_id:
            raise ValueError("conversation_kind='dm' requires a non-empty chat_id.")
        if channel_id or team_id:
            raise ValueError(
                "conversation_kind='dm' must not include channel_id or team_id."
            )
    elif conversation_kind == "channel":
        if not channel_id or not team_id:
            raise ValueError(
                "conversation_kind='channel' requires both channel_id and team_id."
            )
        if chat_id:
            raise ValueError("conversation_kind='channel' must not include chat_id.")
    else:
        raise ValueError(
            f"Invalid conversation_kind '{conversation_kind}' (expected 'dm' or 'channel')."
        )


# ── LanceDB scan ─────────────────────────────────────────────────────────────

def _scan_outlook_thread(store, thread_id: str) -> List[dict]:
    """
    Full filtered scan of one Outlook thread — no vector search.
    Returns chunk rows sorted by sent_at ascending (oldest first).
    """
    where = f"platform = 'outlook' AND thread_id = '{_escape(thread_id)}'"
    try:
        rows = (
            store.chunks
            .search()
            .where(where, prefilter=True)
            .limit(_MAX_THREAD_CHUNKS)
            .to_list()
        )
    except Exception as e:
        logger.error(f"LanceDB scan failed (thread_id={thread_id!r}): {e}")
        return []
    rows.sort(key=lambda r: r.get("sent_at", ""))
    return rows


def _escape(value: str) -> str:
    """Escape single quotes for a LanceDB SQL-style where clause."""
    return value.replace("'", "''")


def _teams_thread_id(
    conversation_kind: str,
    chat_id:    Optional[str],
    channel_id: Optional[str],
    team_id:    Optional[str],
) -> str:
    """
    The value stored in the chunk `thread_id` column for this conversation.
    DM  → the raw Graph chat id.
    Channel → the composite `{team_id}_{channel_id}` (assembled in teams.py and
              written verbatim into thread_id by chat_indexer).
    """
    if conversation_kind == "dm":
        return chat_id or ""
    return f"{team_id}_{channel_id}"


def _scan_teams_conversation(
    store,
    conversation_kind: str,
    chat_id:    Optional[str],
    channel_id: Optional[str],
    team_id:    Optional[str],
) -> List[dict]:
    """
    Full filtered scan of one Teams conversation — no vector search, mirrors
    _scan_outlook_thread. Both DMs and channels are addressed via the single
    `thread_id` column (see propose_teams_reply's docstring for why).
    Returns chunk rows sorted by sent_at ascending (oldest first).
    """
    thread_id = _teams_thread_id(conversation_kind, chat_id, channel_id, team_id)
    where = f"platform = 'teams' AND thread_id = '{_escape(thread_id)}'"
    try:
        rows = (
            store.chunks
            .search()
            .where(where, prefilter=True)
            .limit(_MAX_THREAD_CHUNKS)
            .to_list()
        )
    except Exception as e:
        logger.error(f"LanceDB scan failed (teams thread_id={thread_id!r}): {e}")
        return []
    rows.sort(key=lambda r: r.get("sent_at", ""))
    return rows


# ── Context + source refs ────────────────────────────────────────────────────

def _build_context(rows: List[dict]) -> str:
    """
    Render the thread for the LLM. Each chunk row is emitted as a block; a
    chunk may span several message turns, whose own "[name | ts] ..." headers
    are already embedded in the chunk text by chat_indexer.
    """
    parts: List[str] = []
    for i, r in enumerate(rows, start=1):
        parts.append(
            f"--- Chunk {i} ---\n"
            f"From: {_participants_str(r)}\n"
            f"Sent: {r.get('sent_at') or '(unknown)'}\n"
            f"Body: {(r.get('text') or '').strip()}"
        )
    return "\n\n".join(parts)


def _build_source_refs(rows: List[dict]) -> List[Dict[str, Any]]:
    """One source_ref per chunk used to build the proposal."""
    refs: List[Dict[str, Any]] = []
    for r in rows:
        refs.append({
            "chunk_id":  r.get("chunk_id", ""),
            "doc_id":    r.get("document_id") or r.get("file_id", ""),
            "thread_id": r.get("thread_id", ""),
            "platform":  r.get("platform", ""),
            "sent_at":   r.get("sent_at", ""),
        })
    return refs


def _participants_str(row: dict) -> str:
    raw = row.get("participants", "")
    try:
        names = json.loads(raw) if raw else []
    except Exception:
        names = [raw] if raw else []
    return ", ".join(str(n) for n in names) if names else "(unknown)"


# ── Prompt builders ──────────────────────────────────────────────────────────

def _build_system_prompt(tone: Optional[str]) -> str:
    prompt = (
        "You draft reply emails to existing Outlook threads. You are given the "
        "full thread in chronological order. Draft a SINGLE reply email.\n"
        "- 'to' should be the thread participants who expect a response.\n"
        "- Leave 'cc' empty unless the thread clearly warrants copying someone.\n"
        "- 'subject' should be 'Re: <original subject>'.\n"
        "- 'body' must be plain text and include a greeting and a sign-off.\n"
        "You MUST call the draft_email_reply tool. You are drafting only — "
        "nothing is sent."
    )
    if tone:
        prompt += f"\nWrite the reply in this tone: {tone}."
    return prompt


def _build_user_message(context: str, instructions: Optional[str]) -> str:
    msg = f"Outlook thread:\n\n{context}\n\n"
    if instructions:
        msg += f"Instructions for the reply:\n{instructions}\n\n"
    msg += "Draft the reply now by calling draft_email_reply."
    return msg


# ── Teams prompt + context builders ──────────────────────────────────────────

def _build_teams_context(rows: List[dict]) -> str:
    """
    Render a Teams conversation for the LLM. Each chunk row is emitted as a
    single line `From: <participants> | Sent: <ts> | Body: <text>`; a chunk may
    span several message turns, whose own "[name | ts] ..." headers are already
    embedded in the chunk text by chat_indexer.
    """
    parts: List[str] = []
    for r in rows:
        parts.append(
            f"From: {_participants_str(r)} | "
            f"Sent: {r.get('sent_at') or '(unknown)'} | "
            f"Body: {(r.get('text') or '').strip()}"
        )
    return "\n\n".join(parts)


def _build_teams_system_prompt(conversation_kind: str, tone: Optional[str]) -> str:
    where = "a direct message (DM)" if conversation_kind == "dm" else "a Teams channel"
    prompt = (
        f"You draft reply messages to existing Teams conversations. This is "
        f"{where}. You are given the full conversation in chronological order. "
        "Draft a SINGLE reply message.\n"
        "- 'body' is the plain-text reply. The conversation already defines "
        "the audience — do not add recipients or a subject line.\n"
        "- If you reference participants, write them as plain '@Name' mentions "
        "in the body and also list those names in 'mentions'. They will not be "
        "resolved or hyperlinked at this stage.\n"
        "You MUST call the draft_teams_reply tool. You are drafting only — "
        "nothing is sent."
    )
    if tone:
        prompt += f"\nWrite the reply in this tone: {tone}."
    return prompt


def _build_teams_user_message(context: str, instructions: Optional[str]) -> str:
    msg = f"Teams conversation:\n\n{context}\n\n"
    if instructions:
        msg += f"Instructions for the reply:\n{instructions}\n\n"
    msg += "Draft the reply now by calling draft_teams_reply."
    return msg
