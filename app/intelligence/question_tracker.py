# intelligence/question_tracker.py
# Finds questions asked in Teams channels that haven't received a clear answer.
#
# Strategy — two-phase hybrid:
#
# Phase 1 — Regex (fast, cheap):
#   Parse individual message turns from chunk text (format: "[Name | ts] content")
#   Flag turns that end in "?" OR start with a question word.
#   Group candidates by channel.
#
# Phase 2 — LLM tool call (accurate, enriched):
#   For each candidate question, send surrounding context (the turns before +
#   after) to the LLM.  The tool forces the model to classify:
#     is_genuine_question, is_answered, answer_summary, topic, urgency
#   This eliminates false positives ("Ready?" "Good?") and finds rhetorical ?s.
#
# Why tool calling here?
#   Each question needs a structured classification object — not prose.
#   Tool calling guarantees all fields are present and correctly typed,
#   which lets the API route build a reliable filtered/sorted response.
from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from intelligence.llm import llm_tool_call
from intelligence.summariser import _scan

logger = logging.getLogger(__name__)

# Regex patterns for question detection in turn text
_QUESTION_WORD_RE = re.compile(
    r"^\s*(who|what|when|where|why|how|does|did|is|are|isn't|aren't|can|could|"
    r"would|should|has|have|will|won't|shall|do|don't)\b",
    re.IGNORECASE,
)
_ENDS_QUESTION_RE = re.compile(r"\?\s*$")

# Turn line format written by chat_indexer.py
_TURN_RE = re.compile(
    r"\[([^\|]+?)\s*\|\s*(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}[^\]]*)\]\s*(.*)",
    re.DOTALL,
)

# Max questions to send to LLM for enrichment in one batch
_BATCH_SIZE        = 15
# Context window: how many turns before/after the question to include
_CONTEXT_TURNS     = 3
# LLM enrichment token budget
_ENRICH_MAX_TOKENS = 2000


# ── Tool schema ───────────────────────────────────────────────────────────────

