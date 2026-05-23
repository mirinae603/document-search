# actions/models.py
# Data model for the actions capability.
#
# ActionProposal is the contract: a structured, stored, editable proposal to
# perform an action — built from indexed content, never auto-executed.
#
# JSON-typed fields (proposed_payload, current_payload, source_refs, audit_log)
# are held here as plain Python objects (dict / list). The storage layer is
# responsible for (de)serialising them to/from TEXT columns.
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


class ActionType(str, enum.Enum):
    """
    The kind of action a proposal represents.

    Phase 1 implements only DRAFT_EMAIL. TEAMS_REPLY and ACTION_ITEM are
    declared now so the enum, storage schema, and registry are stable when
    their proposers land in the next slices — do not implement them here.
    """
    DRAFT_EMAIL = "draft_email"
    TEAMS_REPLY = "teams_reply"
    ACTION_ITEM = "action_item"


class ActionStatus(str, enum.Enum):
    """Lifecycle state of a proposal."""
    PROPOSED  = "proposed"
    EDITED    = "edited"
    DISMISSED = "dismissed"
    EXECUTED  = "executed"


@dataclass
class ActionProposal:
    """
    A single proposed action.

    Field notes:
      - proposed_payload : the original LLM output. Immutable after creation —
                           the storage layer never updates this column.
      - current_payload  : mutable copy, edited via PATCH. Starts equal (by
                           value) to proposed_payload.
      - source_refs      : the indexed chunks this proposal was built from.
                           Never empty — an empty proposal is a bug, not a row.
      - audit_log        : append-only list of audit entries. Always has at
                           least the "created" entry.
    """
    id:               str
    type:             ActionType
    status:           ActionStatus
    proposed_payload: Dict[str, Any]
    current_payload:  Dict[str, Any]
    source_refs:      List[Dict[str, Any]]
    created_at:       str
    created_by:       str
    model_used:       str
    executed_at:      Optional[str] = None
    audit_log:        List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serialisable representation — used directly as an API response."""
        return {
            "id":               self.id,
            "type":             self.type.value,
            "status":           self.status.value,
            "proposed_payload": self.proposed_payload,
            "current_payload":  self.current_payload,
            "source_refs":      self.source_refs,
            "created_at":       self.created_at,
            "created_by":       self.created_by,
            "model_used":       self.model_used,
            "executed_at":      self.executed_at,
            "audit_log":        self.audit_log,
        }


# ── Helpers ─────────────────────────────────────────────────────────────────

def now_iso() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def make_audit_entry(action: str, actor: str, details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Build one append-only audit-log entry: {ts, action, actor, details}."""
    return {
        "ts":      now_iso(),
        "action":  action,
        "actor":   actor,
        "details": details or {},
    }
