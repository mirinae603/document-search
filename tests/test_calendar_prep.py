# tests/test_calendar_prep.py
#
# Offline test harness for the read-only /calendar/prep vertical slice.
#
# Mirrors the conventions of tests/test_actions_phase1.py:
#   - The LanceDB store is a hand-written in-memory fake (FakeCalStore) whose
#     query chain reproduces store.chunks.search().where(...).limit(...).to_list()
#     and understands the participant-overlap `LIKE` clause + sent_at lower bound
#     that meeting_prep.retrieve_context builds.
#   - The CalendarConnector is a fake whose get_events() returns hand-written
#     event fixtures and records the (start_iso, end_iso) it was called with.
#   - llm_tool_call() is stubbed with a deterministic payload (AsyncMock).
#   - Async coroutines are driven from sync tests via asyncio.run().
#
# Vector search is intentionally NOT exercised: every test passes embedder=None,
# so retrieval runs the participant-overlap leg only — fully deterministic and
# offline. (The vector leg is the live-validation gap; see the report.)
#
# Run:  ./env/bin/python -m pytest tests/test_calendar_prep.py -v
from __future__ import annotations

import asyncio
import copy
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import BaseModel, ValidationError

# Resolves because tests/conftest.py prepends app/ to sys.path.
from intelligence import meeting_prep
from intelligence.meeting_prep import get_meeting_prep, retrieve_context


# ─────────────────────────────────────────────────────────────────────────────
# Time helpers — fixtures are built relative to "now" so the 60-day context
# window in get_meeting_prep accepts them regardless of the machine clock.
# ─────────────────────────────────────────────────────────────────────────────

def _ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


OWN_EMAIL = "me@acme.com"


def _chunks():
    """
    Synthetic ingested chunks:
      thread-1 (Outlook) — two chunks, NON-chronological in list order, both with
                           alice + bob. Exercises dedupe-by-thread + newest-wins.
      thread-3 (Outlook) — one chunk with carol. A second overlapping thread, so
                           recency ordering between threads is checkable.
      thread-9 (Teams)   — participants are UIDs (not emails), so the email
                           participant filter must NOT match it.
    """
    return [
        {
            "chunk_id": "c1b", "thread_id": "thread-1", "platform": "outlook",
            "file_id": "doc-1", "chunk_index": 1,
            "participants": '["alice@acme.com", "bob@acme.com"]',
            "sent_at": _ago(2),   # NEWER
            "text": "Newer message in the Acme thread about the quarterly review.",
        },
        {
            "chunk_id": "c1a", "thread_id": "thread-1", "platform": "outlook",
            "file_id": "doc-1", "chunk_index": 0,
            "participants": '["alice@acme.com", "bob@acme.com"]',
            "sent_at": _ago(9),   # OLDER
            "text": "Older message kicking off the Acme quarterly review thread.",
        },
        {
            "chunk_id": "c3", "thread_id": "thread-3", "platform": "outlook",
            "file_id": "doc-3", "chunk_index": 0,
            "participants": '["carol@acme.com"]',
            "sent_at": _ago(5),
            "text": "Carol asked about the budget line items.",
        },
        {
            "chunk_id": "c9", "thread_id": "thread-9", "platform": "teams",
            "file_id": "doc-9", "chunk_index": 0,
            "participants": '["alice-uid", "bob-uid"]',
            "sent_at": _ago(1),
            "text": "Teams DM — participants are opaque UIDs, not emails.",
        },
    ]


def _event(event_id, subject, attendee_emails, *, hours_out=2.0):
    start = datetime.now(timezone.utc) + timedelta(hours=hours_out)
    end   = start + timedelta(hours=1)
    return {
        "event_id":     event_id,
        "subject":      subject,
        "start":        start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end":          end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "location":     "Teams",
        "body_preview": "Agenda: review the numbers.",
        "is_online":    True,
        "organizer":    {"name": "Me", "email": OWN_EMAIL},
        "attendees":    [
            {"name": e.split("@")[0], "email": e, "response": "accepted"}
            for e in attendee_emails
        ],
    }


# Deterministic stand-in for what llm_tool_call() returns (the parsed arguments
# dict of the forced build_meeting_prep tool call).
LLM_RESULT = {
    "summary": "Quarterly review with Acme; prior threads cover scope and budget.",
    "thread_notes": [
        {"thread_id": "thread-1", "one_line": "Kickoff + scheduling for the review."},
        {"thread_id": "thread-3", "one_line": "Budget line-item questions from Carol."},
    ],
    "open_questions":           ["Is the budget finalised?"],
    "decisions_needed":         ["Approve the Q3 spend."],
    "suggested_talking_points": ["Walk through the revised numbers."],
    "confidence": "high",
}


