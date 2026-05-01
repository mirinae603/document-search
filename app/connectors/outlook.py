
from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import httpx
import msal

from config import (
    CONNECTOR_MAX_EMAILS_PER_THREAD,
    CONNECTOR_MAX_THREADS,
    TEAMS_CLIENT_ID ,
    TEAMS_CLIENT_SECRET,
    TEAMS_REDIRECT_URI,
    TEAMS_SCOPES,
    OUTLOOK_SYNC_FOLDERS,
    TEAMS_TENANT_ID,
)
from connectors.base import ConnectorBase
from connectors.models import ConversationDoc, MessageTurn
from connectors.token_store import (
    get_state,
    get_token,
    is_token_expired,
    save_token,
    set_state,
)

logger = logging.getLogger(__name__)
OUTLOOK_CLIENT_ID =TEAMS_CLIENT_ID
OUTLOOK_CLIENT_SECRET = TEAMS_CLIENT_SECRET
OUTLOOK_REDIRECT_URI = TEAMS_REDIRECT_URI
OUTLOOK_TENANT_ID = TEAMS_TENANT_ID
OUTLOOK_SCOPES = TEAMS_SCOPES
GRAPH = "https://graph.microsoft.com/v1.0"

class OutlookConnector(ConnectorBase):

    def __init__(self):
        if not OUTLOOK_CLIENT_ID:
            logger.warning(
                "OUTLOOK_CLIENT_ID is not set — Outlook connector will not authenticate. "
                "Set OUTLOOK_CLIENT_ID / OUTLOOK_CLIENT_SECRET / OUTLOOK_TENANT_ID in env."
            )
        self._msal = msal.ConfidentialClientApplication(
            OUTLOOK_CLIENT_ID,
            authority=f"https://login.microsoftonline.com/{OUTLOOK_TENANT_ID}",
            client_credential=OUTLOOK_CLIENT_SECRET,
        )

    # ── ConnectorBase ─────────────────────────────────────────────────────────

    def platform_name(self) -> str:
        return "outlook"

    async def authenticate(self, user_id: str = "default") -> Optional[str]:
        """
        Return a valid, non-expired access token for *user_id*.
        Silently refreshes via MSAL if expired.
        Returns None when re-auth is required.
        """
        row = get_token(user_id, "outlook")
        if not row:
            return None

        if not is_token_expired(row):
            return row["access_token"]

        try:
            accounts = self._msal.get_accounts()
            result   = self._msal.acquire_token_silent(
                scopes  = OUTLOOK_SCOPES,
                account = accounts[0] if accounts else None,
            )
            if result and "access_token" in result:
                save_token(
                    user_id       = user_id,
                    platform      = "outlook",
                    access_token  = result["access_token"],
                    expires_in    = result.get("expires_in", 3600),
                    refresh_token = result.get("refresh_token", row.get("refresh_token")),
                    scopes        = result.get("scope", "").split(),
                    account       = result.get("account") or json.loads(
                        row.get("account_json", "{}")
                    ),
                )
                logger.info(f"Outlook token silently refreshed for user={user_id}")
                return result["access_token"]
        except Exception as e:
            logger.warning(f"Silent refresh failed for user={user_id}: {e}")

        logger.warning(
            f"Outlook token expired and silent refresh failed for user={user_id}. "
            "Re-authentication required."
        )
        return None

    async def fetch_conversations(
        self,
        delta:   bool = True,
        user_id: str  = "default",
    ) -> List[ConversationDoc]:
        """
        Fetch email threads from configured folders.
        Each unique conversationId becomes one ConversationDoc.
        """
        token = await self.authenticate(user_id)
        if not token:
            raise RuntimeError(
                f"No valid Outlook token for user={user_id}. "
                "Visit /connector/outlook/oauth/start to authenticate."
            )

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
        }

        # Collect raw emails from all configured folders
        raw_emails: List[dict] = []

        async with httpx.AsyncClient(timeout=60) as client:
            folder_ids = await self._resolve_folder_ids(
                client, headers, OUTLOOK_SYNC_FOLDERS
            )

            for folder_id, folder_name in folder_ids:
                emails, new_delta = await self._fetch_folder_emails(
                    client, headers, folder_id, folder_name, user_id, delta
                )
                raw_emails.extend(emails)

        # Group by conversationId → one ConversationDoc per thread
        conversations = self._group_into_threads(raw_emails)

        logger.info(
            f"Outlook connector: {len(raw_emails)} emails → "
            f"{len(conversations)} threads for user={user_id} (delta={delta})"
        )
        return conversations

    # ── OAuth helpers (called by connector_routes) ────────────────────────────

    def get_auth_url(self, state: str = "") -> str:
        return self._msal.get_authorization_request_url(
            OUTLOOK_SCOPES,
            redirect_uri = OUTLOOK_REDIRECT_URI,
            state        = state,
        )

    async def exchange_code(self, code: str, user_id: str) -> bool:
        """Exchange OAuth auth code for tokens. Returns True on success."""
        try:
            result = self._msal.acquire_token_by_authorization_code(
                code,
                scopes       = OUTLOOK_SCOPES,
                redirect_uri = OUTLOOK_REDIRECT_URI,
            )
            if "access_token" not in result:
                logger.error(
                    f"Token exchange failed: {result.get('error_description', result)}"
                )
                return False

            save_token(
                user_id       = user_id,
                platform      = "outlook",
                access_token  = result["access_token"],
                expires_in    = result.get("expires_in", 3600),
                refresh_token = result.get("refresh_token"),
                scopes        = result.get("scope", "").split(),
                account       = result.get("account", {}),
            )
            logger.info(f"Outlook OAuth complete for user={user_id}")
            return True
        except Exception as e:
            logger.error(f"exchange_code failed: {e}", exc_info=True)
            return False

    # ── Folder resolution ─────────────────────────────────────────────────────

    async def _resolve_folder_ids(
        self,
        client:  httpx.AsyncClient,
        headers: dict,
        folder_names: List[str],
    ) -> List[Tuple[str, str]]:
        """
        Resolve human folder names (e.g. "Inbox") to Graph folder IDs.
        Returns list of (folder_id, folder_display_name).
        Falls back to "inbox" and "sentitems" well-known IDs if the API fails.
        """
        resp = await client.get(
            f"{GRAPH}/me/mailFolders?$top=50", headers=headers
        )
        if resp.status_code != 200:
            logger.warning(
                f"mailFolders list failed ({resp.status_code}) — "
                "falling back to well-known IDs"
            )
            return [("inbox", "Inbox"), ("sentitems", "Sent Items")]

        all_folders = resp.json().get("value", [])
        name_map: Dict[str, Tuple[str, str]] = {
            f["displayName"].lower(): (f["id"], f["displayName"])
            for f in all_folders
        }

        resolved = []
        for name in folder_names:
            match = name_map.get(name.lower())
            if match:
                resolved.append(match)
            else:
                # Try Graph well-known ID (inbox, drafts, sentitems, deleteditems)
                well_known = name.lower().replace(" ", "")
                logger.warning(
                    f"Folder '{name}' not found — trying well-known ID '{well_known}'"
                )
                resolved.append((well_known, name))

        return resolved or [("inbox", "Inbox"), ("sentitems", "Sent Items")]

    # ── Email fetching ────────────────────────────────────────────────────────

    async def _fetch_folder_emails(
        self,
        client:      httpx.AsyncClient,
        headers:     dict,
        folder_id:   str,
        folder_name: str,
        user_id:     str,
        delta:       bool,
    ) -> Tuple[List[dict], Optional[str]]:
        """
        Fetch all emails from one folder.
        Uses delta endpoint when a stored deltaLink is available.
        Annotates each email dict with _folder_name for later grouping.

        Graph fields requested:
          id, subject, conversationId, sender, toRecipients, ccRecipients,
          receivedDateTime, sentDateTime, bodyPreview, body, isRead, isDraft
        """
        select = (
            "id,subject,conversationId,sender,from,"
            "toRecipients,ccRecipients,receivedDateTime,sentDateTime,"
            "bodyPreview,body,isRead,isDraft,internetMessageId"
        )
        delta_key    = f"delta:folder:{folder_id}"
        stored_delta = get_state(user_id, "outlook", delta_key) if delta else None

        if stored_delta:
            start_url = stored_delta
        else:
            start_url = (
                f"{GRAPH}/me/mailFolders/{folder_id}/messages/delta"
                f"?$select={select}&$top=50"
            )

        emails:    List[dict]     = []
        new_delta: Optional[str]  = None
        page_count = 0
        url        = start_url

        while url and page_count < (CONNECTOR_MAX_THREADS * 2):
            resp = await client.get(url, headers=headers)
            if resp.status_code == 404:
                logger.warning(f"Folder {folder_id!r} not found — skipping")
                break
            if resp.status_code != 200:
                logger.warning(
                    f"folder={folder_id} page={page_count}: "
                    f"HTTP {resp.status_code}"
                )
                break

            data = resp.json()
            for msg in data.get("value", []):
                # Skip drafts — they aren't sent conversations
                if msg.get("isDraft"):
                    continue
                msg["_folder_name"] = folder_name
                emails.append(msg)

                if len(emails) >= CONNECTOR_MAX_THREADS * CONNECTOR_MAX_EMAILS_PER_THREAD:
                    logger.info(f"folder={folder_id}: hit email cap, stopping")
                    url = None
                    break

            new_delta = data.get("@odata.deltaLink", new_delta)
            url       = data.get("@odata.nextLink") if url else None
            page_count += 1

        if new_delta:
            set_state(user_id, "outlook", delta_key, new_delta)

        return emails, new_delta

    # ── Thread grouping ───────────────────────────────────────────────────────

    def _group_into_threads(self, emails: List[dict]) -> List[ConversationDoc]:
        """
        Group raw email dicts by conversationId.
        Each group becomes one ConversationDoc where:
          - participants = union of all from/to/cc email addresses in the thread
          - display_names = address → name map built from those same fields
          - messages = one MessageTurn per email, sorted by sentDateTime
          - channel_name = email subject of the first (oldest) email
        """
        by_thread: Dict[str, List[dict]] = defaultdict(list)
        for email in emails:
            conv_id = email.get("conversationId") or email.get("id", "unknown")
            by_thread[conv_id].append(email)

        docs: List[ConversationDoc] = []
        for conv_id, thread_emails in by_thread.items():
            # Sort oldest → newest
            thread_emails.sort(
                key=lambda e: e.get("sentDateTime") or e.get("receivedDateTime", "")
            )
            # Cap per thread
            thread_emails = thread_emails[:CONNECTOR_MAX_EMAILS_PER_THREAD]

            participants: Dict[str, str] = {}   # email_addr → display_name
            turns: List[MessageTurn] = []

            for email in thread_emails:
                # Collect addresses from this email
                _collect_addresses(email, participants)

                # Build the MessageTurn
                sender_addr = _sender_address(email)
                sender_name = participants.get(sender_addr, sender_addr)
                body        = _extract_body(email)
                sent_at     = (
                    email.get("sentDateTime")
                    or email.get("receivedDateTime")
                    or datetime.now(timezone.utc).isoformat()
                )

                if not body:
                    continue

                turns.append(MessageTurn(
                    message_id  = email.get("id", ""),
                    sender_id   = sender_addr,
                    sender_name = sender_name,
                    content     = body,
                    sent_at     = sent_at,
                    thread_id   = conv_id,
                ))

            if not turns:
                continue

            subject      = thread_emails[0].get("subject", "(no subject)") or "(no subject)"
            participant_ids = list(participants.keys())

            docs.append(ConversationDoc(
                platform      = "outlook",
                chat_id       = conv_id,
                participants  = participant_ids,
                display_names = participants,
                channel_name  = f"Email: {subject}",
                messages      = turns,
                fetched_at    = datetime.now(timezone.utc).isoformat(),
                delta_link    = None,   # stored per-folder, not per-thread
            ))

        return docs


