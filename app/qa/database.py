# qa/database.py
import logging
import os
import sqlite3
from pathlib import Path
from typing import Optional

logger  = logging.getLogger(__name__)

_DB_DIR = Path(__file__).parent.parent / "data"
_DB_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = os.getenv("DB_PATH", str(_DB_DIR / "chat.db"))

_conn: Optional[sqlite3.Connection] = None


def get_db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        raise RuntimeError("DB not initialized — call init_db() first")
    return _conn


def init_db():
    global _conn
    try:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")   # safe for concurrent reads
        _conn.execute("PRAGMA foreign_keys=ON")
        _run_migrations()
        logger.info(f"✓ DB ready — {DB_PATH}")
    except Exception as e:
        logger.error(f"DB init failed: {e}")
        raise


def close_db():
    global _conn
    if _conn:
        _conn.close()
        _conn = None


def _run_migrations():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id            TEXT PRIMARY KEY,
            user_id       TEXT NOT NULL,
            title         TEXT NOT NULL DEFAULT 'New Conversation',
            message_count INTEGER NOT NULL DEFAULT 0,
            deleted       INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS messages (
            id         TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            role       TEXT NOT NULL,
            content    TEXT NOT NULL,
            sources    TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_sessions_user
            ON sessions(user_id, updated_at);

        CREATE INDEX IF NOT EXISTS idx_messages_session
            ON messages(session_id, created_at);

        -- ── Connector: OAuth token storage ──────────────────────────────────
        CREATE TABLE IF NOT EXISTS connector_tokens (
            id            TEXT PRIMARY KEY,
            user_id       TEXT NOT NULL,
            platform      TEXT NOT NULL,
            access_token  TEXT NOT NULL,
            refresh_token TEXT,
            expires_at    TEXT NOT NULL,
            scopes        TEXT NOT NULL DEFAULT '[]',
            account_json  TEXT NOT NULL DEFAULT '{}',
            updated_at    TEXT NOT NULL,
            UNIQUE(user_id, platform)
        );

        CREATE INDEX IF NOT EXISTS idx_connector_tokens_user
            ON connector_tokens(user_id, platform);

        -- ── Connector: arbitrary per-user key-value state ───────────────────
        -- Used for: last_sync timestamps, delta links, sync status
        CREATE TABLE IF NOT EXISTS connector_state (
            user_id    TEXT NOT NULL,
            platform   TEXT NOT NULL,
            key        TEXT NOT NULL,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, platform, key)
        );
    """)
    db.commit()
    logger.info("✓ Migrations complete")
