# tests/test_actions_phase1.py
#
# Phase 1 integration harness for the "actions" vertical slice.
#
# Validates the propose -> get -> list -> patch -> execute -> dismiss flow
# end-to-end with the three external dependencies mocked:
#
#   1. LanceDB store      -> FakeLanceStore (in-memory chunk rows)
#   2. llm_tool_call()    -> AsyncMock returning a deterministic tool result
#   3. the sqlite DB file -> redirected into pytest's tmp_path
#
# Storage is NOT mocked: every test runs against a real sqlite file (real
# migrations, real JSON serialise/deserialise). That is the point of an
# integration-style harness.
#
# Async route/proposer coroutines are driven with asyncio.run() inside plain
# sync test functions, so the harness needs no pytest-asyncio dependency.
#
# Run:  ./env/bin/python -m pytest tests/test_actions_phase1.py -v
from __future__ import annotations

import asyncio
import copy
import re
import uuid
from datetime import datetime
from unittest.mock import AsyncMock

import httpx
import pytest

# These imports resolve because tests/conftest.py prepends `app/` to sys.path.
from actions.models import (
    ActionProposal,
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

# ─────────────────────────────────────────────────────────────────────────────
# Fixed test data
# ─────────────────────────────────────────────────────────────────────────────

THREAD_ID = "test-thread-abc123"

# Three fake Outlook chunks for THREAD_ID, deliberately stored in
# NON-chronological order so the chronological sort in propose_draft_email's
# _scan_outlook_thread() is actually exercised.
#
# Column names match storage/models.py:Chunk exactly:
#   - `participants` is a JSON *string* (the real schema stores it that way and
#     proposer._participants_str() calls json.loads on it).
#   - `document_id` is the doc id column (proposer reads document_id/file_id,
#     not `doc_id`).
# Each `text` carries a unique SENTINEL token so prompt ordering is checkable.
FAKE_CHUNKS = [
    {  # chronologically LAST, listed first
        "chunk_id": "chunk-3",
        "file_id": "doc-outlook-1",
        "document_id": "doc-outlook-1",
        "filename": "outlook-thread.eml",
        "file_path": "outlook://test-thread-abc123",
        "platform": "outlook",
        "thread_id": THREAD_ID,
        "sent_at": "2025-11-03T11:15:00Z",
        "participants": '["alice@example.com", "bob@example.com"]',
        "section_heading": "Re: Q4 planning sync",
        "text": "SENTINEL_CHARLIE Bob: Tuesday afternoon works, let's confirm.",
        "source_type": "outlook_connector",
    },
    {  # chronologically FIRST, listed second
        "chunk_id": "chunk-1",
        "file_id": "doc-outlook-1",
        "document_id": "doc-outlook-1",
        "filename": "outlook-thread.eml",
        "file_path": "outlook://test-thread-abc123",
        "platform": "outlook",
        "thread_id": THREAD_ID,
        "sent_at": "2025-11-01T09:00:00Z",
        "participants": '["alice@example.com", "bob@example.com"]',
        "section_heading": "Re: Q4 planning sync",
        "text": "SENTINEL_ALPHA Hi Bob, can we move the Q4 planning sync to next Tuesday? Thanks, Alice",
        "source_type": "outlook_connector",
    },
    {  # chronologically MIDDLE, listed third
        "chunk_id": "chunk-2",
        "file_id": "doc-outlook-1",
        "document_id": "doc-outlook-1",
        "filename": "outlook-thread.eml",
        "file_path": "outlook://test-thread-abc123",
        "platform": "outlook",
        "thread_id": THREAD_ID,
        "sent_at": "2025-11-02T14:30:00Z",
        "participants": '["alice@example.com", "bob@example.com"]',
        "section_heading": "Re: Q4 planning sync",
        "text": "SENTINEL_BRAVO Alice: Any time after 1pm is fine for me.",
        "source_type": "outlook_connector",
    },
]

# Deterministic stand-in for what llm_tool_call() returns: per intelligence/
# llm.py the function returns the *parsed arguments dict* of the first tool
# call directly (json.loads of tool_calls[0].function.arguments).
LLM_RESULT = {
    "to": ["alice@example.com"],
    "cc": [],
    "subject": "Re: Q4 planning sync",
    "body": "Hi Alice,\n\nTuesday works for me. Let's lock it in.\n\nThanks,\nBob",
}


# ── Teams fixtures ────────────────────────────────────────────────────────────
#
# Ground truth from connectors/chat_indexer.py + connectors/teams.py:
# Teams chunks carry NO chat_id/channel_id/team_id columns. The conversation
# identifier always lands in the single `thread_id` column —
#   DM      -> thread_id == the raw Graph chat id
#   channel -> thread_id == f"{team_id}_{channel_id}" (composite, built in
#              teams.py and written verbatim by chat_indexer).
# `participants` is a JSON string of user IDs. These fakes match that exactly.

TEAMS_DM_CHAT_ID = "19:teams-dm-xyz789@thread.v2"
TEAMS_TEAM_ID = "team-id-111"
TEAMS_CHANNEL_ID = "channel-id-222"
TEAMS_CHANNEL_THREAD_ID = f"{TEAMS_TEAM_ID}_{TEAMS_CHANNEL_ID}"


def _teams_chunk(chunk_id, thread_id, sent_at, text):
    return {
        "chunk_id": chunk_id,
        "file_id": "doc-teams-1",
        "document_id": "doc-teams-1",
        "filename": "teams_conversation.json",
        "file_path": f"teams://{thread_id}",
        "platform": "teams",
        "thread_id": thread_id,
        "sent_at": sent_at,
        "participants": '["alice-uid", "bob-uid"]',
        "section_heading": "Engineering > general",
        "text": text,
        "source_type": "teams_connector",
    }


# 3 Teams DM chunks, deliberately NON-chronological in list order.
FAKE_TEAMS_DM_CHUNKS = [
    _teams_chunk("dm-chunk-3", TEAMS_DM_CHAT_ID, "2025-12-03T16:00:00Z",
                 "DM_SENTINEL_GAMMA [Bob] Sounds good, shipping it."),
    _teams_chunk("dm-chunk-1", TEAMS_DM_CHAT_ID, "2025-12-01T10:00:00Z",
                 "DM_SENTINEL_ALPHA [Alice] Can you review the PR today?"),
    _teams_chunk("dm-chunk-2", TEAMS_DM_CHAT_ID, "2025-12-02T11:30:00Z",
                 "DM_SENTINEL_BETA [Bob] Looking now, one comment incoming."),
]

# 3 Teams channel chunks, NON-chronological in list order.
FAKE_TEAMS_CHANNEL_CHUNKS = [
    _teams_chunk("ch-chunk-2", TEAMS_CHANNEL_THREAD_ID, "2025-12-05T09:15:00Z",
                 "CH_SENTINEL_BETA [Carol] Agenda looks complete."),
    _teams_chunk("ch-chunk-1", TEAMS_CHANNEL_THREAD_ID, "2025-12-04T08:00:00Z",
                 "CH_SENTINEL_ALPHA [Dave] Posting the sprint agenda here."),
    _teams_chunk("ch-chunk-3", TEAMS_CHANNEL_THREAD_ID, "2025-12-06T17:45:00Z",
                 "CH_SENTINEL_GAMMA [Dave] Thanks all, locking the agenda."),
]

# Teams-shaped tool result: the draft_teams_reply tool returns body + mentions.
TEAMS_LLM_RESULT = {
    "body": "Thanks @Alice — I'll review the PR this afternoon and confirm here.",
    "mentions": ["@Alice"],
}


# ─────────────────────────────────────────────────────────────────────────────
# Fake LanceDB store
# ─────────────────────────────────────────────────────────────────────────────
#
# propose_draft_email() uses exactly one store call shape, from
# proposer._scan_outlook_thread():
#
#     store.chunks.search().where(where, prefilter=True).limit(N).to_list()
#
# where `where` is a SQL-ish string:
#     platform = 'outlook' AND thread_id = '<id>'
#
# The fake reproduces that chain and filters its in-memory rows accordingly.


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
        # The proposers always filter on `platform` and `thread_id`. Parse both
        # out of the SQL-ish where clause and match in-memory rows on each.
        # (The Outlook proposer queries thread_id; the Teams proposer queries
        # the same thread_id column for both DMs and channels — see
        # proposer.propose_teams_reply.)
        m_thread = re.search(r"thread_id\s*=\s*'([^']*)'", self._where)
        m_plat = re.search(r"platform\s*=\s*'([^']*)'", self._where)
        wanted_thread = m_thread.group(1) if m_thread else None
        wanted_platform = m_plat.group(1) if m_plat else None
        out = [
            copy.deepcopy(r)
            for r in self._rows
            if r.get("thread_id") == wanted_thread
            and (wanted_platform is None or r.get("platform") == wanted_platform)
        ]
        if self._limit is not None:
            out = out[: self._limit]
        return out


class _FakeChunks:
    def __init__(self, rows):
        self._rows = rows
        # Every where() clause ever issued against this table, in order — lets
        # tests inspect exactly what filter the proposer built.
        self.where_log = []

    def search(self):
        return _FakeQuery(self._rows, self.where_log)


class FakeLanceStore:
    """Minimal stand-in for LanceDBStore — only `.chunks` is needed by Phase 1."""

    def __init__(self, rows):
        self.chunks = _FakeChunks(rows)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path, monkeypatch):
    """
    Real sqlite database in a per-test temp dir.

    qa.database.init_db() opens sqlite3.connect(DB_PATH) where DB_PATH is a
    module-level constant (`DB_PATH = os.getenv("DB_PATH", <data/chat.db>)`)
    resolved at import time. There is no per-call path argument, so we patch
    the module attribute directly. monkeypatch reverts it after the test.
    """
    import qa.database as database

    database.close_db()  # drop any connection a previous test left open
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()    # runs the real migrations, incl. action_proposals
    yield database
    database.close_db()


