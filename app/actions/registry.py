# actions/registry.py
# OpenAI-style function-call tool schemas, one per action type.
#
# Phase 1 registers only DRAFT_EMAIL_TOOL. The TOOL_BY_ACTION_TYPE map and
# get_tool() are structured so the next slices add a tool by:
#   1. defining a new <X>_TOOL constant below, and
#   2. adding one ActionType -> tool line to TOOL_BY_ACTION_TYPE.
# Nothing else changes.
from __future__ import annotations

from typing import Dict

from actions.models import ActionType

# ── draft_email ──────────────────────────────────────────────────────────────

DRAFT_EMAIL_TOOL: Dict = {
    "type": "function",
    "function": {
        "name": "draft_email_reply",
        "description": (
            "Draft a reply email to an existing Outlook thread. "
            "Returns a structured draft. Does not send."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Recipient email addresses",
                },
                "cc": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "CC recipients (may be empty)",
                },
                "subject": {
                    "type": "string",
                    "description": "Email subject, typically 'Re: ...'",
                },
                "body": {
                    "type": "string",
                    "description": (
                        "Full email body in plain text, including greeting and sign-off"
                    ),
                },
            },
            "required": ["to", "subject", "body"],
        },
    },
}


# ── draft_teams_reply ────────────────────────────────────────────────────────

TEAMS_REPLY_TOOL: Dict = {
    "type": "function",
    "function": {
        "name": "draft_teams_reply",
        "description": (
            "Draft a reply message to a Teams conversation (DM or channel "
            "post). Returns a structured draft. Does not send."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "body": {
                    "type": "string",
                    "description": (
                        "The reply message body. May include @mentions as "
                        "plain text (e.g. '@Alice') — they will not be "
                        "resolved or hyperlinked at this stage."
                    ),
                },
                "mentions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "List of participant names mentioned in the body, "
                        "for future resolution. May be empty."
                    ),
                },
            },
            "required": ["body"],
        },
    },
}


# ── Registry ─────────────────────────────────────────────────────────────────

# Maps an ActionType to the tool schema used to propose that action.
# action_item slots in here when its proposer is built.
TOOL_BY_ACTION_TYPE: Dict[ActionType, Dict] = {
    ActionType.DRAFT_EMAIL: DRAFT_EMAIL_TOOL,
    ActionType.TEAMS_REPLY: TEAMS_REPLY_TOOL,
}


def get_tool(action_type: ActionType) -> Dict:
    """Return the tool schema for an action type, or raise if none is registered."""
    tool = TOOL_BY_ACTION_TYPE.get(action_type)
    if tool is None:
        raise NotImplementedError(
            f"No tool registered for action type '{action_type.value}'"
        )
    return tool