# ─────────────────────────────────────────────────────────────────────────────
# Fake LanceDB store — understands meeting_prep's structured where() clause.
# ─────────────────────────────────────────────────────────────────────────────

class _FakeQuery:
    def __init__(self, rows, where_log):
        self._rows = rows
        self._where_log = where_log
        self._where = ""
        self._limit = None

    def where(self, clause, prefilter=False):
        self._where = clause or ""
        self._where_log.append(self._where)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def to_list(self):
        likes    = re.findall(r"LIKE\s+'%([^%]*)%'", self._where)
        m_sent   = re.search(r"sent_at\s*>=\s*'([^']*)'", self._where)
        min_sent = m_sent.group(1) if m_sent else None
        need_platform = "platform != ''" in self._where

        out = []
        for r in self._rows:
            if need_platform and not r.get("platform"):
                continue
            if likes and not any(sub in (r.get("participants") or "") for sub in likes):
                continue
            if min_sent and (r.get("sent_at") or "") < min_sent:
                continue
            out.append(copy.deepcopy(r))
        if self._limit is not None:
            out = out[: self._limit]
        return out


class _FakeChunks:
    def __init__(self, rows):
        self._rows = rows
        self.where_log = []   # every where() clause issued, in order

    def search(self, *args, **kwargs):
        # A vector arg would be passed positionally; tests never supply an
        # embedder, so search() is only ever called with no args here.
        return _FakeQuery(self._rows, self.where_log)


class FakeCalStore:
    def __init__(self, rows):
        self.chunks = _FakeChunks(rows)


# ─────────────────────────────────────────────────────────────────────────────
# Fake calendar connector
# ─────────────────────────────────────────────────────────────────────────────

class FakeCalendar:
    def __init__(self, events):
        self._events = events
        self.calls = []   # list of (start_iso, end_iso, user_id)

    async def get_events(self, start_iso, end_iso, user_id="default", top=50):
        self.calls.append((start_iso, end_iso, user_id))
        return copy.deepcopy(self._events)


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic schema for §5 response validation
# ─────────────────────────────────────────────────────────────────────────────

class _Person(BaseModel):
    name: str
    email: str


class _Attendee(_Person):
    response: str


class _RecentThread(BaseModel):
    platform: str
    thread_id: str
    one_line: str
    last_activity: str
    participants_overlap: List[str]


class _Prep(BaseModel):
    summary: str
    recent_threads: List[_RecentThread]
    open_questions: List[str]
    decisions_needed: List[str]
    suggested_talking_points: List[str]


class _Meeting(BaseModel):
    event_id: str
    subject: str
    start: str
    end: str
    location: str
    attendees: List[_Attendee]
    organizer: _Person
    is_online: bool
    prep: _Prep
    confidence: str


class _PrepResponse(BaseModel):
    window_start: str
    window_end: str
    meetings: List[_Meeting]


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def store():
    return FakeCalStore(_chunks())


@pytest.fixture
def mock_llm(monkeypatch):
    """Patch llm_tool_call at the point meeting_prep imported it."""
    mock = AsyncMock(return_value=copy.deepcopy(LLM_RESULT))
    monkeypatch.setattr(meeting_prep, "llm_tool_call", mock)
    return mock


# ─────────────────────────────────────────────────────────────────────────────
# retrieve_context unit tests (deterministic — cutoff disabled, no embedder)
# ─────────────────────────────────────────────────────────────────────────────

def test_retrieve_context_dedupes_and_sorts_by_recency(store):
    event = _event("evt-1", "Quarterly review with Acme",
                   ["alice@acme.com", "carol@acme.com"])

    threads = asyncio.run(retrieve_context(
        event, store, embedder=None, context_per_meeting=5,
        cutoff_iso=None, own_email=OWN_EMAIL,
    ))

    # Two distinct threads, NOT three chunks — thread-1's two chunks deduped.
    ids = [t["thread_id"] for t in threads]
    assert ids == ["thread-1", "thread-3"], "expected dedupe + recency order"

    t1 = threads[0]
    # newest chunk wins as the thread's representative
    assert t1["last_activity"] == _chunks()[0]["sent_at"]  # the _ago(2) chunk
    assert "alice@acme.com" in t1["participants_overlap"]
    # bob is in the thread but not an attendee → not in the overlap
    assert "bob@acme.com" not in t1["participants_overlap"]
    assert len(t1["excerpt"]) <= 500

    assert threads[1]["participants_overlap"] == ["carol@acme.com"]