@pytest.fixture
def store():
    """Fresh fake LanceDB store with the canonical 3-chunk Outlook thread."""
    return FakeLanceStore(FAKE_CHUNKS)


@pytest.fixture
def teams_store():
    """Fake LanceDB store holding both the Teams DM and Teams channel threads."""
    return FakeLanceStore(FAKE_TEAMS_DM_CHUNKS + FAKE_TEAMS_CHANNEL_CHUNKS)


@pytest.fixture
def mock_llm(monkeypatch):
    """
    Patch llm_tool_call where proposer.py looked it up (it did
    `from intelligence.llm import llm_tool_call`, so the live reference is
    `actions.proposer.llm_tool_call`). The AsyncMock records call args, which
    test_chronological_sort_in_context inspects.
    """
    mock = AsyncMock(return_value=copy.deepcopy(LLM_RESULT))
    monkeypatch.setattr("actions.proposer.llm_tool_call", mock)
    return mock


@pytest.fixture
def mock_llm_teams(monkeypatch):
    """Same patch point as mock_llm, but returns the draft_teams_reply shape
    ({body, mentions}). Use this for teams_reply tests."""
    mock = AsyncMock(return_value=copy.deepcopy(TEAMS_LLM_RESULT))
    monkeypatch.setattr("actions.proposer.llm_tool_call", mock)
    return mock


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _propose(store, thread_id=THREAD_ID, **kwargs):
    """Drive the async proposer from a sync test. `store` is positional-first."""
    return asyncio.run(
        propose_draft_email(store, thread_id=thread_id, **kwargs)
    )


