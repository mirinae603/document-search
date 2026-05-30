# intelligence/calendar_priority.py
# Ranks upcoming Outlook calendar meetings by importance for a fast morning
# triage view (GET /calendar/priority). The calendar twin of
# intelligence/priority.py — same posture: ONE forced LLM tool call across ALL
# meetings (never a per-meeting fan-out), a top-N sort, and user_context plumbing.
#
# Deliberately NOT corpus-aware. This endpoint ranks; /calendar/prep
# contextualises against the ingested email/Teams corpus. No LanceDB is touched
# here — the store is never imported, queried, or passed through.
#
# Pipeline:
#   1. CalendarConnector.get_events(start, end)          [live Graph read]
#   2. Identify the signed-in user ONCE (get_signed_in_user_email). None ⇒
#      degraded self-signals (is_external / attendance_required / response_status
#      become null) but the endpoint still works.
#   3. Deterministic pre-processing per event — conflicts, externality,
#      attendance-required, response-status, organizer flag, attendee count,
#      duration. These are computed IN CODE; the LLM only consumes them.
#   4. ONE llm_tool_call (tool_choice="required") with every meeting + its
#      precomputed signals.
#   5. Merge the LLM ranking with each event's metadata + deterministic signals.
#   6. Sort by priority_score desc, cap at top_n.
#
# Zero events ⇒ return {window_*, meetings: []} WITHOUT calling the LLM (mirrors
# the no_context short-circuit in /calendar/prep).
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from connectors.graph_users import get_signed_in_user_email
from intelligence.llm import llm_tool_call

logger = logging.getLogger(__name__)

# Controlled vocabulary for urgency_signals — the LLM must pick ONLY from this
# list (same discipline as priority.py's signal handling). Kept in code so the
# schema enum and the system-prompt instruction can never drift apart.
_URGENCY_VOCAB = [
    "decision_needed",
    "external_stakeholder",
    "senior_leadership",
    "client_facing",
    "large_attendee_count",
    "recurring_skipped",
    "tight_deadline_mentioned",
    "unprepared",
    "conflict_with_other_meeting",
    "optional_attendance",
    "declined_response_pending",
    "long_duration",
    "first_meeting_with_attendee",
]


# ── Tool schema (OpenAI function-calling format, forced) ──────────────────────
# The LLM produces ONLY the ranking/analysis fields. Hard facts (event metadata,
# conflicts, externality, attendance, response status) come from Graph + the
# deterministic pass and are merged in afterwards — the model never determines them.
_RANK_TOOL = {
    "type": "function",
    "function": {
        "name":        "rank_meetings",
        "description": (
            "Analyse a list of upcoming calendar meetings (each annotated with "
            "deterministic signals computed by the caller) and return them ranked "
            "by importance. Score each meeting 1-10 where 10 = most critical. "
            "Treat the supplied signals (has_conflict, is_external, "
            "attendance_required, response_status, attendee_count, "
            "duration_minutes, is_organizer) as ground truth — do not contradict "
            "them. Rank EVERY meeting provided; do not skip any."
        ),
        "parameters": {
            "type":     "object",
            "required": ["meetings"],
            "properties": {
                "meetings": {
                    "type":        "array",
                    "description": "All meetings, each scored. One entry per input meeting.",
                    "items": {
                        "type":     "object",
                        "required": [
                            "event_id", "priority_score", "priority_label",
                            "reason", "urgency_signals", "action_required",
                            "prep_recommended",
                        ],
                        "properties": {
                            "event_id": {
                                "type":        "string",
                                "description": "Exact event_id value from the input meeting.",
                            },
                            "priority_score": {
                                "type":        "integer",
                                "minimum":     1,
                                "maximum":     10,
                                "description": "Priority 1 (low) to 10 (critical).",
                            },
                            "priority_label": {
                                "type":        "string",
                                "enum":        ["critical", "high", "medium", "low"],
                                "description": "Human label matching the score band.",
                            },
                            "reason": {
                                "type":        "string",
                                "description": "1-2 sentence explanation of this priority.",
                            },
                            "urgency_signals": {
                                "type":        "array",
                                "items":       {"type": "string", "enum": _URGENCY_VOCAB},
                                "description": (
                                    "Short tags drawn ONLY from the allowed vocabulary. "
                                    "Omit any that do not apply — do not invent new tags."
                                ),
                            },
                            "action_required": {
                                "type":        ["string", "null"],
                                "description": (
                                    "What the user must do before this meeting, or null "
                                    "if nothing is required."
                                ),
                            },
                            "prep_recommended": {
                                "type":        "boolean",
                                "description": (
                                    "True if the attendees/subject suggest dedicated prep "
                                    "would meaningfully help."
                                ),
                            },
                        },
                    },
                },
            },
        },
    },
}


