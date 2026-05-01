# intelligence/priority.py
# Ranks indexed Outlook email threads by priority using LLM tool calling.
#
# Why tool calling (not plain prompting)?
#   - Guarantees a typed schema per email: score, label, reason, deadline, tasks
#   - `tool_choice="required"` means the model MUST fill every field
#   - No JSON parsing guesswork — the OpenAI function-calling contract handles it
#   - Easy to extend: add a field to the schema, it appears in every response
#
# Pipeline:
#   1. Pull Outlook chunks from the last N hours (default 48)
#   2. Group by thread_id → one "email thread" object per group
#   3. Build compact descriptors (subject, participants, preview text)
#   4. Send ALL descriptors to the LLM in a single tool-call request
#   5. LLM returns: [{email_id, priority_score, label, reason, tasks, ...}]
#   6. Merge LLM scores back with metadata → sorted ranked list
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional

from intelligence.llm import llm_tool_call
from intelligence.summariser import _cutoff_iso, _participants, _scan

logger = logging.getLogger(__name__)

_MAX_THREADS_TO_RANK = 30   # cap so the tool-call payload doesn't exceed context
_PREVIEW_CHARS       = 600  # chars of thread text sent to LLM per email


# ── Tool schema (OpenAI function-calling format) ──────────────────────────────

_RANK_TOOL = {
    "type": "function",
    "function": {
        "name":        "rank_priority_emails",
        "description": (
            "Analyse a list of email threads and return them ranked by priority. "
            "Score each thread 1–10 where 10 = most critical/urgent."
        ),
        "parameters": {
            "type":     "object",
            "required": ["ranked_emails"],
            "properties": {
                "ranked_emails": {
                    "type":        "array",
                    "description": "All email threads, ranked highest priority first.",
                    "items": {
                        "type":     "object",
                        "required": [
                            "email_id", "priority_score", "priority_label",
                            "reason", "has_deadline", "action_required", "extracted_tasks"
                        ],
                        "properties": {
                            "email_id": {
                                "type": "string",
                                "description": "Exact email_id value from the input."
                            },
                            "priority_score": {
                                "type":        "integer",
                                "minimum":     1,
                                "maximum":     10,
                                "description": "Priority 1 (low) to 10 (critical)."
                            },
                            "priority_label": {
                                "type": "string",
                                "enum": ["critical", "high", "medium", "low"],
                                "description": "Human label matching the score band."
                            },
                            "reason": {
                                "type":        "string",
                                "description": "1–2 sentence explanation for this priority score."
                            },
                            "urgency_signals": {
                                "type":  "array",
                                "items": {"type": "string"},
                                "description": "Specific phrases that indicate urgency (e.g. 'EOD', 'ASAP')."
                            },
                            "has_deadline": {
                                "type":        "boolean",
                                "description": "True if the thread mentions a specific deadline or date."
                            },
                            "deadline_text": {
                                "type":        "string",
                                "description": "The deadline text extracted verbatim, or empty string."
                            },
                            "action_required": {
                                "type":        "boolean",
                                "description": "True if you (the recipient) need to do something."
                            },
                            "extracted_tasks": {
                                "type":  "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Specific action items from this thread. "
                                    "Each item should name what needs doing and who should do it."
                                )
                            },
                        },
                    },
                },
            },
        },
    },
}


# ── Public API ────────────────────────────────────────────────────────────────