def _propose_teams(store, **kwargs):
    """Drive the async Teams proposer from a sync test (keyword-only args)."""
    return asyncio.run(propose_teams_reply(store, **kwargs))


def _is_iso(value: str) -> bool:
    try:
        datetime.fromisoformat(value)
        return True
    except (ValueError, TypeError):
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


def test_propose_creates_valid_proposal(db, store, mock_llm):
    proposal = _propose(
        store, instructions="Acknowledge and confirm", tone="friendly"
    )

    assert isinstance(proposal, ActionProposal)

    # id is a non-empty, UUID-parseable string
    assert isinstance(proposal.id, str) and proposal.id
    assert str(uuid.UUID(proposal.id)) == proposal.id

    assert proposal.type == ActionType.DRAFT_EMAIL
    assert proposal.status == ActionStatus.PROPOSED

    # proposed_payload and current_payload are equal by value but not aliased
    assert proposal.proposed_payload == proposal.current_payload
    assert proposal.proposed_payload is not proposal.current_payload

    assert proposal.proposed_payload["subject"] == "Re: Q4 planning sync"
    assert proposal.proposed_payload["body"]  # non-empty

    # one source_ref per mock chunk
    assert isinstance(proposal.source_refs, list)
    assert len(proposal.source_refs) == 3
    for ref in proposal.source_refs:
        assert ref["chunk_id"]
        assert ref["thread_id"] == THREAD_ID
        assert ref["platform"] == "outlook"
        assert ref["sent_at"]

    # audit log: exactly the "created" entry
    assert len(proposal.audit_log) == 1
    assert proposal.audit_log[0]["action"] == "created"

    assert proposal.executed_at is None
    assert _is_iso(proposal.created_at)


