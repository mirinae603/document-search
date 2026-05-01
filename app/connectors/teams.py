# connectors/teams.py
# Microsoft Teams connector.
#
# Fetches two kinds of conversations:
#   1. Direct chats  — /me/chats  (1:1 and group DMs)
#   2. Channel posts — /me/joinedTeams → /teams/{id}/channels → messages
#
# Auth:  MSAL confidential client, authorization-code flow.
#        Tokens stored in connector_tokens via token_store.
#        Silent refresh attempted before every API call.
#
# Sync:  delta=True  → only new messages since last run (Graph deltaLink)
#        delta=False → full pull (first-run or forced re-sync)
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import httpx
import msal

from config import (
    CHAT_CHUNK_CHARS,
    CONNECTOR_MAX_MESSAGES_PER_CHAT,
    TEAMS_CLIENT_ID,
    TEAMS_CLIENT_SECRET,
    TEAMS_REDIRECT_URI,
    TEAMS_SCOPES,
    TEAMS_TENANT_ID,
)
from connectors.base import ConnectorBase
from connectors.models import ConversationDoc, MessageTurn
from connectors.token_store import (
    delete_token,
    get_state,
    get_token,
    is_token_expired,
    save_token,
    set_state,
)

logger = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
# Pages Microsoft returns at most 50 messages; we follow nextLink until done
# or until CONNECTOR_MAX_MESSAGES_PER_CHAT is reached.
_MAX_PAGES = CONNECTOR_MAX_MESSAGES_PER_CHAT // 50 + 1