def test_retrieve_context_excludes_owner_and_nonmatching(store):
    # Only the meeting owner as attendee → after stripping owner, no emails to
    # match → no structured hits, no embedder → empty (no_context).
    event = _event("evt-solo", "1:1 with myself", [OWN_EMAIL])
    threads = asyncio.run(retrieve_context(
        event, store, embedder=None, cutoff_iso=None, own_email=OWN_EMAIL,
    ))
    assert threads == []


def test_retrieve_context_teams_uids_do_not_match_email_filter(store):
    # An external attendee that matches nothing — Teams UID chunk must not leak.
    event = _event("evt-x", "External sync", ["stranger@external.com"])
    threads = asyncio.run(retrieve_context(
        event, store, embedder=None, cutoff_iso=None, own_email=OWN_EMAIL,
    ))
    assert threads == []


# ─────────────────────────────────────────────────────────────────────────────
# get_meeting_prep handler-level tests
# ─────────────────────────────────────────────────────────────────────────────

def test_get_events_called_with_correct_iso_window(store, mock_llm):
    events = [_event("evt-1", "Acme review", ["alice@acme.com"])]
    cal = FakeCalendar(events)

    before = datetime.now(timezone.utc)
    asyncio.run(get_meeting_prep(
        store, cal, hours_ahead=24, top_n=5, context_per_meeting=5, embedder=None,
    ))
    after = datetime.now(timezone.utc)

    assert len(cal.calls) == 1
    start_iso, end_iso, _ = cal.calls[0]

    start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    end_dt   = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))

    # window starts ~now and spans exactly hours_ahead
    assert before - timedelta(seconds=5) <= start_dt <= after + timedelta(seconds=5)
    assert end_dt - start_dt == timedelta(hours=24)


def test_no_context_path_skips_llm(store, mock_llm):
    # One meeting WITH overlapping context, one WITHOUT.
    events = [
        _event("evt-ctx", "Acme review", ["alice@acme.com"]),
        _event("evt-empty", "Cold intro", ["stranger@external.com"]),
    ]
    cal = FakeCalendar(events)

    resp = asyncio.run(get_meeting_prep(
        store, cal, hours_ahead=24, top_n=5, context_per_meeting=5, embedder=None,
    ))

    by_id = {m["event_id"]: m for m in resp["meetings"]}

    # no_context card assembled directly, LLM not consulted for it
    empty = by_id["evt-empty"]
    assert empty["confidence"] == "no_context"
    assert empty["prep"]["recent_threads"] == []
    assert empty["prep"]["summary"] == meeting_prep._NO_CONTEXT_SUMMARY

    # the meeting with context did go through the LLM
    ctx = by_id["evt-ctx"]
    assert ctx["confidence"] == "high"
    assert ctx["prep"]["recent_threads"]

    # exactly one LLM call — only the meeting that had context
    assert mock_llm.call_count == 1


def test_llm_called_with_tool_choice_required(store, mock_llm):
    events = [_event("evt-ctx", "Acme review", ["alice@acme.com"])]
    cal = FakeCalendar(events)

    asyncio.run(get_meeting_prep(
        store, cal, hours_ahead=24, top_n=5, embedder=None,
    ))

    assert mock_llm.call_count == 1
    kwargs = mock_llm.call_args.kwargs
    assert kwargs["tool_choice"] == "required"
    # the forced tool is our prep schema
    assert kwargs["tools"][0]["function"]["name"] == "build_meeting_prep"


def test_response_matches_schema(store, mock_llm):
    events = [
        _event("evt-ctx", "Acme review", ["alice@acme.com", "carol@acme.com"]),
        _event("evt-empty", "Cold intro", ["stranger@external.com"]),
    ]
    cal = FakeCalendar(events)

    resp = asyncio.run(get_meeting_prep(
        store, cal, hours_ahead=48, top_n=5, context_per_meeting=5,
        user_context="VP of Engineering", embedder=None,
    ))

    # Round-trips through the §5 Pydantic schema with no extra/missing fields.
    try:
        validated = _PrepResponse(**resp)
    except ValidationError as e:  # pragma: no cover - failure path
        pytest.fail(f"response did not match §5 schema:\n{e}")

    assert len(validated.meetings) == 2
    # merged thread facts survive: one_line from LLM, last_activity from retrieval
    ctx = next(m for m in validated.meetings if m.event_id == "evt-ctx")
    threads = {t.thread_id: t for t in ctx.prep.recent_threads}
    assert threads["thread-1"].one_line  # populated from LLM thread_notes
    assert threads["thread-1"].last_activity  # populated from retrieval facts


