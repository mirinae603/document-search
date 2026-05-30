# tests/test_calendar_priority.py
#
# Offline test harness for the read-only /calendar/priority triage endpoint.
#
# Conventions mirror tests/test_calendar_prep.py:
#   - The CalendarConnector is a fake whose get_events() returns hand-written
#     event fixtures and records the (start_iso, end_iso, user_id) it was called with.
#   - llm_tool_call() is stubbed with a deterministic payload (AsyncMock) and its
#     call_count is the load-bearing "single LLM call" assertion.
#   - get_signed_in_user_email() is stubbed per test (self known / unknown).
#   - Async coroutines are driven from sync tests via asyncio.run().
#
# No LanceDB, no store — this endpoint is not corpus-aware. Fully offline.
#
# Run:  ./env/bin/python -m pytest tests/test_calendar_priority.py -v
from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel, ValidationError

# Resolves because tests/conftest.py prepends app/ to sys.path.
from intelligence import calendar_priority
from intelligence.calendar_priority import get_calendar_priority


OWN_EMAIL = "alice@acme.com"


# ─────────────────────────────────────────────────────────────────────────────
# Event fixtures — built relative to "now" so they always fall in the window.
# ─────────────────────────────────────────────────────────────────────────────

def _attendee(email, *, response="accepted", type_="required"):
    return {"name": email.split("@")[0], "email": email, "response": response, "type": type_}


def _event(event_id, subject, attendees, *, organizer_email=OWN_EMAIL,
           hours_out=2.0, duration_hours=1.0):
    start = datetime.now(timezone.utc) + timedelta(hours=hours_out)
    end   = start + timedelta(hours=duration_hours)
    return {
        "event_id":     event_id,
        "subject":      subject,
        "start":        start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end":          end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "location":     "Teams",
        "body_preview": "Agenda: review the numbers.",
        "is_online":    True,
        "organizer":    {"name": organizer_email.split("@")[0], "email": organizer_email},
        "attendees":    list(attendees),
    }