class TeamsConnector(ConnectorBase):

    def __init__(self):
        if not TEAMS_CLIENT_ID:
            logger.warning(
                "TEAMS_CLIENT_ID is not set — Teams connector will not authenticate. "
                "Set TEAMS_CLIENT_ID, TEAMS_CLIENT_SECRET, TEAMS_TENANT_ID in env."
            )
        self._msal = msal.ConfidentialClientApplication(
            TEAMS_CLIENT_ID,
            authority=f"https://login.microsoftonline.com/{TEAMS_TENANT_ID}",
            client_credential=TEAMS_CLIENT_SECRET,
        )

    # ── ConnectorBase implementation ──────────────────────────────────────────

    def platform_name(self) -> str:
        return "teams"

    async def authenticate(self, user_id: str = "default") -> Optional[str]:
        """
        Return a valid access token for *user_id*, refreshing silently if needed.
        Returns None when the user must re-do the OAuth flow.
        """
        row = get_token(user_id, "teams")
        if not row:
            return None

        if not is_token_expired(row):
            return row["access_token"]

        # Attempt silent refresh via MSAL token cache
        try:
            accounts = self._msal.get_accounts()
            result   = self._msal.acquire_token_silent(
                scopes  = TEAMS_SCOPES,
                account = accounts[0] if accounts else None,
            )
            if result and "access_token" in result:
                save_token(
                    user_id       = user_id,
                    platform      = "teams",
                    access_token  = result["access_token"],
                    expires_in    = result.get("expires_in", 3600),
                    refresh_token = result.get("refresh_token", row.get("refresh_token")),
                    scopes        = result.get("scope", "").split(),
                    account       = result.get("account") or json.loads(row.get("account_json", "{}")),
                )
                logger.info(f"Teams token silently refreshed for user={user_id}")
                return result["access_token"]
        except Exception as e:
            logger.warning(f"Silent refresh failed for user={user_id}: {e}")

        logger.warning(
            f"Teams token expired and silent refresh failed for user={user_id}. "
            "Re-authentication required."
        )
        return None

    async def fetch_conversations(
        self,
        delta:   bool = True,
        user_id: str  = "default",
    ) -> List[ConversationDoc]:
        """Fetch all DM chats and channel conversations for *user_id*."""
        token = await self.authenticate(user_id)
        if not token:
            raise RuntimeError(
                f"No valid Teams token for user={user_id}. "
                "Visit /connector/teams/oauth/start to authenticate."
            )

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
        }
        conversations: List[ConversationDoc] = []

        async with httpx.AsyncClient(timeout=60) as client:
            dm_docs = await self._fetch_direct_chats(client, headers, user_id, delta)
            conversations.extend(dm_docs)

            ch_docs = await self._fetch_channel_conversations(client, headers, user_id, delta)
            conversations.extend(ch_docs)

        logger.info(
            f"Teams connector: fetched {len(conversations)} conversations "
            f"for user={user_id} (delta={delta})"
        )
        return conversations

    # ── OAuth helpers (called by connector_routes) ────────────────────────────

    def get_auth_url(self, state: str = "") -> str:
        """Build the Microsoft login URL the user should be redirected to."""
        return self._msal.get_authorization_request_url(
            TEAMS_SCOPES,
            redirect_uri = TEAMS_REDIRECT_URI,
            state        = state,
        )

    async def exchange_code(self, code: str, user_id: str) -> bool:
        """
        Exchange the OAuth authorization code for tokens.
        Returns True on success, False on failure.
        Called by the OAuth callback route.
        """
        try:
            result = self._msal.acquire_token_by_authorization_code(
                code,
                scopes       = TEAMS_SCOPES,
                redirect_uri = TEAMS_REDIRECT_URI,
            )
            if "access_token" not in result:
                logger.error(
                    f"Token exchange failed: {result.get('error_description', result)}"
                )
                return False

            save_token(
                user_id       = user_id,
                platform      = "teams",
                access_token  = result["access_token"],
                expires_in    = result.get("expires_in", 3600),
                refresh_token = result.get("refresh_token"),
                scopes        = result.get("scope", "").split(),
                account       = result.get("account", {}),
            )
            logger.info(f"Teams OAuth complete for user={user_id}")
            return True
        except Exception as e:
            logger.error(f"exchange_code failed: {e}", exc_info=True)
            return False

    # ── Direct Chats (1:1 and group DMs) ─────────────────────────────────────

    async def _fetch_direct_chats(
        self,
        client:  httpx.AsyncClient,
        headers: dict,
        user_id: str,
        delta:   bool,
    ) -> List[ConversationDoc]:
        docs: List[ConversationDoc] = []
        url = f"{GRAPH}/me/chats?$expand=members&$top=50"

        while url:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 403:
                logger.warning("Chat.Read permission not granted — skipping direct chats")
                break
            if resp.status_code != 200:
                logger.warning(f"GET /me/chats → {resp.status_code}: {resp.text[:200]}")
                break

            data  = resp.json()
            chats = data.get("value", [])

            for chat in chats:
                chat_id   = chat["id"]
                chat_type = chat.get("chatType", "")

                members       = chat.get("members", [])
                participants  = []
                display_names: Dict[str, str] = {}
                for m in members:
                    uid  = m.get("userId") or m.get("id", "")
                    name = m.get("displayName", uid)
                    if uid:
                        participants.append(uid)
                        display_names[uid] = name

                messages, delta_link = await self._fetch_chat_messages(
                    client, headers, chat_id, user_id, delta
                )
                if not messages:
                    continue

                if chat_type == "oneOnOne":
                    names       = sorted(display_names.values())
                    channel_name = f"DM: {' ↔ '.join(names)}"
                elif chat_type == "group":
                    topic       = chat.get("topic", "").strip()
                    channel_name = topic or f"Group: {', '.join(sorted(display_names.values()))}"
                else:
                    channel_name = chat.get("topic") or chat_type or "Chat"

                docs.append(ConversationDoc(
                    platform      = "teams",
                    chat_id       = chat_id,
                    participants  = participants,
                    display_names = display_names,
                    channel_name  = channel_name,
                    messages      = messages,
                    fetched_at    = datetime.now(timezone.utc).isoformat(),
                    delta_link    = delta_link,
                ))

            url = data.get("@odata.nextLink")

        return docs

    async def _fetch_chat_messages(
        self,
        client:  httpx.AsyncClient,
        headers: dict,
        chat_id: str,
        user_id: str,
        delta:   bool,
    ) -> Tuple[List[MessageTurn], Optional[str]]:
        """
        Fetch messages for one chat.  Follows @odata.nextLink pages.
        If delta=True and a deltaLink was stored from the last sync,
        starts from there so only new messages are returned.
        """
        delta_state_key = f"delta:chat:{chat_id}"
        stored_delta    = get_state(user_id, "teams", delta_state_key) if delta else None
        start_url       = stored_delta or f"{GRAPH}/me/chats/{chat_id}/messages?$top=50"

        messages:   List[MessageTurn] = []
        new_delta:  Optional[str]     = None
        page_count  = 0

        url = start_url
        while url and page_count < _MAX_PAGES:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 404:
                break
            if resp.status_code != 200:
                logger.debug(f"chat messages {chat_id}: {resp.status_code}")
                break

            data = resp.json()
            for msg in data.get("value", []):
                turn = _parse_message(msg)
                if turn:
                    messages.append(turn)

            new_delta = data.get("@odata.deltaLink", new_delta)
            url       = data.get("@odata.nextLink")
            page_count += 1

        if new_delta:
            set_state(user_id, "teams", delta_state_key, new_delta)

        messages.sort(key=lambda m: m.sent_at)
        return messages, new_delta

    # ── Channel Messages ──────────────────────────────────────────────────────

    async def _fetch_channel_conversations(
        self,
        client:  httpx.AsyncClient,
        headers: dict,
        user_id: str,
        delta:   bool,
    ) -> List[ConversationDoc]:
        docs: List[ConversationDoc] = []

        resp = await client.get(f"{GRAPH}/me/joinedTeams", headers=headers)
        if resp.status_code != 200:
            logger.warning(f"joinedTeams → {resp.status_code}")
            return docs

        teams = resp.json().get("value", [])
        for team in teams:
            team_id   = team["id"]
            team_name = team["displayName"]

            ch_resp = await client.get(
                f"{GRAPH}/teams/{team_id}/channels", headers=headers
            )
            if ch_resp.status_code != 200:
                continue

            for channel in ch_resp.json().get("value", []):
                channel_id   = channel["id"]
                channel_name = channel["displayName"]

                messages, _ = await self._fetch_channel_messages(
                    client, headers, team_id, channel_id, user_id, delta
                )
                if not messages:
                    continue

                participants  = list({m.sender_id for m in messages if m.sender_id})
                display_names = {
                    m.sender_id: m.sender_name
                    for m in messages
                    if m.sender_id
                }

                docs.append(ConversationDoc(
                    platform      = "teams",
                    chat_id       = f"{team_id}_{channel_id}",
                    participants  = participants,
                    display_names = display_names,
                    channel_name  = f"{team_name} › {channel_name}",
                    messages      = messages,
                    fetched_at    = datetime.now(timezone.utc).isoformat(),
                ))

        return docs

    async def _fetch_channel_messages(
        self,
        client:     httpx.AsyncClient,
        headers:    dict,
        team_id:    str,
        channel_id: str,
        user_id:    str,
        delta:      bool,
    ) -> Tuple[List[MessageTurn], Optional[str]]:
        delta_key  = f"delta:channel:{team_id}:{channel_id}"
        stored     = get_state(user_id, "teams", delta_key) if delta else None
        start_url  = stored or (
            f"{GRAPH}/teams/{team_id}/channels/{channel_id}/messages?$top=50"
        )

        messages:  List[MessageTurn] = []
        new_delta: Optional[str]     = None
        page_count = 0
        url        = start_url

        while url and page_count < _MAX_PAGES:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                break
            data = resp.json()
            for msg in data.get("value", []):
                turn = _parse_message(msg)
                if turn:
                    messages.append(turn)

            new_delta = data.get("@odata.deltaLink", new_delta)
            url       = data.get("@odata.nextLink")
            page_count += 1

        if new_delta:
            set_state(user_id, "teams", delta_key, new_delta)

        messages.sort(key=lambda m: m.sent_at)
        return messages, new_delta