# ── Public API ────────────────────────────────────────────────────────────────

async def get_calendar_priority(
    calendar,
    *,
    hours_ahead:  int           = 24,
    top_n:        int           = 10,
    user_context: Optional[str] = None,
    user_id:      str           = "default",
) -> dict:
    """
    Rank the upcoming meetings in the next `hours_ahead` hours by importance.

    `user_context` — optional string describing who the user is / their role,
    used to personalise the scoring (e.g. "VP of Engineering").

    Returns the response dict: {window_start, window_end, meetings: [...]}.
    Read-only: creates, modifies, and sends nothing. Does NOT touch LanceDB.
    """
    now          = datetime.now(timezone.utc)
    window_start = now
    window_end   = now + timedelta(hours=hours_ahead)
    start_iso    = _iso_z(window_start)
    end_iso      = _iso_z(window_end)

    events = await calendar.get_events(start_iso, end_iso, user_id=user_id)

    # Empty-result short-circuit — no LLM call.
    if not events:
        return {
            "window_start": start_iso,
            "window_end":   end_iso,
            "meetings":     [],
        }

    # Identify self ONCE. None ⇒ degraded self-signals (handled in _signals).
    signed_in_email = await get_signed_in_user_email(user_id)
    if signed_in_email is None:
        logger.info(
            "calendar_priority: signed-in email unknown — self-related signals "
            "(is_external, attendance_required, response_status) degrade to null."
        )

    # Deterministic pass: conflicts are mutual, so compute them across the set
    # first, then fold the per-event self/externality signals in.
    conflict_flags = _conflict_flags(events)
    base_meetings: Dict[str, dict] = {}
    descriptors: List[dict]        = []

    for idx, ev in enumerate(events):
        signals = _signals(ev, signed_in_email, conflict_flags[idx])
        base    = _base_meeting(ev, signals)
        base_meetings[base["event_id"]] = base

        descriptors.append({
            "event_id":            base["event_id"],
            "subject":             base["subject"],
            "start":               base["start"],
            "end":                 base["end"],
            "duration_minutes":    base["duration_minutes"],
            "organizer":           base["organizer"].get("name", ""),
            "attendee_count":      base["attendee_count"],
            "is_organizer":        base["is_organizer"],
            "is_external":         base["is_external"],
            "has_conflict":        base["has_conflict"],
            "attendance_required": base["attendance_required"],
            "response_status":     base["response_status"],
            "is_online":           bool(ev.get("is_online", False)),
            "location":            ev.get("location", "") or "",
            "body_preview":        (ev.get("body_preview", "") or "")[:200],
        })

    # ── Single forced tool call across ALL meetings ───────────────────────────
    role_context = f"User context: {user_context}\n\n" if user_context else ""
    system = (
        "You are a calendar triage analyst. Rank the provided meetings by how "
        "much they should command the user's attention in the look-ahead window. "
        "Weigh: decisions to be made, external/client-facing stakeholders, senior "
        "leadership, unresolved attendance (tentative/none on a required meeting), "
        "scheduling conflicts, large attendee counts, and long durations.\n\n"
        "Score 8-10 = critical/high (needs attention today), "
        "5-7 = medium (this week), 1-4 = low (can wait).\n\n"
        "For urgency_signals, choose ONLY from this exact vocabulary and omit any "
        "that do not apply:\n"
        f"{', '.join(_URGENCY_VOCAB)}.\n\n"
        f"{role_context}"
        "You MUST call the rank_meetings tool with ALL meetings provided — do not "
        "skip any."
    )

    user_msg = (
        f"Please rank these {len(descriptors)} meetings by priority:\n\n"
        + json.dumps(descriptors, separators=(",", ":"))
    )

    logger.info(f"calendar_priority: calling LLM tool with {len(descriptors)} meetings")

    result = await llm_tool_call(
        messages    = [{"role": "user", "content": user_msg}],
        tools       = [_RANK_TOOL],
        tool_choice = "required",
        system      = system,
    )

    ranked_raw = (result or {}).get("meetings", [])
    if not ranked_raw:
        logger.warning("calendar_priority: LLM returned no ranking — using neutral scores")

    # Overlay the LLM analysis onto the deterministic base. Deterministic signals
    # always win; the model only fills the ranking/analysis fields.
    for item in ranked_raw:
        base = base_meetings.get(item.get("event_id", ""))
        if base is None:
            continue
        base["priority_score"]   = _clamp_score(item.get("priority_score", 5))
        base["priority_label"]   = _valid_label(item.get("priority_label"))
        base["reason"]           = item.get("reason", "") or ""
        base["urgency_signals"]  = _valid_signals(item.get("urgency_signals"))
        base["action_required"]  = item.get("action_required") or None
        base["prep_recommended"] = bool(item.get("prep_recommended", False))

    meetings = list(base_meetings.values())
    meetings.sort(key=lambda m: m["priority_score"], reverse=True)

    return {
        "window_start": start_iso,
        "window_end":   end_iso,
        "meetings":     meetings[:top_n],
    }


