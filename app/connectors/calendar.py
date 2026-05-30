# connectors/calendar.py
# CalendarConnector — read-only live access to a user's Outlook calendar via
# Microsoft Graph.
#
# Deliberately mirrors connectors/outlook.py:
#   - MSAL ConfidentialClientApplication built in __init__ (NO network I/O there)
#   - All network happens inside async methods
#   - Token persistence reuses connectors/token_store (no second token store)
#   - Silent refresh via MSAL acquire_token_silent on expiry
#
# This connector is READ-ONLY. It only ever issues GET /me/calendarView.
# It never creates, accepts, declines, or modifies events, and it never writes
# calendar data into LanceDB. The Outlook calendar shares the same Microsoft
# Graph token as the email connector, so we reuse the "outlook" platform token —
# this requires the OAuth app to be (re-)consented with Calendars.Read added to
# the scope list (see config.TEAMS_SCOPES).
from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional

import httpx
import msal

from config import (
    TEAMS_CLIENT_ID,
    TEAMS_CLIENT_SECRET,
    TEAMS_REDIRECT_URI,
    TEAMS_SCOPES,
    TEAMS_TENANT_ID,
)
from connectors.base import ConnectorBase
from connectors.models import ConversationDoc
from connectors.token_store import get_token, is_token_expired, save_token

logger = logging.getLogger(__name__)

# Calendar reuses the same registered Microsoft Graph application + token as the
# Outlook email connector (same client id / tenant). After Calendars.Read is
# added to the scope list and the user re-consents, that token grants calendar
# read access too.
CALENDAR_CLIENT_ID     = TEAMS_CLIENT_ID
CALENDAR_CLIENT_SECRET = TEAMS_CLIENT_SECRET
CALENDAR_REDIRECT_URI  = TEAMS_REDIRECT_URI
CALENDAR_TENANT_ID     = TEAMS_TENANT_ID
CALENDAR_SCOPES        = TEAMS_SCOPES

# The token is persisted by the Outlook connector under this platform slug; the
# calendar piggy-backs on the same Microsoft Graph credential.
_TOKEN_PLATFORM = "outlook"

GRAPH = "https://graph.microsoft.com/v1.0"

# Max events to request from a single calendarView page.
_DEFAULT_TOP = 50


class CalendarConnector(ConnectorBase):
    """Read-only Outlook calendar reader. Lazily authenticates; no I/O in __init__."""

    def __init__(self):
        if not CALENDAR_CLIENT_ID:
            logger.warning(
                "TEAMS_CLIENT_ID is not set — Calendar connector will not authenticate. "
                "Set TEAMS_CLIENT_ID / TEAMS_CLIENT_SECRET / TEAMS_TENANT_ID in env."
            )
        self._msal = msal.ConfidentialClientApplication(
            CALENDAR_CLIENT_ID,
            authority=f"https://login.microsoftonline.com/{CALENDAR_TENANT_ID}",
            client_credential=CALENDAR_CLIENT_SECRET,
        )

    # ── ConnectorBase ─────────────────────────────────────────────────────────

    def platform_name(self) -> str:
        return "calendar"

    async def authenticate(self, user_id: str = "default") -> Optional[str]:
        """
        Return a valid, non-expired Microsoft Graph access token for *user_id*.
        Reuses the Outlook token (same Graph app) and silently refreshes via MSAL
        if expired. Returns None when re-auth is required.
        """
        row = get_token(user_id, _TOKEN_PLATFORM)
        if not row:
            return None

        if not is_token_expired(row):
            return row["access_token"]

        try:
            accounts = self._msal.get_accounts()
            result   = self._msal.acquire_token_silent(
                scopes  = CALENDAR_SCOPES,
                account = accounts[0] if accounts else None,
            )
            if result and "access_token" in result:
                save_token(
                    user_id       = user_id,
                    platform      = _TOKEN_PLATFORM,
                    access_token  = result["access_token"],
                    expires_in    = result.get("expires_in", 3600),
                    refresh_token = result.get("refresh_token", row.get("refresh_token")),
                    scopes        = result.get("scope", "").split(),
                    account       = result.get("account") or json.loads(
                        row.get("account_json", "{}")
                    ),
                )
                logger.info(f"Calendar token silently refreshed for user={user_id}")
                return result["access_token"]
        except Exception as e:
            logger.warning(f"Calendar silent refresh failed for user={user_id}: {e}")

        logger.warning(
            f"Calendar token expired and silent refresh failed for user={user_id}. "
            "Re-authentication required."
        )
        return None

    async def fetch_conversations(
        self,
        delta:   bool = True,
        user_id: str  = "default",
    ) -> List[ConversationDoc]:
        """
        Not applicable — calendar events are never ingested as conversations.
        Present only to satisfy the ConnectorBase contract. This connector is a
        live, read-only synthesis source, not an ingestion source.
        """
        raise NotImplementedError(
            "CalendarConnector does not ingest conversations — it is a live, "
            "read-only source consumed by intelligence/meeting_prep.py."
        )

    # ── Public API ─────────────────────────────────────────────────────────────

    async def get_events(
        self,
        start_iso: str,
        end_iso:   str,
        user_id:   str = "default",
        top:       int = _DEFAULT_TOP,
    ) -> List[dict]:
        """
        Return calendar events that occur within [start_iso, end_iso) as a list
        of structured event dicts (see _parse_event). LIVE Graph call — nothing
        is cached or persisted.

        Uses GET /me/calendarView, which expands recurring series into concrete
        occurrences within the window (unlike GET /me/events).
        """
        token = await self.authenticate(user_id)
        if not token:
            raise RuntimeError(
                f"No valid calendar token for user={user_id}. "
                "Visit /connector/outlook/oauth/start to authenticate "
                "(ensure Calendars.Read consent has been granted)."
            )

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
            # Ask Graph to return start/end in UTC so downstream code can append 'Z'.
            "Prefer":        'outlook.timezone="UTC"',
        }

        select = (
            "id,subject,start,end,location,bodyPreview,"
            "isOnlineMeeting,organizer,attendees"
        )
        url = (
            f"{GRAPH}/me/calendarView"
            f"?startDateTime={start_iso}&endDateTime={end_iso}"
            f"&$orderby=start/dateTime&$select={select}&$top={top}"
        )

        events: List[dict] = []
        async with httpx.AsyncClient(timeout=60) as client:
            # Single page is sufficient for a prep window; follow nextLink defensively.
            page_count = 0
            while url and page_count < 5:
                resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    raise RuntimeError(
                        f"calendarView failed: HTTP {resp.status_code}: {resp.text[:300]}"
                    )
                data = resp.json()
                for ev in data.get("value", []):
                    events.append(_parse_event(ev))
                url = data.get("@odata.nextLink")
                page_count += 1

        logger.info(
            f"Calendar connector: {len(events)} events for user={user_id} "
            f"in [{start_iso}, {end_iso})"
        )
        return events


