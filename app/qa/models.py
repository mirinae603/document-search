# qa/models.py
# All Pydantic schemas and DB table definitions for the QA chat system.
from __future__ import annotations
from datetime import datetime
from enum import Enum
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field
import uuid


# ── Enums ─────────────────────────────────────────────────────────────────────

class MessageRole(str, Enum):
    USER      = "user"
    ASSISTANT = "assistant"
    SYSTEM    = "system"

class QAMode(str, Enum):
    SCOPED  = "scoped"   # selected file_ids only  → /qa/ask
    DATASET = "dataset"  # entire corpus            → /qa/chat

class ConversationStatus(str, Enum):
    ACTIVE   = "active"
    ARCHIVED = "archived"
    DELETED  = "deleted"


# ── DB Row Models (what gets stored) ──────────────────────────────────────────

class ConversationRow(BaseModel):
    id:           str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id:      str
    title:        str = "New Conversation"
    mode:         QAMode = QAMode.DATASET
    file_ids:     str = "[]"          # JSON string — file scope for SCOPED mode
    status:       ConversationStatus = ConversationStatus.ACTIVE
    message_count:int = 0
    created_at:   str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at:   str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    summary:      str = ""            # rolling compression of old turns
    summary_at_turn: int = 0         # which turn was last summarized

class MessageRow(BaseModel):
    id:              str = Field(default_factory=lambda: str(uuid.uuid4()))
    conversation_id: str
    user_id:         str
    role:            MessageRole
    content:         str
    turn_index:      int = 0
    tokens_used:     int = 0
    model:           str = ""
    qa_mode:         QAMode = QAMode.DATASET
    retrieval_tier:  str = ""         # A / B / C from _fetch_chunks
    created_at:      str = Field(default_factory=lambda: datetime.utcnow().isoformat())

class CitationRow(BaseModel):
    id:              str = Field(default_factory=lambda: str(uuid.uuid4()))
    message_id:      str
    conversation_id: str
    user_id:         str
    file_id:         str
    filename:        str
    chunk_id:        str
    chunk_index:     int
    score:           float
    excerpt:         str              # first 200 chars of chunk text
    created_at:      str = Field(default_factory=lambda: datetime.utcnow().isoformat())

class SessionRow(BaseModel):
    session_id:      str
    user_id:         str
    conversation_id: str
    created_at:      str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    last_active:     str = Field(default_factory=lambda: datetime.utcnow().isoformat())


# ── API Request / Response Models ─────────────────────────────────────────────

class ChatRequest(BaseModel):
    """
    Universal request for both /qa/ask (scoped) and /qa/chat (dataset-wide).
    session_id  : client holds this, server validates ownership
    conversation_id : None = start new conversation
    file_ids    : required for scoped mode, ignored for dataset mode
    """
    question:        str
    session_id:      str
    conversation_id: Optional[str] = None
    file_ids:        Optional[List[str]] = None   # None = dataset-wide
    top_k:           int = Field(5, ge=1, le=20)

class ScopedQARequest(BaseModel):
    """Legacy-compatible request for /qa/ask — scoped to selected files."""
    question:             str
    file_ids:             List[str]
    top_k:                int      = Field(5, ge=1, le=20)
    session_id:           Optional[str] = None
    conversation_id:      Optional[str] = None
    # Deprecated — ignored if session_id present, kept for backward compat
    conversation_history: List[Dict] = Field(default_factory=list)

class SourcesRequest(BaseModel):
    question: str
    file_ids: Optional[List[str]] = None
    top_k:    int = Field(5, ge=1, le=20)

class ConversationListResponse(BaseModel):
    conversations: List[Dict[str, Any]]
    total:         int

class MessageListResponse(BaseModel):
    messages:   List[Dict[str, Any]]
    total:      int
    has_more:   bool

class CitationResponse(BaseModel):
    file_id:     str
    filename:    str
    chunk_id:    str
    chunk_index: int
    score:       float
    excerpt:     str