_CLASSIFY_TOOL = {
    "type": "function",
    "function": {
        "name":        "classify_questions",
        "description": "Classify each candidate question from a Teams channel.",
        "parameters": {
            "type":     "object",
            "required": ["questions"],
            "properties": {
                "questions": {
                    "type":  "array",
                    "items": {
                        "type":     "object",
                        "required": [
                            "question_id", "is_genuine_question",
                            "is_answered", "topic", "urgency"
                        ],
                        "properties": {
                            "question_id": {
                                "type":        "string",
                                "description": "Exact question_id from the input."
                            },
                            "is_genuine_question": {
                                "type":        "boolean",
                                "description": (
                                    "True only if this is a real question that someone "
                                    "expects an answer to. False for rhetorical, greetings, confirmations."
                                )
                            },
                            "is_answered": {
                                "type":        "boolean",
                                "description": (
                                    "True if a subsequent message in the context "
                                    "clearly answers the question."
                                )
                            },
                            "answer_summary": {
                                "type":        "string",
                                "description": "Brief summary of the answer if is_answered=true, else empty string."
                            },
                            "topic": {
                                "type":        "string",
                                "description": "2–4 word topic tag (e.g. 'deployment schedule', 'budget approval')."
                            },
                            "urgency": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                                "description": (
                                    "high = blocking / time-sensitive, "
                                    "medium = important but not urgent, "
                                    "low = general / informational."
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

async def get_unanswered_questions(
    store,
    channel_filter: Optional[str] = None,
    hours:          Optional[int]  = None,
    unanswered_only: bool          = True,
) -> dict:
    """
    Returns questions asked in Teams channels, classified by whether they
    received an answer.

    `channel_filter`  — optional substring to filter channel names.
    `hours`           — if set, only look at messages from the last N hours.
    `unanswered_only` — if True (default), only return unresolved questions.

    Response shape:
    {
      "generated_at": "<iso>",
      "total_scanned": N,
      "unanswered_count": M,
      "channels": {
        "Engineering › General": [
          {
            "question_id":  "...",
            "asker":        "Alice",
            "asked_at":     "2025-03-10T09:14",
            "question":     "What's the status of the auth service?",
            "channel":      "Engineering › General",
            "topic":        "auth service status",
            "urgency":      "high",
            "is_answered":  false,
            "answer_summary": "",
            "context":      "[Bob | ...] ..."    ← surrounding turns
          }, ...
        ]
      }
    }
    """
    parts = ["platform = 'teams'"]
    if hours:
        from intelligence.summariser import _cutoff_iso
        parts.append(f"sent_at >= '{_cutoff_iso(hours)}'")
    where = " AND ".join(parts)

    rows = _scan(store, where, limit=500)
    if not rows:
        return {
            "generated_at":    datetime.now(timezone.utc).isoformat(),
            "total_scanned":   0,
            "unanswered_count": 0,
            "channels":        {},
            "message":         "No Teams messages indexed.",
        }

    # Phase 1 — extract candidate questions from chunk turns
    candidates = _extract_candidates(rows, channel_filter)
    logger.info(f"question_tracker: {len(rows)} chunks → {len(candidates)} candidates")

    if not candidates:
        return {
            "generated_at":    datetime.now(timezone.utc).isoformat(),
            "total_scanned":   len(rows),
            "unanswered_count": 0,
            "channels":        {},
            "message":         "No questions detected in the indexed messages.",
        }

    # Phase 2 — LLM enrichment in batches
    enriched = await _enrich_batched(candidates)

    # Filter and organise
    by_channel: Dict[str, List[dict]] = defaultdict(list)
    unanswered_count = 0

    for item in enriched:
        if not item.get("is_genuine_question"):
            continue
        if unanswered_only and item.get("is_answered"):
            continue

        channel = item.get("channel", "Unknown")
        by_channel[channel].append(item)
        if not item.get("is_answered"):
            unanswered_count += 1

    # Sort each channel: high urgency first, then by asked_at descending
    _urgency_rank = {"high": 0, "medium": 1, "low": 2}
    for ch in by_channel:
        by_channel[ch].sort(
            key=lambda q: (
                _urgency_rank.get(q.get("urgency", "low"), 2),
                -(q.get("asked_at") or ""),
            )
        )

    return {
        "generated_at":    datetime.now(timezone.utc).isoformat(),
        "total_scanned":   len(rows),
        "unanswered_count": unanswered_count,
        "channels":        dict(by_channel),
    }


# ── Phase 1: Candidate extraction ─────────────────────────────────────────────

def _extract_candidates(rows: List[dict], channel_filter: Optional[str]) -> List[dict]:
    """
    Parse every chunk's text into individual turns.
    Flag turns that look like questions.
    Return list of candidate dicts with surrounding context.
    """
    # Group rows by channel so we can build context windows
    channel_chunks: Dict[str, List[dict]] = defaultdict(list)
    for r in rows:
        channel = (r.get("section_heading") or "Unknown").strip()
        if channel_filter and channel_filter.lower() not in channel.lower():
            continue
        channel_chunks[channel].append(r)

    candidates = []
    cand_id    = 0

    for channel, ch_rows in channel_chunks.items():
        # Sort chunks chronologically within this channel
        ch_rows.sort(key=lambda r: r.get("sent_at", ""))

        # Flatten into a list of (name, timestamp, content) turns
        all_turns: List[Tuple[str, str, str]] = []
        for r in ch_rows:
            turns = _parse_turns(r.get("text", ""))
            all_turns.extend(turns)

        for i, (name, ts, content) in enumerate(all_turns):
            if not _is_question(content):
                continue

            # Context: _CONTEXT_TURNS before and after
            ctx_start = max(0, i - _CONTEXT_TURNS)
            ctx_end   = min(len(all_turns), i + _CONTEXT_TURNS + 1)
            context_turns = all_turns[ctx_start:ctx_end]
            context_text  = "\n".join(
                f"[{n} | {t}] {c}" for n, t, c in context_turns
            )

            candidates.append({
                "question_id": f"q{cand_id}",
                "asker":       name.strip(),
                "asked_at":    ts.strip(),
                "question":    content.strip(),
                "channel":     channel,
                "context":     context_text,
                # Placeholder fields filled in Phase 2
                "is_genuine_question": True,
                "is_answered":         False,
                "answer_summary":      "",
                "topic":               "",
                "urgency":             "medium",
            })
            cand_id += 1

    return candidates


def _parse_turns(chunk_text: str) -> List[Tuple[str, str, str]]:
    """Parse '[Name | ts] content' lines from a chunk into (name, ts, content) tuples."""
    turns = []
    for match in _TURN_RE.finditer(chunk_text):
        name    = match.group(1).strip()
        ts      = match.group(2).strip()
        content = match.group(3).strip()
        if content:
            turns.append((name, ts, content))
    return turns


def _is_question(text: str) -> bool:
    """Return True if the text looks like a genuine question."""
    text = text.strip()
    if not text:
        return False
    # Must end with ? OR start with a question word (but not be too short to be meaningful)
    ends_q  = bool(_ENDS_QUESTION_RE.search(text))
    starts_q = bool(_QUESTION_WORD_RE.match(text))
    too_short = len(text.split()) < 4
    return (ends_q or starts_q) and not too_short


# ── Phase 2: LLM enrichment ───────────────────────────────────────────────────

async def _enrich_batched(candidates: List[dict]) -> List[dict]:
    """Send candidates to LLM in batches of _BATCH_SIZE, merge results back."""
    all_enriched: List[dict] = []
    # Keep a lookup so we can merge by question_id
    lookup = {c["question_id"]: c for c in candidates}

    for batch_start in range(0, len(candidates), _BATCH_SIZE):
        batch = candidates[batch_start: batch_start + _BATCH_SIZE]
        enriched_batch = await _enrich_batch(batch, lookup)
        all_enriched.extend(enriched_batch)

    return all_enriched


async def _enrich_batch(batch: List[dict], lookup: Dict[str, dict]) -> List[dict]:
    """Run one LLM tool call for a single batch of candidates."""
    # Build the payload for the LLM — include question + context
    payload = [
        {
            "question_id": c["question_id"],
            "asker":       c["asker"],
            "asked_at":    c["asked_at"],
            "question":    c["question"],
            "channel":     c["channel"],
            "context":     c["context"],
        }
        for c in batch
    ]

    system = (
        "You are a workplace communication analyst reviewing Microsoft Teams messages. "
        "For each candidate question, determine:\n"
        "1. Is it a genuine question (not rhetorical, not a greeting)?\n"
        "2. Is it answered within the provided context?\n"
        "3. What topic does it relate to?\n"
        "4. How urgent is it?\n\n"
        "You MUST call the classify_questions tool with ALL provided question_ids."
    )

    user_msg = (
        f"Classify these {len(payload)} candidate questions from Teams:\n\n"
        + json.dumps(payload, indent=2)
    )

    try:
        result = await llm_tool_call(
            messages    = [{"role": "user", "content": user_msg}],
            tools       = [_CLASSIFY_TOOL],
            tool_choice = {"type": "function", "function": {"name": "classify_questions"}},
            system      = system,
            max_tokens  = _ENRICH_MAX_TOKENS,
        )
    except Exception as e:
        logger.error(f"LLM enrich batch failed: {e}")
        # Return candidates unmodified so the response is still useful
        return batch

    if not result:
        return batch

    # Merge LLM classifications back into our candidate objects
    enriched = []
    for item in result.get("questions", []):
        qid  = item.get("question_id", "")
        base = lookup.get(qid)
        if not base:
            continue
        enriched.append({
            **base,
            "is_genuine_question": item.get("is_genuine_question", True),
            "is_answered":         item.get("is_answered", False),
            "answer_summary":      item.get("answer_summary", ""),
            "topic":               item.get("topic", ""),
            "urgency":             item.get("urgency", "medium"),
        })

    # Include any candidates the LLM silently dropped (shouldn't happen with tool_choice=required)
    llm_ids = {item.get("question_id") for item in result.get("questions", [])}
    for c in batch:
        if c["question_id"] not in llm_ids:
            logger.warning(f"LLM skipped question_id={c['question_id']}")
            enriched.append(c)

    return enriched
