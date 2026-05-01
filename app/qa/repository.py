# qa/repository.py
import json
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional, List

from qa.database import get_db

logger = logging.getLogger(__name__)


def create_session(user_id: str, title: str = "New Conversation") -> str:
    session_id = str(uuid.uuid4())
    db = get_db()
    db.execute(
        """INSERT INTO sessions (id, user_id, title, created_at, updated_at, message_count)
           VALUES (?, ?, ?, ?, ?, 0)""",
        [session_id, user_id, title, _now(), _now()]
    )
    db.commit()
    return session_id


def list_sessions(user_id: str) -> List[dict]:
    db   = get_db()
    rows = db.execute(
        """SELECT id, title, message_count, updated_at
           FROM sessions
           WHERE user_id = ? AND deleted = 0
           ORDER BY updated_at DESC LIMIT 50""",
        [user_id]
    ).fetchall()
    return [dict(r) for r in rows]


def get_session_with_messages(session_id: str, user_id: str) -> Optional[dict]:
    db  = get_db()
    row = db.execute(
        "SELECT * FROM sessions WHERE id = ? AND user_id = ? AND deleted = 0",
        [session_id, user_id]
    ).fetchone()
    if not row:
        return None
    msgs = db.execute(
        """SELECT id, role, content, sources, created_at
           FROM messages WHERE session_id = ?
           ORDER BY created_at ASC""",
        [session_id]
    ).fetchall()
    session             = dict(row)
    session["messages"] = [dict(m) for m in msgs]
    return session


def delete_session(session_id: str, user_id: str):
    db = get_db()
    db.execute(
        "UPDATE sessions SET deleted = 1 WHERE id = ? AND user_id = ?",
        [session_id, user_id]
    )
    db.commit()


def save_message(session_id: str, role: str, content: str, sources: list = None) -> str:
    db     = get_db()
    msg_id = str(uuid.uuid4())
    db.execute(
        """INSERT INTO messages (id, session_id, role, content, sources, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        [msg_id, session_id, role, content, json.dumps(sources or []), _now()]
    )
    db.execute(
        """UPDATE sessions SET message_count = message_count + 1, updated_at = ?
           WHERE id = ?""",
        [_now(), session_id]
    )
    db.commit()
    return msg_id


def update_session_title(session_id: str, title: str):
    db = get_db()
    db.execute("UPDATE sessions SET title = ? WHERE id = ?", [title, session_id])
    db.commit()


def _now():
    return datetime.now(timezone.utc).isoformat()