# ── Deterministic signal computation ──────────────────────────────────────────

def _conflict_flags(events: List[dict]) -> List[bool]:
    """
    Pairwise overlap scan (O(n²); fine at top_n ≤ 30 and a typical day's events).
    Both events in any overlapping pair are flagged True — conflict is mutual.
    Events with unparseable times never conflict.
    """
    spans = [(_parse_dt(e.get("start")), _parse_dt(e.get("end"))) for e in events]
    flags = [False] * len(events)
    for i in range(len(events)):
        si, ei = spans[i]
        if si is None or ei is None:
            continue
        for j in range(i + 1, len(events)):
            sj, ej = spans[j]
            if sj is None or ej is None:
                continue
            # Half-open overlap: touching edges (back-to-back) do NOT conflict.
            if si < ej and sj < ei:
                flags[i] = True
                flags[j] = True
    return flags


def _signals(event: dict, self_email: Optional[str], has_conflict: bool) -> dict:
    """Compute the deterministic per-event signals (everything but the conflict
    flag, which is computed set-wide and passed in)."""
    attendees = event.get("attendees", []) or []
    organizer = event.get("organizer", {}) or {}
    org_email = (organizer.get("email") or "").strip().lower()

    # is_organizer — provable only when we know who "self" is.
    is_organizer = bool(self_email) and self_email == org_email

    # is_external — any participant (attendee/organizer) on a different domain.
    # null when self is unknown (we cannot say what "our" domain is).
    is_external: Optional[bool]
    if not self_email:
        is_external = None
    else:
        self_domain = _domain(self_email)
        others = {
            (a.get("email") or "").strip().lower() for a in attendees
        } | {org_email}
        others.discard(self_email)
        others.discard("")
        is_external = any(_domain(e) != self_domain for e in others)

    # attendance_required / response_status — read from the SELF attendee entry.
    # null when self is unknown or not listed (e.g. the user is the organizer and
    # not also an attendee).
    attendance_required: Optional[bool] = None
    response_status:     Optional[str]  = None
    if self_email:
        for a in attendees:
            if (a.get("email") or "").strip().lower() == self_email:
                attendance_required = (a.get("type", "required") or "required").lower() != "optional"
                response_status     = a.get("response") or None
                break

    return {
        "has_conflict":        has_conflict,
        "is_external":         is_external,
        "attendance_required": attendance_required,
        "response_status":     response_status,
        "is_organizer":        is_organizer,
        "attendee_count":      len(attendees),
        "duration_minutes":    _duration_minutes(event.get("start"), event.get("end")),
    }


def _base_meeting(event: dict, signals: dict) -> dict:
    """The response meeting dict: Graph metadata + deterministic signals +
    neutral defaults for the LLM-supplied ranking fields."""
    return {
        "event_id":            event.get("event_id", ""),
        "subject":             event.get("subject", ""),
        "start":               event.get("start", ""),
        "end":                 event.get("end", ""),
        "duration_minutes":    signals["duration_minutes"],
        "organizer":           event.get("organizer", {}) or {},
        "attendee_count":      signals["attendee_count"],
        "is_organizer":        signals["is_organizer"],
        "is_external":         signals["is_external"],
        "has_conflict":        signals["has_conflict"],
        "attendance_required": signals["attendance_required"],
        "response_status":     signals["response_status"],
        # LLM-supplied (neutral defaults survive if the model is unavailable).
        "priority_score":      5,
        "priority_label":      "medium",
        "reason":              "",
        "urgency_signals":     [],
        "action_required":     None,
        "prep_recommended":    False,
    }


# ── Small helpers ─────────────────────────────────────────────────────────────

def _domain(email: str) -> str:
    return email.split("@", 1)[1] if "@" in email else ""


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except Exception:
        return None


def _duration_minutes(start: Optional[str], end: Optional[str]) -> int:
    s, e = _parse_dt(start), _parse_dt(end)
    if s is None or e is None:
        return 0
    return max(0, int((e - s).total_seconds() // 60))


def _clamp_score(value) -> int:
    try:
        return max(1, min(10, int(value)))
    except (TypeError, ValueError):
        return 5


def _valid_label(label) -> str:
    return label if label in ("critical", "high", "medium", "low") else "medium"


def _valid_signals(signals) -> List[str]:
    if not isinstance(signals, list):
        return []
    return [s for s in signals if s in _URGENCY_VOCAB]


def _iso_z(dt: datetime) -> str:
    """UTC datetime → 'YYYY-MM-DDTHH:MM:SSZ' (Graph-friendly, no microseconds)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
