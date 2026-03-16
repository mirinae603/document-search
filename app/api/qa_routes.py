# api/qa_routes.py
import logging
from typing import Optional, List

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from qa.agent      import stream_chat
from qa.repository import (
    create_session, list_sessions,
    get_session_with_messages, delete_session,
)

logger   = logging.getLogger(__name__)
router   = APIRouter(prefix="/qa", tags=["QA"])
DEV_USER = "dev-user-001"


class NewSessionRequest(BaseModel):
    title: Optional[str] = None

class ChatRequest(BaseModel):
    question: str
    file_ids: Optional[List[str]] = None
    top_k:    int = 8


@router.post("/sessions/new")
async def new_session(req: NewSessionRequest = NewSessionRequest()):
    session_id = create_session(
        user_id = DEV_USER,
        title   = req.title or "New Conversation",
    )
    return {"session_id": session_id}


@router.get("/sessions")
async def list_user_sessions():
    sessions = list_sessions(user_id=DEV_USER)
    return {"sessions": sessions}


@router.get("/sessions/{session_id}")
async def get_session(session_id: str):
    session = get_session_with_messages(session_id, user_id=DEV_USER)
    if not session:
        raise HTTPException(404, "Session not found")
    return session


@router.post("/sessions/{session_id}/chat")
async def chat(session_id: str, req: ChatRequest):
    session = get_session_with_messages(session_id, user_id=DEV_USER)
    if not session:
        raise HTTPException(404, "Session not found")
    return StreamingResponse(
        stream_chat(
            question   = req.question,
            user_id    = DEV_USER,
            session_id = session_id,
            file_ids   = req.file_ids,
            top_k      = req.top_k,
        ),
        media_type = "text/event-stream",
        headers    = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.delete("/sessions/{session_id}")
async def remove_session(session_id: str):
    session = get_session_with_messages(session_id, user_id=DEV_USER)
    if not session:
        raise HTTPException(404, "Session not found")
    delete_session(session_id, user_id=DEV_USER)
    return {"status": "deleted"}