# ── Event parsing helpers (module-level) ───────────────────────────────────────

# Microsoft Graph attendee response strings → the schema's enum.
_RESPONSE_MAP = {
    "accepted":            "accepted",
    "declined":            "declined",
    "tentativelyaccepted": "tentative",
    "notresponded":        "none",
    "none":                "none",
    "organizer":           "accepted",
}


def _parse_event(ev: dict) -> dict:
    """Flatten a Graph calendar event into the structure meeting_prep expects."""
    return {
        "event_id":     ev.get("id", ""),
        "subject":      ev.get("subject", "") or "(no subject)",
        "start":        _graph_datetime(ev.get("start")),
        "end":          _graph_datetime(ev.get("end")),
        "location":     (ev.get("location") or {}).get("displayName", "") or "",
        "body_preview": ev.get("bodyPreview", "") or "",
        "is_online":    bool(ev.get("isOnlineMeeting", False)),
        "organizer":    _parse_person(ev.get("organizer")),
        "attendees":    [_parse_attendee(a) for a in (ev.get("attendees") or [])],
    }


def _graph_datetime(dt_obj: Optional[dict]) -> str:
    """
    Convert a Graph dateTimeTimeZone object to an ISO-8601 string.
    With the 'Prefer: outlook.timezone="UTC"' header the timeZone is UTC, so we
    trim sub-second precision and append 'Z' for a clean RFC-3339 value.
    """
    if not dt_obj:
        return ""
    raw = (dt_obj.get("dateTime") or "").strip()
    if not raw:
        return ""
    # Graph returns e.g. "2026-05-30T14:00:00.0000000"; drop the fractional part.
    raw = raw.split(".")[0]
    tz  = (dt_obj.get("timeZone") or "").lower()
    if tz == "utc" and not raw.endswith("Z"):
        raw += "Z"
    return raw


def _parse_person(person: Optional[dict]) -> dict:
    """Extract {name, email} from a Graph recipient/organizer object."""
    ea = (person or {}).get("emailAddress") or {}
    return {
        "name":  ea.get("name", "") or "",
        "email": (ea.get("address", "") or "").strip().lower(),
    }


def _parse_attendee(att: dict) -> dict:
    """Extract {name, email, response, type} from a Graph attendee object.

    `type` (required | optional | resource) is surfaced so downstream triage
    (calendar_priority) can read attendance_required for the signed-in user.
    Defaults to "required" when Graph omits it (the Graph default).
    """
    person   = _parse_person(att)
    raw_resp = ((att.get("status") or {}).get("response", "") or "").lower()
    return {
        "name":     person["name"],
        "email":    person["email"],
        "response": _RESPONSE_MAP.get(raw_resp, "none"),
        "type":     (att.get("type", "") or "required").lower(),
    }