def test_top_n_caps_meetings(store, mock_llm):
    events = [
        _event(f"evt-{i}", f"Meeting {i}", ["alice@acme.com"], hours_out=i + 1)
        for i in range(8)
    ]
    cal = FakeCalendar(events)

    resp = asyncio.run(get_meeting_prep(
        store, cal, hours_ahead=168, top_n=3, embedder=None,
    ))
    assert len(resp["meetings"]) == 3


def test_handler_makes_no_outbound_http(store, mock_llm, monkeypatch):
    """
    With the connector and llm_tool_call mocked and no embedder, the prep path
    must make ZERO outbound HTTP. Patch both AsyncClient.post and .send.
    """
    events = [
        _event("evt-ctx", "Acme review", ["alice@acme.com"]),
        _event("evt-empty", "Cold intro", ["stranger@external.com"]),
    ]
    cal = FakeCalendar(events)

    post_spy = AsyncMock()
    send_spy = AsyncMock()
    monkeypatch.setattr(httpx.AsyncClient, "post", post_spy)
    monkeypatch.setattr(httpx.AsyncClient, "send", send_spy)

    asyncio.run(get_meeting_prep(
        store, cal, hours_ahead=24, top_n=5, embedder=None,
    ))

    assert post_spy.call_count == 0, "prep made an outbound httpx POST"
    assert send_spy.call_count == 0, "prep made an outbound httpx request"


# ─────────────────────────────────────────────────────────────────────────────
# Slice 2 — attendee email → Graph UID resolution + own-email strip
#
# These tests mock the Graph directory lookups at two layers:
#   - graph_users._get_token  → patched to a fixed fake token (bypasses MSAL)
#   - httpx.AsyncClient.get    → routed by URL to /me and /users/{email} fakes
# Vector search stays off (embedder=None), so the only where() clause issued is
# the structured participant filter, captured via store.chunks.where_log.
# ─────────────────────────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _GraphMock:
    """Routes GET /me and GET /users/{email}; records per-email fetch counts."""

    def __init__(self):
        self.me_email   = "me@acme.com"   # set None to simulate /me failure
        self.uid_map    = {}              # email -> uid (absent ⇒ 404)
        self.fail_users = False           # True ⇒ every /users call 500s
        self.users_calls = []             # HTTP-level /users fetches (emails)
        self.me_calls   = 0

    def handle(self, url):
        if "/users/" in url:
            email = url.split("/users/")[1].split("?")[0]
            self.users_calls.append(email)
            if self.fail_users:
                return _Resp(500, {})
            uid = self.uid_map.get(email)
            if uid is None:
                return _Resp(404, {})
            return _Resp(200, {"id": uid})
        # /me
        self.me_calls += 1
        if not self.me_email:
            return _Resp(403, {})
        return _Resp(200, {"mail": self.me_email, "userPrincipalName": self.me_email})


@pytest.fixture
def mock_graph(monkeypatch):
    from connectors import graph_users

    # Reset module-level memoization so token-keyed /me cache can't leak.
    graph_users._ME_CACHE.clear()
    graph_users._connector = None

    graph = _GraphMock()
    monkeypatch.setattr(graph_users, "_get_token", AsyncMock(return_value="tok"))

    async def _fake_get(self, url, headers=None, **kwargs):
        return graph.handle(url)

    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get)
    return graph


def _participant_filter(store):
    """The structured participant LIKE clause (first one issued)."""
    for w in store.chunks.where_log:
        if "participants LIKE" in w:
            return w
    return ""


def _run(store, events, **kwargs):
    cal = FakeCalendar(events)
    resp = asyncio.run(get_meeting_prep(store, cal, embedder=None, **kwargs))
    return resp


def test_all_attendees_resolve_filter_has_emails_and_uids(store, mock_llm, mock_graph):
    mock_graph.uid_map = {
        "alice@acme.com": "alice-uid",
        "bob@acme.com":   "bob-uid",
        "carol@acme.com": "carol-uid",
    }
    events = [_event("evt-1", "Acme review",
                     ["alice@acme.com", "bob@acme.com", "carol@acme.com"])]
    _run(store, events, hours_ahead=24, top_n=5)

    clause = _participant_filter(store)
    for token in ("alice@acme.com", "bob@acme.com", "carol@acme.com",
                  "alice-uid", "bob-uid", "carol-uid"):
        assert token in clause, f"missing {token} in filter"


