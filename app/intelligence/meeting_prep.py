# intelligence/meeting_prep.py
# Read-only meeting-prep synthesis.
#
# For each upcoming Outlook calendar meeting (fetched LIVE from Microsoft Graph),
# this module retrieves related context from the LanceDB `chunks` table that the
# Outlook/Teams connectors have already ingested, then synthesises a structured
# "prep card" per meeting via a forced LLM tool call.
#
# Pipeline (mirrors the architecture in the handoff brief):
#   1. CalendarConnector.get_events(start, end)            [live Graph read]
#   2. For each event, bounded-concurrency (Semaphore(5)):
#        a. retrieve_context(event, store, embedder)       [LanceDB read]
#             · structured participant-overlap filter (attendees ↔ participants)
#             · vector search on subject+body (only when an embedder is supplied)
#             · merge + dedupe by (platform, thread_id), keep newest per thread
#             · rank by recency + participant overlap + vector similarity
#        b. synthesize_prep(event, threads)                [LLM tool call]
#             · skipped entirely when no context → no_context card
#   3. assemble the §5 response dict
#
# This module is strictly observe-only. It performs NO calendar / email / Teams
# writes and persists nothing to LanceDB or sqlite. It mirrors the posture of
# intelligence/priority.py and intelligence/summariser.py.
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from connectors.graph_users import get_signed_in_user_email, resolve_emails_to_uids
from intelligence.llm import llm_tool_call

logger = logging.getLogger(__name__)

# ── Tunables ────────────────────────────────────────────────────────────────
_CONTEXT_WINDOW_DAYS   = 60     # ignore ingested chunks older than this
_MAX_FILTER_TERMS      = 20     # cap on OR terms in the participant filter
_STRUCTURED_SCAN_LIMIT = 80     # rows pulled by the participant-overlap filter
_VECTOR_TOP_K          = 30     # candidates pulled by vector search
_VECTOR_MIN_SIM        = 0.30   # min cosine-like similarity to count as a hit
_EXCERPT_CHARS         = 500    # per-thread text excerpt fed to the LLM
_CONCURRENCY           = 5      # bounded parallel per-meeting LLM calls

_NO_CONTEXT_SUMMARY = "No prior context found in the ingested corpus."


# ── Per-meeting tool schema (OpenAI function-calling format) ──────────────────
# The LLM produces ONLY the analysis fields. Hard facts (event metadata, thread
# ids, last_activity, participant overlap) come from Graph + retrieval and are
# merged in afterwards — the model never invents thread ids or timestamps.
_PREP_TOOL = {
    "type": "function",
    "function": {
        "name":        "build_meeting_prep",
        "description": (
            "Given an upcoming meeting and a set of related prior conversation "
            "threads retrieved from the user's email/Teams corpus, produce a "
            "concise preparation card. Base every statement ONLY on the supplied "
            "threads — do not invent context."
        ),
        "parameters": {
            "type":     "object",
            "required": [
                "summary", "thread_notes", "open_questions",
                "decisions_needed", "suggested_talking_points", "confidence",
            ],
            "properties": {
                "summary": {
                    "type":        "string",
                    "description": (
                        "1–2 sentence framing of what this meeting is about given "
                        "the prior context."
                    ),
                },
                "thread_notes": {
                    "type":        "array",
                    "description": (
                        "One entry per related thread you reference. Use the EXACT "
                        "thread_id value from the input."
                    ),
                    "items": {
                        "type":     "object",
                        "required": ["thread_id", "one_line"],
                        "properties": {
                            "thread_id": {
                                "type":        "string",
                                "description": "Exact thread_id from the input threads.",
                            },
                            "one_line": {
                                "type":        "string",
                                "description": "Short description of this thread's relevance.",
                            },
                        },
                    },
                },
                "open_questions": {
                    "type":  "array",
                    "items": {"type": "string"},
                    "description": "Unresolved questions relevant to this meeting.",
                },
                "decisions_needed": {
                    "type":  "array",
                    "items": {"type": "string"},
                    "description": "Decisions that likely need to be made in/around this meeting.",
                },
                "suggested_talking_points": {
                    "type":  "array",
                    "items": {"type": "string"},
                    "description": (
                        "Points worth raising. For human reading only — these are "
                        "NOT action proposals."
                    ),
                },
                "confidence": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                    "description": "How well the retrieved context covers this meeting.",
                },
            },
        },
    },
}