def test_propose_raises_on_unknown_thread(db, store, mock_llm):
    with pytest.raises(ThreadNotFoundError):
        _propose(store, thread_id="does-not-exist")


def test_chronological_sort_in_context(db, store, mock_llm):
    """
    FAKE_CHUNKS is stored as [Nov-3, Nov-1, Nov-2]. The proposer must sort by
    sent_at ascending before building the LLM context. We inspect the captured
    llm_tool_call args and assert the per-chunk SENTINEL tokens appear in
    chronological order in the prompt.
    """
    proposal = _propose(store)

    assert mock_llm.call_count == 1
    sent_messages = mock_llm.call_args.kwargs["messages"]
    prompt = sent_messages[0]["content"]

    pos_alpha = prompt.find("SENTINEL_ALPHA")    # Nov 1
    pos_bravo = prompt.find("SENTINEL_BRAVO")    # Nov 2
    pos_charlie = prompt.find("SENTINEL_CHARLIE")  # Nov 3

    assert -1 not in (pos_alpha, pos_bravo, pos_charlie), "all chunks in prompt"
    assert pos_alpha < pos_bravo < pos_charlie, (
        "thread chunks not in ascending sent_at order in the LLM prompt"
    )

    # source_refs must follow the same chronological order
    ref_order = [r["chunk_id"] for r in proposal.source_refs]
    assert ref_order == ["chunk-1", "chunk-2", "chunk-3"]


def test_storage_roundtrip(db, store, mock_llm):
    """create_proposal -> get_proposal must survive the JSON (de)serialisation
    of every nested field unchanged."""
    proposal = _propose(store)
    create_proposal(proposal)

    fetched = get_proposal(proposal.id)
    assert fetched is not None

    # dataclass equality compares every field, incl. nested dict/list payloads
    assert fetched == proposal
    assert fetched.to_dict() == proposal.to_dict()

    # spot-check the nested fields specifically
    assert fetched.proposed_payload == proposal.proposed_payload
    assert fetched.current_payload == proposal.current_payload
    assert fetched.source_refs == proposal.source_refs
    assert fetched.audit_log == proposal.audit_log


def test_patch_preserves_proposed_payload_immutability(db, store, mock_llm):
    proposal = _propose(store)
    create_proposal(proposal)
    original_proposed = copy.deepcopy(proposal.proposed_payload)

    # ── PATCH #1: legitimate edit of current_payload ─────────────────────────
    edited_current = {
        **proposal.current_payload,
        "subject": "EDITED: Re: Q4 planning sync",
        "body": "Edited body text.",
    }
    audit_1 = proposal.audit_log + [
        make_audit_entry("edited", "system", {"changed_keys": ["body", "subject"]})
    ]
    save_payload_edit(
        proposal.id, edited_current, ActionStatus.EDITED.value, audit_1
    )

    reloaded = get_proposal(proposal.id)
    assert reloaded.current_payload["subject"] == "EDITED: Re: Q4 planning sync"
    assert reloaded.current_payload["body"] == "Edited body text."
    assert reloaded.proposed_payload == original_proposed  # untouched
    assert reloaded.status == ActionStatus.EDITED
    assert len(reloaded.audit_log) == 2
    assert reloaded.audit_log[1]["action"] == "edited"

    # ── PATCH #2: hostile attempt to mutate proposed_payload ─────────────────
    # save_payload_edit() only accepts current_payload — there is no parameter
    # for proposed_payload (structural immutability). Smuggle a "proposed_payload"
    # key inside the current_payload dict and confirm the real proposed_payload
    # column is still untouched afterwards.
    hostile_current = {
        **reloaded.current_payload,
        "proposed_payload": {"subject": "HACKED", "body": "HACKED"},
    }
    audit_2 = reloaded.audit_log + [
        make_audit_entry("edited", "system", {"changed_keys": ["proposed_payload"]})
    ]
    save_payload_edit(
        proposal.id, hostile_current, ActionStatus.EDITED.value, audit_2
    )

    after = get_proposal(proposal.id)
    assert after.proposed_payload == original_proposed, (
        "proposed_payload was mutated — Phase 1 immutability invariant broken"
    )