# ── Email parsing helpers (module-level) ──────────────────────────────────────

_HTML_TAG_RE   = re.compile(r"<[^>]+>")
_MULTI_NL_RE   = re.compile(r"\n{3,}")
_QUOTED_RE     = re.compile(
    r"(_{32,}|From:\s.+?Sent:\s.+?To:\s.+?Subject:\s.+?)$",
    re.DOTALL | re.MULTILINE,
)


def _extract_body(email: dict) -> str:
    """
    Extract plain-text body from an email dict.
    Prefers bodyPreview (already stripped) but uses full body when available.
    Strips HTML tags, quoted-reply blocks, and excessive whitespace.
    """
    body_obj     = email.get("body") or {}
    content_type = body_obj.get("contentType", "text")
    raw          = body_obj.get("content", "") or email.get("bodyPreview", "")

    if not raw:
        return ""

    if content_type == "html":
        raw = _HTML_TAG_RE.sub(" ", raw)

    # Strip quoted reply blocks ("From: ... Sent: ... To: ... Subject: ...")
    raw = _QUOTED_RE.sub("", raw)
    # Normalise whitespace
    raw = " ".join(raw.split())
    return raw.strip()


def _sender_address(email: dict) -> str:
    """Extract the sender's email address."""
    sender = (
        email.get("from")
        or email.get("sender")
        or {}
    )
    return (
        sender.get("emailAddress", {}).get("address", "")
        or "unknown@unknown"
    ).lower()


def _collect_addresses(email: dict, dest: Dict[str, str]) -> None:
    """
    Add all from/to/cc addresses in this email to the dest dict.
    dest maps lowercase_address → display_name.
    Existing entries are kept (first-seen name wins).
    """
    def _add(addr_obj: dict) -> None:
        ea   = addr_obj.get("emailAddress") or {}
        addr = (ea.get("address") or "").strip().lower()
        name = (ea.get("name") or addr).strip()
        if addr and addr not in dest:
            dest[addr] = name

    for field in ("from", "sender"):
        val = email.get(field)
        if isinstance(val, dict):
            _add(val)

    for field in ("toRecipients", "ccRecipients", "bccRecipients"):
        for item in email.get(field) or []:
            _add(item)
