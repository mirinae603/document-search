# connectors/token_store.py
# Persistent storage for:
#   - OAuth tokens (access + refresh) in connector_tokens
#   - Arbitrary per-user key/value state in connector_state
#     (delta links, last-sync timestamps, sync status, etc.)
#
# Both tables are created by qa/database.py migrations so this
# module only needs get_db() — no separate init step required.
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from qa.database import get_db

logger = logging.getLogger(__name__)

_BUFFER_SECONDS = 300   # refresh token 5 min before actual expiry


# ── Token operations ──────────────────────────────────────────────────────────

def save_token(
    user_id:       str,
    platform:      str,
    access_token:  str,
    expires_in:    int,
    refresh_token: Optional[str] = None,
    scopes:        list           = None,
    account:       dict           = None,
) -> None:
    """Upsert an OAuth token record for (user_id, platform)."""
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    ).isoformat()

    db = get_db()
    db.execute(
        """
        INSERT INTO connector_tokens
            (id, user_id, platform, access_token, refresh_token,
             expires_at, scopes, account_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, platform) DO UPDATE SET
            access_token  = excluded.access_token,
            refresh_token = COALESCE(excluded.refresh_token, connector_tokens.refresh_token),
            expires_at    = excluded.expires_at,
            scopes        = excluded.scopes,
            account_json  = excluded.account_json,
            updated_at    = excluded.updated_at
        """,
        [
            str(uuid.uuid4()),
            user_id,
            platform,
            access_token,
            refresh_token,
            expires_at,
            json.dumps(scopes or []),
            json.dumps(account or {}),
            datetime.now(timezone.utc).isoformat(),
        ],
    )
    db.commit()
    logger.debug(f"Token saved: user={user_id} platform={platform} expires={expires_at}")


def get_token(user_id: str, platform: str) -> Optional[Dict]:
    """Return the stored token row, or None if not found."""
    db  = get_db()
    row = db.execute(
        "SELECT * FROM connector_tokens WHERE user_id = ? AND platform = ?",
        [user_id, platform],
    ).fetchone()
    return dict(row) if row else None


def is_token_expired(token_row: Dict) -> bool:
    """True if the token is expired (or will expire within BUFFER_SECONDS)."""
    try:
        expires_at = datetime.fromisoformat(token_row["expires_at"])
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        threshold = datetime.now(timezone.utc) + timedelta(seconds=_BUFFER_SECONDS)
        return expires_at <= threshold
    except Exception:
        return True   # treat parse errors as expired


def delete_token(user_id: str, platform: str) -> None:
    """Remove stored token (user disconnected)."""
    db = get_db()
    db.execute(
        "DELETE FROM connector_tokens WHERE user_id = ? AND platform = ?",
        [user_id, platform],
    )
    db.commit()


def has_token(user_id: str, platform: str) -> bool:
    """Return True if a token exists (may be expired)."""
    return get_token(user_id, platform) is not None


# ── Connector state operations ────────────────────────────────────────────────

def set_state(user_id: str, platform: str, key: str, value: Any) -> None:
    """Persist an arbitrary value under (user_id, platform, key)."""
    db = get_db()
    db.execute(
        """
        INSERT INTO connector_state (user_id, platform, key, value, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id, platform, key) DO UPDATE SET
            value      = excluded.value,
            updated_at = excluded.updated_at
        """,
        [
            user_id,
            platform,
            key,
            json.dumps(value),
            datetime.now(timezone.utc).isoformat(),
        ],
    )
    db.commit()


def get_state(user_id: str, platform: str, key: str, default: Any = None) -> Any:
    """Retrieve a stored value, returning *default* if not found."""
    db  = get_db()
    row = db.execute(
        "SELECT value FROM connector_state WHERE user_id=? AND platform=? AND key=?",
        [user_id, platform, key],
    ).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except Exception:
        return row["value"]


def delete_state(user_id: str, platform: str, key: str) -> None:
    db = get_db()
    db.execute(
        "DELETE FROM connector_state WHERE user_id=? AND platform=? AND key=?",
        [user_id, platform, key],
    )
    db.commit()


def clear_platform_state(user_id: str, platform: str) -> None:
    """Wipe all state for a platform (used on disconnect)."""
    db = get_db()
    db.execute(
        "DELETE FROM connector_state WHERE user_id=? AND platform=?",
        [user_id, platform],
    )
    db.commit()