# ── Message parser (module-level, reused by both chat and channel paths) ──────

_HTML_TAG_RE    = re.compile(r"<[^>]+>")
_WHITESPACE_RE  = re.compile(r"\s+")

def _parse_message(msg: dict) -> Optional[MessageTurn]:
    """
    Convert a raw Graph API message dict into a MessageTurn.
    Returns None for system events and empty messages.
    """
    body         = msg.get("body") or {}
    content_type = body.get("contentType", "text")
    raw          = (body.get("content") or "").strip()

    if not raw or raw == "<systemEventMessage/>":
        return None

    # Strip HTML markup for HTML-type messages
    if content_type == "html":
        raw = _HTML_TAG_RE.sub(" ", raw)
        raw = _WHITESPACE_RE.sub(" ", raw).strip()

    if not raw:
        return None

    sender    = msg.get("from") or {}
    user_info = sender.get("user") or sender.get("application") or {}
    sender_id  = user_info.get("id", "unknown")
    sender_name = user_info.get("displayName", "Unknown")
    sent_at    = msg.get("createdDateTime", datetime.now(timezone.utc).isoformat())

    return MessageTurn(
        message_id  = msg.get("id", ""),
        sender_id   = sender_id,
        sender_name = sender_name,
        content     = raw,
        sent_at     = sent_at,
        thread_id   = msg.get("replyToId"),
    )