def test_execute_is_dry_run(db, store, mock_llm, monkeypatch):
    """
    Execute the real route handler and assert it is a pure dry run: status/
    timestamp/audit are updated, payloads are untouched, and ZERO outbound
    HTTP calls are made.

    The codebase performs outbound HTTP exclusively via httpx.AsyncClient
    (see intelligence/llm.py). We patch both AsyncClient.post and the lower
    level AsyncClient.send (post/get/etc. all funnel through send) and assert
    neither was called.
    """
    from api.action_routes import execute_proposal_route

    proposal = _propose(store)
    create_proposal(proposal)
    before = get_proposal(proposal.id)

    post_spy = AsyncMock()
    send_spy = AsyncMock()
    monkeypatch.setattr(httpx.AsyncClient, "post", post_spy)
    monkeypatch.setattr(httpx.AsyncClient, "send", send_spy)

    result = asyncio.run(execute_proposal_route(proposal.id))

    after = get_proposal(proposal.id)
    assert after.status == ActionStatus.EXECUTED
    assert result["status"] == ActionStatus.EXECUTED.value

    assert after.executed_at is not None
    assert _is_iso(after.executed_at)

    last_audit = after.audit_log[-1]
    assert last_audit["action"] == "executed"
    assert last_audit["details"] == {"dry_run": True}

    # payloads must be byte-for-byte identical to pre-execute
    assert after.current_payload == before.current_payload
    assert after.proposed_payload == before.proposed_payload

    # the dry-run guarantee: no outbound HTTP whatsoever
    assert post_spy.call_count == 0, "execute made an outbound httpx POST"
    assert send_spy.call_count == 0, "execute made an outbound httpx request"


def test_dismiss(db, store, mock_llm):
    from api.action_routes import dismiss_proposal_route

    proposal = _propose(store)
    create_proposal(proposal)

    asyncio.run(dismiss_proposal_route(proposal.id))

    dismissed = get_proposal(proposal.id)
    assert dismissed is not None, "dismiss hard-deleted the row (should be soft)"
    assert dismissed.status == ActionStatus.DISMISSED
    assert dismissed.executed_at is None
    assert dismissed.audit_log[-1]["action"] == "dismissed"


def test_list_filtering(db, store, mock_llm):
    # three proposals: one stays proposed, one executed, one dismissed
    p_proposed = _propose(store)
    p_executed = _propose(store)
    p_dismissed = _propose(store)
    for p in (p_proposed, p_executed, p_dismissed):
        create_proposal(p)

    save_status_change(
        p_executed.id,
        ActionStatus.EXECUTED.value,
        p_executed.audit_log + [make_audit_entry("executed", "system", {"dry_run": True})],
        executed_at=now_iso(),
    )
    save_status_change(
        p_dismissed.id,
        ActionStatus.DISMISSED.value,
        p_dismissed.audit_log + [make_audit_entry("dismissed", "system", {})],
    )

    assert len(list_proposals(status=ActionStatus.PROPOSED)) == 1
    assert len(list_proposals(status=ActionStatus.EXECUTED)) == 1
    assert len(list_proposals(status=ActionStatus.DISMISSED)) == 1

    assert len(list_proposals(type_=ActionType.DRAFT_EMAIL)) == 3
    assert len(list_proposals(type_=ActionType.TEAMS_REPLY)) == 0

    assert len(list_proposals(limit=2)) == 2

    # pagination: page 1 and page 2 must not overlap and must cover all 3
    page_1 = list_proposals(limit=2, offset=0)
    page_2 = list_proposals(limit=2, offset=2)
    assert len(page_1) == 2
    assert len(page_2) == 1
    ids_1 = {p.id for p in page_1}
    ids_2 = {p.id for p in page_2}
    assert ids_1.isdisjoint(ids_2)
    assert ids_1 | ids_2 == {p_proposed.id, p_executed.id, p_dismissed.id}