# ── Public API ────────────────────────────────────────────────────────────────

async def get_meeting_prep(
    store,
    calendar,
    hours_ahead:         int           = 24,
    top_n:               int           = 5,
    context_per_meeting: int           = 5,
    user_context:        Optional[str] = None,
    embedder=None,
    user_id:             str           = "default",
) -> dict:
    """
    Build prep cards for upcoming meetings.

    `store`     — LanceDBStore (read-only here).
    `calendar`  — CalendarConnector (live Graph reads).
    `embedder`  — optional OpenRouterEmbedder. When None, vector search is
                  skipped and retrieval relies on the participant-overlap filter
                  alone (this is what the offline test suite exercises).

    Returns the §5 response dict: {window_start, window_end, meetings: [...]}.
    """
    now           = datetime.now(timezone.utc)
    window_start  = now
    window_end    = now + timedelta(hours=hours_ahead)
    start_iso     = _iso_z(window_start)
    end_iso       = _iso_z(window_end)

    events = await calendar.get_events(start_iso, end_iso, user_id=user_id)
    events = events[:top_n]

    if not events:
        return {
            "window_start": start_iso,
            "window_end":   end_iso,
            "meetings":     [],
        }

    # Resolve the signed-in user's own email ONCE per request so it can be
    # stripped from every meeting's attendee list. Falls back to a user_id that
    # already looks like an email, then to None (no strip) — never fails.
    signed_in_email = await get_signed_in_user_email(user_id)
    own_email = signed_in_email or (
        (user_id or "").strip().lower() if "@" in (user_id or "") else None
    )

    cutoff = _iso_z(now - timedelta(days=_CONTEXT_WINDOW_DAYS))

    # Pre-resolve every attendee email across ALL meetings into one shared cache
    # before the concurrent per-meeting fan-out. This makes the email→UID lookup
    # exactly-once per address request-wide (avoiding a cache race under gather)
    # so the structured filter can match Teams chunks (which store Graph UIDs).
    request_cache: Dict[str, Optional[str]] = {}
    all_emails: set = set()
    for ev in events:
        all_emails |= _attendee_emails(ev, own_email)
    if all_emails:
        await resolve_emails_to_uids(sorted(all_emails), cache=request_cache, user_id=user_id)

    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _prep_one(event: dict) -> dict:
        async with sem:
            threads = await retrieve_context(
                event,
                store,
                embedder            = embedder,
                context_per_meeting = context_per_meeting,
                cutoff_iso          = cutoff,
                own_email           = own_email,
                resolve_cache       = request_cache,
                user_id             = user_id,
            )
            return await synthesize_prep(event, threads, user_context=user_context)

    meetings = await asyncio.gather(*[_prep_one(ev) for ev in events])

    return {
        "window_start": start_iso,
        "window_end":   end_iso,
        "meetings":     list(meetings),
    }


# ── Retrieval ───────────────────────────────────────────────────────────────