def test_mixed_resolution_unresolvable_stays_email_only(store, mock_llm, mock_graph):
    # alice + bob resolve, carol does not (404 → None).
    mock_graph.uid_map = {"alice@acme.com": "alice-uid", "bob@acme.com": "bob-uid"}
    events = [_event("evt-1", "Acme review",
                     ["alice@acme.com", "bob@acme.com", "carol@acme.com"])]
    _run(store, events, hours_ahead=24, top_n=5)

    clause = _participant_filter(store)
    # all three emails present
    assert "alice@acme.com" in clause
    assert "bob@acme.com" in clause
    assert "carol@acme.com" in clause       # unresolvable, still present as email
    # the two resolved uids present
    assert "alice-uid" in clause
    assert "bob-uid" in clause
    # carol has no uid form
    assert "carol-uid" not in clause


def test_total_resolver_failure_is_email_only(store, mock_llm, mock_graph):
    mock_graph.fail_users = True   # every /users call 500s
    events = [_event("evt-1", "Acme review", ["alice@acme.com", "bob@acme.com"])]
    resp = _run(store, events, hours_ahead=24, top_n=5)

    clause = _participant_filter(store)
    assert "alice@acme.com" in clause
    assert "bob@acme.com" in clause
    # no UID terms leaked in
    assert "-uid" not in clause
    # meeting still returned, no exception bubbled
    assert len(resp["meetings"]) == 1


def test_per_request_cache_dedups_shared_attendee(store, mock_llm, mock_graph):
    mock_graph.uid_map = {"alice@acme.com": "alice-uid", "carol@acme.com": "carol-uid"}
    # Two meetings both include alice@acme.com.
    events = [
        _event("evt-1", "Acme review A", ["alice@acme.com"], hours_out=2),
        _event("evt-2", "Acme review B", ["alice@acme.com", "carol@acme.com"], hours_out=4),
    ]
    _run(store, events, hours_ahead=24, top_n=5)

    # alice fetched from Graph exactly once across the whole request.
    assert mock_graph.users_calls.count("alice@acme.com") == 1
    assert mock_graph.users_calls.count("carol@acme.com") == 1


def test_own_email_stripped_from_filter(store, mock_llm, mock_graph):
    mock_graph.me_email = "me@acme.com"
    mock_graph.uid_map = {"me@acme.com": "me-uid", "alice@acme.com": "alice-uid"}
    # me@acme.com is in the attendee list and is the signed-in user.
    events = [_event("evt-1", "Acme review", ["me@acme.com", "alice@acme.com"])]
    _run(store, events, hours_ahead=24, top_n=5)

    clause = _participant_filter(store)
    # neither the owner's email nor its UID form may appear
    assert "me@acme.com" not in clause
    assert "me-uid" not in clause
    # the other attendee is still there in both forms
    assert "alice@acme.com" in clause
    assert "alice-uid" in clause
    # owner was never even looked up
    assert "me@acme.com" not in mock_graph.users_calls


def test_own_email_lookup_failure_preserves_behavior(store, mock_llm, mock_graph):
    mock_graph.me_email = None   # GET /me fails → no strip
    mock_graph.uid_map = {"alice@acme.com": "alice-uid"}
    events = [_event("evt-1", "Acme review", ["alice@acme.com"])]
    resp = _run(store, events, hours_ahead=24, top_n=5)

    # request succeeds, meeting returned, filter still built from the attendee
    assert len(resp["meetings"]) == 1
    clause = _participant_filter(store)
    assert "alice@acme.com" in clause


def test_filter_term_cap_prefers_emails(store, mock_llm, mock_graph):
    attendees = [f"user{i}@acme.com" for i in range(25)]
    mock_graph.uid_map = {e: f"uid-{i}" for i, e in enumerate(attendees)}
    events = [_event("evt-1", "Big meeting", attendees)]
    _run(store, events, hours_ahead=24, top_n=5)

    clause = _participant_filter(store)
    n_terms = clause.count("participants LIKE")
    assert n_terms <= 20, f"filter has {n_terms} OR terms, cap is 20"
    # emails preferred at the boundary → 20 emails fill the cap, no UIDs
    assert "uid-" not in clause