def _llm_item(event_id, score):
    return {
        "event_id":         event_id,
        "priority_score":   score,
        "priority_label":   "high" if score >= 8 else "medium" if score >= 5 else "low",
        "reason":           f"Synthetic reason for {event_id}.",
        "urgency_signals":  ["decision_needed"],
        "action_required":  "Review the agenda." if score >= 8 else None,
        "prep_recommended": score >= 8,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Fakes
# ─────────────────────────────────────────────────────────────────────────────

class FakeCalendar:
    def __init__(self, events):
        self._events = events
        self.calls = []   # list of (start_iso, end_iso, user_id)

    async def get_events(self, start_iso, end_iso, user_id="default", top=50):
        self.calls.append((start_iso, end_iso, user_id))
        return copy.deepcopy(self._events)


def _mock_llm_scoring(events, scores):
    """Build an AsyncMock returning a rank_meetings payload mapping events→scores."""
    payload = {"meetings": [_llm_item(e["event_id"], s) for e, s in zip(events, scores)]}
    return AsyncMock(return_value=copy.deepcopy(payload))


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic schema for the response shape
# ─────────────────────────────────────────────────────────────────────────────

class _Organizer(BaseModel):
    name: str
    email: str


class _Meeting(BaseModel):
    event_id: str
    subject: str
    start: str
    end: str
    duration_minutes: int
    organizer: _Organizer
    attendee_count: int
    is_organizer: bool
    is_external: Optional[bool]
    has_conflict: bool
    attendance_required: Optional[bool]
    response_status: Optional[str]
    priority_score: int
    priority_label: str
    reason: str
    urgency_signals: List[str]
    action_required: Optional[str]
    prep_recommended: bool


class _PriorityResponse(BaseModel):
    window_start: str
    window_end: str
    meetings: List[_Meeting]


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_self(monkeypatch):
    """Patch get_signed_in_user_email at the point calendar_priority imported it.
    Returns the AsyncMock so tests can flip its return value (e.g. None)."""
    mock = AsyncMock(return_value=OWN_EMAIL)
    monkeypatch.setattr(calendar_priority, "get_signed_in_user_email", mock)
    return mock


def _patch_llm(monkeypatch, mock):
    monkeypatch.setattr(calendar_priority, "llm_tool_call", mock)
    return mock


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_single_llm_call_for_many_meetings(monkeypatch, mock_self):
    """Load-bearing: a 5-meeting window triggers exactly ONE llm_tool_call."""
    events = [_event(f"evt-{i}", f"Meeting {i}", [_attendee(OWN_EMAIL), _attendee(f"x{i}@acme.com")],
                     hours_out=i + 1) for i in range(5)]
    cal = FakeCalendar(events)
    mock_llm = _patch_llm(monkeypatch, _mock_llm_scoring(events, [5, 6, 7, 8, 9]))

    resp = asyncio.run(get_calendar_priority(cal, hours_ahead=24, top_n=10))

    assert mock_llm.call_count == 1
    assert len(resp["meetings"]) == 5


def test_empty_window_short_circuits_llm(monkeypatch, mock_self):
    cal = FakeCalendar([])
    mock_llm = _patch_llm(monkeypatch, AsyncMock(return_value={"meetings": []}))

    resp = asyncio.run(get_calendar_priority(cal, hours_ahead=24, top_n=10))

    assert resp["meetings"] == []
    assert mock_llm.call_count == 0
    assert "window_start" in resp and "window_end" in resp


def test_conflict_detection_mutual_and_three_way(monkeypatch, mock_self):
    # A (2h-3h) and B (2.5h-3.5h) overlap; C (5h-6h) does not.
    a = _event("A", "A", [_attendee(OWN_EMAIL)], hours_out=2.0, duration_hours=1.0)
    b = _event("B", "B", [_attendee(OWN_EMAIL)], hours_out=2.5, duration_hours=1.0)
    c = _event("C", "C", [_attendee(OWN_EMAIL)], hours_out=5.0, duration_hours=1.0)
    cal = FakeCalendar([a, b, c])
    _patch_llm(monkeypatch, _mock_llm_scoring([a, b, c], [5, 5, 5]))

    resp = asyncio.run(get_calendar_priority(cal, hours_ahead=24, top_n=10))
    by_id = {m["event_id"]: m for m in resp["meetings"]}

    assert by_id["A"]["has_conflict"] is True   # both members of a pair flagged
    assert by_id["B"]["has_conflict"] is True
    assert by_id["C"]["has_conflict"] is False

    # Three-way overlap → all three True.
    d = _event("D", "D", [_attendee(OWN_EMAIL)], hours_out=2.0, duration_hours=2.0)
    e = _event("E", "E", [_attendee(OWN_EMAIL)], hours_out=2.5, duration_hours=2.0)
    f = _event("F", "F", [_attendee(OWN_EMAIL)], hours_out=3.0, duration_hours=2.0)
    cal2 = FakeCalendar([d, e, f])
    _patch_llm(monkeypatch, _mock_llm_scoring([d, e, f], [5, 5, 5]))
    resp2 = asyncio.run(get_calendar_priority(cal2, hours_ahead=24, top_n=10))
    assert all(m["has_conflict"] is True for m in resp2["meetings"])


def test_is_external_true_and_false(monkeypatch, mock_self):
    internal = _event("INT", "All-hands",
                       [_attendee(OWN_EMAIL), _attendee("bob@acme.com")])
    external = _event("EXT", "Client sync",
                      [_attendee(OWN_EMAIL), _attendee("vendor@bigclient.com")])
    cal = FakeCalendar([internal, external])
    _patch_llm(monkeypatch, _mock_llm_scoring([internal, external], [5, 6]))

    resp = asyncio.run(get_calendar_priority(cal, hours_ahead=24, top_n=10))
    by_id = {m["event_id"]: m for m in resp["meetings"]}

    assert by_id["INT"]["is_external"] is False
    assert by_id["EXT"]["is_external"] is True


def test_is_external_null_when_self_unknown(monkeypatch, mock_self):
    mock_self.return_value = None   # GET /me failed
    events = [_event("INT", "All-hands", [_attendee(OWN_EMAIL), _attendee("vendor@bigclient.com")])]
    cal = FakeCalendar(events)
    _patch_llm(monkeypatch, _mock_llm_scoring(events, [5]))

    resp = asyncio.run(get_calendar_priority(cal, hours_ahead=24, top_n=10))
    m = resp["meetings"][0]

    assert m["is_external"] is None
    assert m["attendance_required"] is None
    assert m["response_status"] is None
    assert len(resp["meetings"]) == 1   # endpoint still succeeds


def test_self_signal_extraction_optional_tentative(monkeypatch, mock_self):
    # Self is an optional attendee who tentatively accepted.
    events = [_event("M", "Optional sync",
                     [_attendee(OWN_EMAIL, response="tentative", type_="optional"),
                      _attendee("bob@acme.com")])]
    cal = FakeCalendar(events)
    _patch_llm(monkeypatch, _mock_llm_scoring(events, [5]))

    resp = asyncio.run(get_calendar_priority(cal, hours_ahead=24, top_n=10))
    m = resp["meetings"][0]

    assert m["attendance_required"] is False
    assert m["response_status"] == "tentative"


def test_self_organizer_not_in_attendees(monkeypatch, mock_self):
    # Self organises but is not listed among attendees.
    events = [_event("M", "I called this", [_attendee("bob@acme.com")],
                     organizer_email=OWN_EMAIL)]
    cal = FakeCalendar(events)
    _patch_llm(monkeypatch, _mock_llm_scoring(events, [5]))

    resp = asyncio.run(get_calendar_priority(cal, hours_ahead=24, top_n=10))
    m = resp["meetings"][0]

    assert m["is_organizer"] is True
    assert m["attendance_required"] is None   # self not in attendees
    assert m["response_status"] is None


def test_zero_attendee_focus_block(monkeypatch, mock_self):
    # A focus block with no attendees; self is the organizer.
    events = [_event("FOCUS", "Deep work", [], organizer_email=OWN_EMAIL)]
    cal = FakeCalendar(events)
    _patch_llm(monkeypatch, _mock_llm_scoring(events, [3]))

    resp = asyncio.run(get_calendar_priority(cal, hours_ahead=24, top_n=10))
    m = resp["meetings"][0]

    assert m["attendee_count"] == 0
    assert m["is_external"] is False           # self known, no foreign domains
    assert m["attendance_required"] is None    # self not in (empty) attendees
    assert m["response_status"] is None
    assert m["is_organizer"] is True


def test_sort_and_top_n(monkeypatch, mock_self):
    events = [_event(f"evt-{i}", f"Meeting {i}", [_attendee(OWN_EMAIL)], hours_out=i + 1)
              for i in range(5)]
    cal = FakeCalendar(events)
    # Scores out of order: [3, 9, 7, 1, 5]
    _patch_llm(monkeypatch, _mock_llm_scoring(events, [3, 9, 7, 1, 5]))

    resp = asyncio.run(get_calendar_priority(cal, hours_ahead=24, top_n=3))

    scores = [m["priority_score"] for m in resp["meetings"]]
    assert scores == [9, 7, 5]
    assert len(resp["meetings"]) == 3


def test_tool_choice_required(monkeypatch, mock_self):
    events = [_event("M", "Sync", [_attendee(OWN_EMAIL)])]
    cal = FakeCalendar(events)
    mock_llm = _patch_llm(monkeypatch, _mock_llm_scoring(events, [5]))

    asyncio.run(get_calendar_priority(cal, hours_ahead=24, top_n=10))

    assert mock_llm.call_count == 1
    kwargs = mock_llm.call_args.kwargs
    assert kwargs["tool_choice"] == "required"
    assert kwargs["tools"][0]["function"]["name"] == "rank_meetings"


def test_response_matches_schema(monkeypatch, mock_self):
    events = [
        _event("INT", "All-hands", [_attendee(OWN_EMAIL), _attendee("bob@acme.com")]),
        _event("EXT", "Client sync", [_attendee(OWN_EMAIL, response="none"),
                                       _attendee("vendor@bigclient.com")]),
    ]
    cal = FakeCalendar(events)
    _patch_llm(monkeypatch, _mock_llm_scoring(events, [6, 9]))

    resp = asyncio.run(get_calendar_priority(
        cal, hours_ahead=48, top_n=10, user_context="VP of Engineering"))

    try:
        validated = _PriorityResponse(**resp)
    except ValidationError as e:  # pragma: no cover - failure path
        pytest.fail(f"response did not match schema:\n{e}")

    assert len(validated.meetings) == 2
    assert validated.meetings[0].priority_score == 9   # external ranked first


def test_get_events_called_with_correct_iso_window(monkeypatch, mock_self):
    events = [_event("M", "Sync", [_attendee(OWN_EMAIL)])]
    cal = FakeCalendar(events)
    _patch_llm(monkeypatch, _mock_llm_scoring(events, [5]))

    before = datetime.now(timezone.utc)
    asyncio.run(get_calendar_priority(cal, hours_ahead=24, top_n=10))
    after = datetime.now(timezone.utc)

    assert len(cal.calls) == 1
    start_iso, end_iso, _ = cal.calls[0]
    start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    end_dt   = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    assert before - timedelta(seconds=5) <= start_dt <= after + timedelta(seconds=5)
    assert end_dt - start_dt == timedelta(hours=24)


def test_invalid_llm_signals_and_score_are_sanitised(monkeypatch, mock_self):
    """Out-of-vocab urgency tags are dropped; out-of-range scores are clamped."""
    events = [_event("M", "Sync", [_attendee(OWN_EMAIL)])]
    cal = FakeCalendar(events)
    bad_payload = {"meetings": [{
        "event_id": "M",
        "priority_score": 99,                       # out of range → clamp to 10
        "priority_label": "nonsense",               # invalid → medium
        "reason": "x",
        "urgency_signals": ["decision_needed", "made_up_tag"],  # drop the bogus one
        "action_required": None,
        "prep_recommended": True,
    }]}
    _patch_llm(monkeypatch, AsyncMock(return_value=bad_payload))

    resp = asyncio.run(get_calendar_priority(cal, hours_ahead=24, top_n=10))
    m = resp["meetings"][0]

    assert m["priority_score"] == 10
    assert m["priority_label"] == "medium"
    assert m["urgency_signals"] == ["decision_needed"]