async def retrieve_context(
    event:               dict,
    store,
    embedder=None,
    context_per_meeting: int           = 5,
    cutoff_iso:          Optional[str] = None,
    own_email:           Optional[str] = None,
    resolve_cache:       Optional[dict] = None,
    user_id:             str           = "default",
) -> List[dict]:
    """
    Retrieve the most relevant prior conversation threads for one meeting.

    Returns a ranked list (length ≤ context_per_meeting) of CandidateThread dicts:
      {
        "platform":             "outlook" | "teams",
        "thread_id":            "...",
        "last_activity":        "<iso>",
        "participants_overlap": ["alice@...", ...],
        "excerpt":              "<= 500 chars of representative text",
        "score":                <float>,
      }
    Empty list → the no_context path.
    """
    attendee_emails = _attendee_emails(event, own_email)

    # Resolve attendee emails → Graph UIDs so the structured filter can also
    # match Teams chunks (which store Graph UIDs in `participants`, not emails).
    # Cache-backed + degrades to email-only when resolution is unavailable.
    uid_map = (
        await resolve_emails_to_uids(
            sorted(attendee_emails), cache=resolve_cache, user_id=user_id
        )
        if attendee_emails else {}
    )
    resolved_uids = [u for u in uid_map.values() if u]
    # Identity set for overlap scoring spans both forms (email + UID).
    attendee_identities = attendee_emails | set(resolved_uids)

    # Filter terms: emails first, then UIDs, capped — emails win at the boundary.
    filter_terms = list(sorted(attendee_emails)) + resolved_uids
    filter_terms = filter_terms[:_MAX_FILTER_TERMS]

    # Collect candidate chunk rows from two independent retrieval legs.
    rows_by_chunk: Dict[str, dict] = {}

    # Leg 1 — structured participant-overlap filter (emails + resolved UIDs).
    if filter_terms:
        where = _build_participant_where(filter_terms, cutoff_iso)
        for r in _scan(store, where, _STRUCTURED_SCAN_LIMIT):
            cid = r.get("chunk_id") or f"{r.get('thread_id')}::{r.get('chunk_index')}"
            r.setdefault("_similarity", 0.0)
            rows_by_chunk[cid] = r

    # Leg 2 — vector search on subject + body (only when an embedder is present).
    if embedder is not None:
        query = f"{event.get('subject', '')} {event.get('body_preview', '')}".strip()
        if query:
            for r in await _vector_search(store, embedder, query, cutoff_iso):
                sim = r.get("_similarity", 0.0)
                cid = r.get("chunk_id") or f"{r.get('thread_id')}::{r.get('chunk_index')}"
                if cid in rows_by_chunk:
                    rows_by_chunk[cid]["_similarity"] = max(
                        rows_by_chunk[cid].get("_similarity", 0.0), sim
                    )
                else:
                    rows_by_chunk[cid] = r

    if not rows_by_chunk:
        return []

    # Dedupe by (platform, thread_id) — keep the most recent chunk per thread,
    # but track the best similarity and newest activity across the thread's chunks.
    threads: Dict[tuple, dict] = {}
    for r in rows_by_chunk.values():
        platform  = r.get("platform", "") or "outlook"
        thread_id = r.get("thread_id") or r.get("file_id", "unknown")
        key       = (platform, thread_id)
        sent_at   = r.get("sent_at", "") or ""
        sim       = float(r.get("_similarity", 0.0) or 0.0)

        cur = threads.get(key)
        if cur is None:
            threads[key] = {
                "platform":      platform,
                "thread_id":     thread_id,
                "last_activity": sent_at,
                "best_sim":      sim,
                "_newest_row":   r,
                "_participants": _row_participants(r),
            }
        else:
            cur["best_sim"] = max(cur["best_sim"], sim)
            cur["_participants"].update(_row_participants(r))
            if sent_at > cur["last_activity"]:
                cur["last_activity"] = sent_at
                cur["_newest_row"]   = r

    # Score and rank: recency + participant-overlap count + vector similarity.
    candidates: List[dict] = []
    for t in threads.values():
        overlap = sorted(t["_participants"] & attendee_identities) if attendee_identities else []
        score   = (
            _recency_score(t["last_activity"])
            + len(overlap) * 1.0
            + t["best_sim"] * 2.0
        )
        excerpt = (t["_newest_row"].get("text") or "").strip()[:_EXCERPT_CHARS]
        candidates.append({
            "platform":             t["platform"],
            "thread_id":            t["thread_id"],
            "last_activity":        t["last_activity"],
            "participants_overlap": overlap,
            "excerpt":              excerpt,
            "score":                round(score, 4),
        })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates[:context_per_meeting]


# ── Synthesis ───────────────────────────────────────────────────────────────