async def get_priority_emails(
    store,
    hours: int  = 48,
    top_n: int  = 10,
    user_context: Optional[str] = None,
) -> dict:
    """
    Returns the top `top_n` priority email threads from the last `hours`.

    `user_context` — optional string describing who the user is / their role,
    used to make the priority scoring more relevant (e.g. "engineering manager").

    Response shape:
    {
      "period_hours": 48,
      "generated_at": "<iso>",
      "total_threads_scanned": N,
      "ranked_emails": [
        {
          "email_id":        "...",
          "subject":         "Email: Q3 Budget",
          "participants":    ["alice@...", "bob@..."],
          "latest_at":       "<iso>",
          "message_count":   3,
          "priority_score":  9,
          "priority_label":  "critical",
          "reason":          "...",
          "urgency_signals": ["ASAP", "end of day"],
          "has_deadline":    true,
          "deadline_text":   "by Friday 5pm",
          "action_required": true,
          "extracted_tasks": ["Review the draft proposal → Alice"],
        }, ...
      ]
    }
    """
    cutoff = _cutoff_iso(hours)
    rows   = _scan(store, f"platform = 'outlook' AND sent_at >= '{cutoff}'", limit=500)

    if not rows:
        return {
            "period_hours":          hours,
            "generated_at":          datetime.now(timezone.utc).isoformat(),
            "total_threads_scanned": 0,
            "ranked_emails":         [],
            "message":               f"No Outlook emails indexed in the last {hours} hours.",
        }

    groups = _group_by_thread(rows)
    logger.info(f"priority: {len(rows)} chunks → {len(groups)} threads")

    # Build compact thread descriptors for the LLM
    thread_meta: Dict[str, dict] = {}
    descriptors: List[dict]      = []

    for thread_id, thread_rows in list(groups.items())[:_MAX_THREADS_TO_RANK]:
        meta = {
            "thread_id":     thread_id,
            "subject":       _label_field(thread_rows, "channel_name"),
            "participants":  _participants(thread_rows),
            "latest_at":     _latest_at(thread_rows),
            "message_count": len(thread_rows),
        }
        thread_meta[thread_id] = meta

        preview = _build_preview(thread_rows, _PREVIEW_CHARS)
        descriptors.append({
            "email_id":     thread_id,
            "subject":      meta["subject"],
            "participants": ", ".join(meta["participants"][:5]),
            "sent_at":      meta["latest_at"],
            "preview":      preview,
        })

    if not descriptors:
        return {
            "period_hours":          hours,
            "generated_at":          datetime.now(timezone.utc).isoformat(),
            "total_threads_scanned": 0,
            "ranked_emails":         [],
        }

    # ── Tool call ─────────────────────────────────────────────────────────────
    role_context = f"User context: {user_context}\n\n" if user_context else ""
    system = (
        "You are an email priority analyst. Rank the provided email threads by urgency and importance. "
        "Consider: explicit deadlines, escalation language (ASAP/urgent/critical), executive senders, "
        "customer impact, blocking dependencies, and direct action requests to the recipient.\n\n"
        "Score 8-10 = critical/high (needs attention today), "
        "5-7 = medium (this week), 1-4 = low (can wait).\n\n"
        f"{role_context}"
        "You MUST call the rank_priority_emails tool with ALL emails provided — do not skip any."
    )

    user_msg = (
        f"Please rank these {len(descriptors)} email threads by priority:\n\n"
        + json.dumps(descriptors, indent=2)
    )

    logger.info(f"priority: calling LLM tool with {len(descriptors)} threads")

    result = await llm_tool_call(
        messages    = [{"role": "user", "content": user_msg}],
        tools       = [_RANK_TOOL],
        tool_choice = {"type": "function", "function": {"name": "rank_priority_emails"}},
        system      = system,
    )

    if not result:
        return _error_response(hours, thread_meta, "LLM tool call returned no result")

    ranked_raw = result.get("ranked_emails", [])

    # Merge LLM output with our stored metadata
    merged = []
    for item in ranked_raw:
        eid  = item.get("email_id", "")
        meta = thread_meta.get(eid, {})
        merged.append({
            # Our metadata
            "email_id":        eid,
            "subject":         meta.get("subject", item.get("subject", "")),
            "participants":    meta.get("participants", []),
            "latest_at":       meta.get("latest_at", ""),
            "message_count":   meta.get("message_count", 0),
            # LLM output
            "priority_score":  item.get("priority_score", 5),
            "priority_label":  item.get("priority_label", "medium"),
            "reason":          item.get("reason", ""),
            "urgency_signals": item.get("urgency_signals", []),
            "has_deadline":    item.get("has_deadline", False),
            "deadline_text":   item.get("deadline_text", ""),
            "action_required": item.get("action_required", False),
            "extracted_tasks": item.get("extracted_tasks", []),
        })

    # Sort by priority_score descending
    merged.sort(key=lambda e: e["priority_score"], reverse=True)

    return {
        "period_hours":          hours,
        "generated_at":          datetime.now(timezone.utc).isoformat(),
        "total_threads_scanned": len(groups),
        "ranked_emails":         merged[:top_n],
    }


# ── Internal helpers ──────────────────────────────────────────────────────────

def _group_by_thread(rows: List[dict]) -> Dict[str, List[dict]]:
    groups: Dict[str, List[dict]] = defaultdict(list)
    for r in rows:
        key = r.get("thread_id") or r.get("file_id", "unknown")
        groups[key].append(r)
    return dict(groups)


def _build_preview(rows: List[dict], max_chars: int) -> str:
    sorted_rows = sorted(rows, key=lambda r: r.get("sent_at", ""))
    buf, total = [], 0
    for r in sorted_rows:
        text = (r.get("text") or "").strip()
        if not text:
            continue
        remaining = max_chars - total
        if remaining <= 0:
            break
        snippet = text[:remaining]
        buf.append(snippet)
        total += len(snippet)
    return "\n\n".join(buf)


def _label_field(rows: List[dict], field: str) -> str:
    for r in rows:
        val = r.get(field, "")
        if val:
            return str(val)
    return "Unknown"


def _latest_at(rows: List[dict]) -> str:
    dates = [r.get("sent_at", "") for r in rows if r.get("sent_at")]
    return max(dates) if dates else ""


def _error_response(hours: int, thread_meta: dict, message: str) -> dict:
    logger.error(f"priority error: {message}")
    return {
        "period_hours":          hours,
        "generated_at":          datetime.now(timezone.utc).isoformat(),
        "total_threads_scanned": len(thread_meta),
        "ranked_emails":         [],
        "error":                 message,
    }