def test_route_handlers_via_testclient(db, store, mock_llm):
    """
    Full HTTP surface via fastapi TestClient — catches DI/wiring bugs (route
    paths, response shapes, status codes) that direct function calls miss.

    The app is built without the real lifespan: only init_action_routes(store)
    is needed, since action_routes.py's sole startup dependency is the module
    `_store` global. The DB is the real temp sqlite from the `db` fixture.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.action_routes import init_action_routes, router

    init_action_routes(store)  # inject the fake LanceDB store
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    # POST /actions/propose/draft-email
    resp = client.post(
        "/actions/propose/draft-email",
        json={"thread_id": THREAD_ID, "instructions": "Confirm", "tone": "friendly"},
    )
    assert resp.status_code == 200, resp.text
    created = resp.json()
    proposal_id = created["id"]
    assert created["status"] == ActionStatus.PROPOSED.value
    assert created["type"] == ActionType.DRAFT_EMAIL.value
    assert created["proposed_payload"]["subject"] == "Re: Q4 planning sync"
    assert len(created["source_refs"]) == 3

    # POST propose with an unknown thread -> 404
    resp_404 = client.post(
        "/actions/propose/draft-email", json={"thread_id": "nope"}
    )
    assert resp_404.status_code == 404

    # GET /actions/proposals/{id}
    resp = client.get(f"/actions/proposals/{proposal_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == proposal_id

    # GET /actions/proposals (list)
    resp = client.get("/actions/proposals")
    assert resp.status_code == 200
    listing = resp.json()
    assert listing["count"] >= 1
    assert any(p["id"] == proposal_id for p in listing["proposals"])

    # PATCH /actions/proposals/{id}
    resp = client.patch(
        f"/actions/proposals/{proposal_id}",
        json={"subject": "Re: Q4 planning sync (edited)"},
    )
    assert resp.status_code == 200
    patched = resp.json()
    assert patched["status"] == ActionStatus.EDITED.value
    assert patched["current_payload"]["subject"] == "Re: Q4 planning sync (edited)"
    # proposed_payload must remain the original LLM output
    assert patched["proposed_payload"]["subject"] == "Re: Q4 planning sync"

    # POST /actions/proposals/{id}/execute
    resp = client.post(f"/actions/proposals/{proposal_id}/execute")
    assert resp.status_code == 200
    executed = resp.json()
    assert executed["status"] == ActionStatus.EXECUTED.value
    assert executed["executed_at"] is not None
    assert executed["audit_log"][-1]["details"] == {"dry_run": True}

    # DELETE /actions/proposals/{id} (soft delete -> dismissed)
    resp = client.delete(f"/actions/proposals/{proposal_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == ActionStatus.DISMISSED.value

    # row still retrievable after soft delete
    resp = client.get(f"/actions/proposals/{proposal_id}")
    assert resp.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# Teams reply slice
# ─────────────────────────────────────────────────────────────────────────────


def test_propose_teams_dm_reply_creates_valid_proposal(db, teams_store, mock_llm_teams):
    proposal = _propose_teams(
        teams_store,
        conversation_kind="dm",
        chat_id=TEAMS_DM_CHAT_ID,
        instructions="Acknowledge and confirm",
        tone="friendly",
    )

    assert isinstance(proposal, ActionProposal)
    assert str(uuid.UUID(proposal.id)) == proposal.id
    assert proposal.type == ActionType.TEAMS_REPLY
    assert proposal.status == ActionStatus.PROPOSED

    payload = proposal.proposed_payload
    assert payload["conversation_kind"] == "dm"
    assert payload["chat_id"] == TEAMS_DM_CHAT_ID
    assert payload["channel_id"] is None
    assert payload["team_id"] is None
    assert payload["body"]  # non-empty
    assert isinstance(payload["mentions"], list)
    assert payload["in_reply_to_message_id"] is None

    # proposed_payload and current_payload equal by value, not aliased
    assert proposal.proposed_payload == proposal.current_payload
    assert proposal.proposed_payload is not proposal.current_payload

    assert len(proposal.source_refs) == 3
    for ref in proposal.source_refs:
        assert ref["platform"] == "teams"
        assert ref["thread_id"] == TEAMS_DM_CHAT_ID
        assert ref["chunk_id"]
        assert ref["sent_at"]

    assert len(proposal.audit_log) == 1
    assert proposal.audit_log[0]["action"] == "created"
    assert proposal.executed_at is None
    assert _is_iso(proposal.created_at)


def test_propose_teams_channel_reply_creates_valid_proposal(db, teams_store, mock_llm_teams):
    proposal = _propose_teams(
        teams_store,
        conversation_kind="channel",
        channel_id=TEAMS_CHANNEL_ID,
        team_id=TEAMS_TEAM_ID,
        instructions="Confirm the agenda",
    )

    assert proposal.type == ActionType.TEAMS_REPLY
    assert proposal.status == ActionStatus.PROPOSED

    payload = proposal.proposed_payload
    assert payload["conversation_kind"] == "channel"
    assert payload["channel_id"] == TEAMS_CHANNEL_ID
    assert payload["team_id"] == TEAMS_TEAM_ID
    assert payload["chat_id"] is None
    assert payload["body"]
    assert isinstance(payload["mentions"], list)
    assert proposal.proposed_payload == proposal.current_payload

    # channel chunks are addressed by the composite thread_id
    assert len(proposal.source_refs) == 3
    for ref in proposal.source_refs:
        assert ref["platform"] == "teams"
        assert ref["thread_id"] == TEAMS_CHANNEL_THREAD_ID


def test_propose_teams_reply_validates_kind_id_combinations(db, teams_store, mock_llm_teams):
    """Every invalid conversation_kind / id combination must raise ValueError."""
    invalid_combinations = [
        # kind="dm" but no chat_id
        dict(conversation_kind="dm"),
        # kind="dm" with both chat_id and channel_id
        dict(conversation_kind="dm", chat_id=TEAMS_DM_CHAT_ID, channel_id=TEAMS_CHANNEL_ID),
        # kind="channel" but no channel_id
        dict(conversation_kind="channel", team_id=TEAMS_TEAM_ID),
        # kind="channel" with chat_id set
        dict(conversation_kind="channel", chat_id=TEAMS_DM_CHAT_ID,
             channel_id=TEAMS_CHANNEL_ID, team_id=TEAMS_TEAM_ID),
        # kind="channel" with channel_id but no team_id
        dict(conversation_kind="channel", channel_id=TEAMS_CHANNEL_ID),
    ]
    for combo in invalid_combinations:
        with pytest.raises(ValueError):
            _propose_teams(teams_store, **combo)


def test_propose_teams_reply_raises_on_unknown_conversation(db, teams_store, mock_llm_teams):
    with pytest.raises(ConversationNotFoundError):
        _propose_teams(
            teams_store,
            conversation_kind="dm",
            chat_id="19:does-not-exist@thread.v2",
        )


def test_teams_dm_chronological_sort_in_context(db, teams_store, mock_llm_teams):
    """
    FAKE_TEAMS_DM_CHUNKS is stored [Dec-3, Dec-1, Dec-2]. The proposer must
    sort by sent_at ascending before building the LLM context. Inspect the
    captured llm_tool_call args and assert the SENTINEL tokens are in order.
    """
    proposal = _propose_teams(
        teams_store, conversation_kind="dm", chat_id=TEAMS_DM_CHAT_ID
    )

    assert mock_llm_teams.call_count == 1
    prompt = mock_llm_teams.call_args.kwargs["messages"][0]["content"]

    pos_alpha = prompt.find("DM_SENTINEL_ALPHA")    # Dec 1
    pos_beta = prompt.find("DM_SENTINEL_BETA")      # Dec 2
    pos_gamma = prompt.find("DM_SENTINEL_GAMMA")    # Dec 3

    assert -1 not in (pos_alpha, pos_beta, pos_gamma), "all chunks in prompt"
    assert pos_alpha < pos_beta < pos_gamma, (
        "Teams DM chunks not in ascending sent_at order in the LLM prompt"
    )

    ref_order = [r["chunk_id"] for r in proposal.source_refs]
    assert ref_order == ["dm-chunk-1", "dm-chunk-2", "dm-chunk-3"]


def test_teams_filter_routing_in_proposer(db, teams_store, mock_llm_teams):
    """
    Catches a proposer that addresses the wrong conversation. Teams chunks have
    no separate chat_id/channel_id columns — the identifier always lives in the
    `thread_id` column — so this asserts the proposer builds the correct
    thread_id VALUE for each kind: the raw chat id for a DM, the composite
    `{team_id}_{channel_id}` for a channel.
    """
    # ── DM ───────────────────────────────────────────────────────────────────
    _propose_teams(teams_store, conversation_kind="dm", chat_id=TEAMS_DM_CHAT_ID)
    dm_filter = teams_store.chunks.where_log[-1]
    assert "platform = 'teams'" in dm_filter
    assert f"thread_id = '{TEAMS_DM_CHAT_ID}'" in dm_filter
    # must NOT be addressing the channel composite
    assert TEAMS_CHANNEL_THREAD_ID not in dm_filter

    # ── Channel ──────────────────────────────────────────────────────────────
    _propose_teams(
        teams_store,
        conversation_kind="channel",
        channel_id=TEAMS_CHANNEL_ID,
        team_id=TEAMS_TEAM_ID,
    )
    channel_filter = teams_store.chunks.where_log[-1]
    assert "platform = 'teams'" in channel_filter
    assert f"thread_id = '{TEAMS_CHANNEL_THREAD_ID}'" in channel_filter
    # the composite is built from team_id + channel_id, not the DM chat id
    assert f"thread_id = '{TEAMS_DM_CHAT_ID}'" not in channel_filter


def test_list_filtering_includes_teams_reply(db, store, teams_store, mock_llm):
    """
    The shared storage layer must filter the new teams_reply type correctly.
    (test_list_filtering itself is left unchanged — it asserts exact counts
    over a draft-email-only set; this is the dedicated mixed-type check.)
    """
    draft_a = _propose(store)
    draft_b = _propose(store)
    teams_one = _propose_teams(
        teams_store, conversation_kind="dm", chat_id=TEAMS_DM_CHAT_ID
    )
    for p in (draft_a, draft_b, teams_one):
        create_proposal(p)

    teams_listed = list_proposals(type_=ActionType.TEAMS_REPLY)
    assert len(teams_listed) == 1
    assert teams_listed[0].id == teams_one.id

    draft_listed = list_proposals(type_=ActionType.DRAFT_EMAIL)
    draft_ids = {p.id for p in draft_listed}
    assert draft_ids == {draft_a.id, draft_b.id}
    assert teams_one.id not in draft_ids


def test_teams_reply_via_testclient(db, teams_store, mock_llm_teams):
    """
    POST /actions/propose/teams-reply end-to-end for both conversation kinds,
    then confirm the new proposals are served by the *existing* shared
    GET /actions/proposals/{id} route with no storage-layer change.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.action_routes import init_action_routes, router

    init_action_routes(teams_store)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    # ── DM ───────────────────────────────────────────────────────────────────
    resp = client.post(
        "/actions/propose/teams-reply",
        json={"conversation_kind": "dm", "chat_id": TEAMS_DM_CHAT_ID,
              "instructions": "Confirm"},
    )
    assert resp.status_code == 200, resp.text
    dm = resp.json()
    assert dm["type"] == ActionType.TEAMS_REPLY.value
    assert dm["status"] == ActionStatus.PROPOSED.value
    assert dm["proposed_payload"]["conversation_kind"] == "dm"
    assert dm["proposed_payload"]["chat_id"] == TEAMS_DM_CHAT_ID
    assert len(dm["source_refs"]) == 3

    # ── Channel ──────────────────────────────────────────────────────────────
    resp = client.post(
        "/actions/propose/teams-reply",
        json={"conversation_kind": "channel", "channel_id": TEAMS_CHANNEL_ID,
              "team_id": TEAMS_TEAM_ID},
    )
    assert resp.status_code == 200, resp.text
    channel = resp.json()
    assert channel["proposed_payload"]["conversation_kind"] == "channel"
    assert channel["proposed_payload"]["channel_id"] == TEAMS_CHANNEL_ID
    assert channel["proposed_payload"]["team_id"] == TEAMS_TEAM_ID

    # invalid kind/id combination -> 422 (Pydantic model_validator)
    resp_422 = client.post(
        "/actions/propose/teams-reply",
        json={"conversation_kind": "dm", "channel_id": TEAMS_CHANNEL_ID},
    )
    assert resp_422.status_code == 422

    # unknown conversation -> 404
    resp_404 = client.post(
        "/actions/propose/teams-reply",
        json={"conversation_kind": "dm", "chat_id": "19:nope@thread.v2"},
    )
    assert resp_404.status_code == 404

    # both new proposals retrievable via the shared GET route
    for created in (dm, channel):
        got = client.get(f"/actions/proposals/{created['id']}")
        assert got.status_code == 200
        assert got.json()["id"] == created["id"]
        assert got.json()["type"] == ActionType.TEAMS_REPLY.value