async def synthesize_prep(
    event:        dict,
    threads:      List[dict],
    user_context: Optional[str] = None,
) -> dict:
    """
    Assemble one meeting card (§5 shape). When `threads` is empty, build the
    no_context card directly WITHOUT calling the LLM — saving a round-trip and
    avoiding hallucinated context.
    """
    base = _event_card_base(event)

    if not threads:
        base["prep"] = {
            "summary":                  _NO_CONTEXT_SUMMARY,
            "recent_threads":           [],
            "open_questions":           [],
            "decisions_needed":         [],
            "suggested_talking_points": [],
        }
        base["confidence"] = "no_context"
        return base

    # ── Forced tool call: analysis fields only ────────────────────────────────
    role_context = f"User context: {user_context}\n\n" if user_context else ""
    system = (
        "You are a meeting-prep analyst. You are given an upcoming meeting and a "
        "set of related prior conversation threads from the user's email/Teams "
        "corpus. Produce a concise, factual prep card grounded ONLY in the "
        "supplied threads — never invent context, attendees, or decisions.\n\n"
        "Suggested talking points are for the user to read before the meeting; "
        "they are NOT actions to be sent or executed.\n\n"
        f"{role_context}"
        "You MUST call the build_meeting_prep tool."
    )

    llm_threads = [
        {
            "thread_id":            t["thread_id"],
            "platform":             t["platform"],
            "last_activity":        t["last_activity"],
            "participants_overlap": t["participants_overlap"],
            "excerpt":              t["excerpt"],
        }
        for t in threads
    ]
    user_msg = (
        "Meeting:\n"
        + json.dumps(
            {
                "subject":      event.get("subject", ""),
                "start":        event.get("start", ""),
                "end":          event.get("end", ""),
                "location":     event.get("location", ""),
                "body_preview": event.get("body_preview", ""),
                "attendees":    [a.get("email", "") for a in event.get("attendees", [])],
            },
            separators=(",", ":"),
        )
        + "\n\nRelated threads:\n"
        + json.dumps(llm_threads, separators=(",", ":"))
    )

    result = None
    try:
        result = await llm_tool_call(
            messages    = [{"role": "user", "content": user_msg}],
            tools       = [_PREP_TOOL],
            tool_choice = "required",
            system      = system,
        )
    except Exception as e:
        logger.error(f"meeting_prep synthesis failed for {event.get('event_id')}: {e}",
                     exc_info=True)

    if not result:
        # LLM unavailable / returned nothing: degrade to a low-confidence card
        # built purely from retrieval facts (still no hallucinated analysis).
        base["prep"] = {
            "summary":                  "Related prior context found; automatic summary unavailable.",
            "recent_threads":           _facts_only_threads(threads),
            "open_questions":           [],
            "decisions_needed":         [],
            "suggested_talking_points": [],
        }
        base["confidence"] = "low"
        return base

    # Merge LLM analysis with retrieval facts. one_line comes from the LLM keyed
    # by thread_id; everything else is fact from retrieval.
    one_lines = {
        n.get("thread_id", ""): n.get("one_line", "")
        for n in (result.get("thread_notes") or [])
    }
    recent_threads = [
        {
            "platform":             t["platform"],
            "thread_id":            t["thread_id"],
            "one_line":             one_lines.get(t["thread_id"], ""),
            "last_activity":        t["last_activity"],
            "participants_overlap": t["participants_overlap"],
        }
        for t in threads
    ]

    confidence = result.get("confidence", "medium")
    if confidence not in ("high", "medium", "low"):
        confidence = "medium"

    base["prep"] = {
        "summary":                  result.get("summary", ""),
        "recent_threads":           recent_threads,
        "open_questions":           result.get("open_questions", []),
        "decisions_needed":         result.get("decisions_needed", []),
        "suggested_talking_points": result.get("suggested_talking_points", []),
    }
    base["confidence"] = confidence
    return base


# ── Internal helpers ──────────────────────────────────────────────────────────

def _event_card_base(event: dict) -> dict:
    """The Graph-sourced portion of a §5 meeting card (no prep/confidence yet)."""
    return {
        "event_id":  event.get("event_id", ""),
        "subject":   event.get("subject", ""),
        "start":     event.get("start", ""),
        "end":       event.get("end", ""),
        "location":  event.get("location", ""),
        "attendees": event.get("attendees", []),
        "organizer": event.get("organizer", {}),
        "is_online": bool(event.get("is_online", False)),
    }


def _facts_only_threads(threads: List[dict]) -> List[dict]:
    return [
        {
            "platform":             t["platform"],
            "thread_id":            t["thread_id"],
            "one_line":             "",
            "last_activity":        t["last_activity"],
            "participants_overlap": t["participants_overlap"],
        }
        for t in threads
    ]


def _attendee_emails(event: dict, own_email: Optional[str]) -> set:
    """Lowercased attendee + organizer emails, excluding the meeting owner."""
    emails = set()
    for a in event.get("attendees", []) or []:
        e = (a.get("email") or "").strip().lower()
        if e:
            emails.add(e)
    org = (event.get("organizer") or {}).get("email", "")
    if org:
        emails.add(org.strip().lower())
    if own_email:
        emails.discard(own_email)
    emails.discard("")
    return emails


def _build_participant_where(terms, cutoff_iso: Optional[str]) -> str:
    """
    Build a LanceDB where() clause matching chunks whose `participants` field
    overlaps any of `terms` (attendee emails and/or resolved Graph UIDs).

    `participants` is a JSON-encoded *string* (e.g. '["a@co.com","b@co.com"]'
    for Outlook, '["uid-1","uid-2"]' for Teams), so overlap is a substring
    (LIKE) match, not array-contains. Single quotes are escaped defensively.
    `terms` is assumed already capped/ordered by the caller.
    """
    clauses = [
        f"participants LIKE '%{_esc(t)}%'"
        for t in terms
    ]
    where = "(" + " OR ".join(clauses) + ")"
    where = f"platform != '' AND {where}"
    if cutoff_iso:
        where += f" AND sent_at >= '{cutoff_iso}'"
    return where


def _scan(store, where: str, limit: int) -> List[dict]:
    """Filtered table scan — no vector search. Mirrors summariser._scan."""
    try:
        return (
            store.chunks
            .search()
            .where(where, prefilter=True)
            .limit(limit)
            .to_list()
        )
    except Exception as e:
        logger.error(f"meeting_prep scan failed (where={where!r}): {e}")
        return []


async def _vector_search(
    store,
    embedder,
    query:      str,
    cutoff_iso: Optional[str],
) -> List[dict]:
    """
    Vector search composed directly on the chunks table (LanceDBStore does not
    expose a search helper). Restricts to ingested conversation chunks and the
    recency window. Annotates each row with a cosine-like `_similarity` in
    [0, 1] derived from `_distance`, and drops rows below _VECTOR_MIN_SIM.
    """
    try:
        vec   = await embedder.embed(query)
        where = "platform != ''"
        if cutoff_iso:
            where += f" AND sent_at >= '{cutoff_iso}'"
        rows = (
            store.chunks
            .search(vec.tolist())
            .where(where, prefilter=True)
            .limit(_VECTOR_TOP_K)
            .to_list()
        )
    except Exception as e:
        logger.error(f"meeting_prep vector search failed: {e}")
        return []

    out = []
    for r in rows:
        dist = r.get("_distance")
        sim  = max(0.0, 1.0 - float(dist)) if dist is not None else 0.0
        if sim < _VECTOR_MIN_SIM:
            continue
        r["_similarity"] = sim
        out.append(r)
    return out


def _row_participants(row: dict) -> set:
    """Parse the JSON-string `participants` field into a lowercased set."""
    raw = row.get("participants", "") or ""
    try:
        vals = json.loads(raw) if raw else []
    except Exception:
        vals = [raw] if raw else []
    return {str(v).strip().lower() for v in vals if v}


def _recency_score(sent_at: str) -> float:
    """
    Map an ISO timestamp to a [0, 1] recency score (newer = higher). Uses a
    60-day linear decay aligned with the context window. Unparseable → 0.
    """
    if not sent_at:
        return 0.0
    try:
        ts = datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except Exception:
        return 0.0
    age_days = (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0
    return max(0.0, 1.0 - age_days / _CONTEXT_WINDOW_DAYS)


def _esc(value: str) -> str:
    """Escape single quotes for embedding inside a SQL-ish LIKE literal."""
    return value.replace("'", "''")


def _iso_z(dt: datetime) -> str:
    """UTC datetime → 'YYYY-MM-DDTHH:MM:SSZ' (Graph-friendly, no microseconds)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
