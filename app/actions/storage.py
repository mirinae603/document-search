# actions/storage.py
# CRUD for the `action_proposals` table.
#
# Reuses the shared sqlite connection from qa.database (get_db()) — same
# pattern as qa/repository.py. No new DB abstraction.
#
# proposed_payload IMMUTABILITY:
#   create_proposal() is the only function that writes the proposed_payload
#   column. No update function below references it — that is the structural
#   enforcement of the "immutable after creation" contract. Edits go to
#   current_payload only.
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from qa.database import get_db

from actions.models import ActionProposal, ActionStatus, ActionType

logger = logging.getLogger(__name__)

# Sentinel: distinguishes "do not change executed_at" from "set executed_at = None".
_UNSET = object()


def _row_to_proposal(row) -> ActionProposal:
    return ActionProposal(
        id               = row["id"],
        type             = ActionType(row["type"]),
        status           = ActionStatus(row["status"]),
        proposed_payload = json.loads(row["proposed_payload"]),
        current_payload  = json.loads(row["current_payload"]),
        source_refs      = json.loads(row["source_refs"]),
        created_at       = row["created_at"],
        created_by       = row["created_by"],
        model_used       = row["model_used"],
        executed_at      = row["executed_at"],
        audit_log        = json.loads(row["audit_log"]),
    )


def create_proposal(p: ActionProposal) -> None:
    """Insert a new proposal. Writes proposed_payload exactly once, here."""
    db = get_db()
    db.execute(
        """INSERT INTO action_proposals
           (id, type, status, proposed_payload, current_payload, source_refs,
            created_at, created_by, model_used, executed_at, audit_log)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            p.id,
            p.type.value,
            p.status.value,
            json.dumps(p.proposed_payload),
            json.dumps(p.current_payload),
            json.dumps(p.source_refs),
            p.created_at,
            p.created_by,
            p.model_used,
            p.executed_at,
            json.dumps(p.audit_log),
        ],
    )
    db.commit()
    logger.info(f"action proposal created: id={p.id} type={p.type.value}")


def get_proposal(proposal_id: str) -> Optional[ActionProposal]:
    db  = get_db()
    row = db.execute(
        "SELECT * FROM action_proposals WHERE id = ?",
        [proposal_id],
    ).fetchone()
    return _row_to_proposal(row) if row else None


def list_proposals(
    status: Optional[ActionStatus] = None,
    type_:  Optional[ActionType]   = None,
    limit:  int = 50,
    offset: int = 0,
) -> List[ActionProposal]:
    db      = get_db()
    clauses: List[str] = []
    params:  List[Any] = []
    if status is not None:
        clauses.append("status = ?")
        params.append(status.value)
    if type_ is not None:
        clauses.append("type = ?")
        params.append(type_.value)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])
    rows = db.execute(
        f"SELECT * FROM action_proposals {where} "
        f"ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params,
    ).fetchall()
    return [_row_to_proposal(r) for r in rows]


def save_payload_edit(
    proposal_id:     str,
    current_payload: Dict[str, Any],
    status:          str,
    audit_log:       List[Dict[str, Any]],
) -> None:
    """
    Persist a PATCH edit: new current_payload, possibly-changed status, and the
    appended audit_log. proposed_payload is deliberately not in this statement.
    """
    db = get_db()
    db.execute(
        """UPDATE action_proposals
           SET current_payload = ?, status = ?, audit_log = ?
           WHERE id = ?""",
        [json.dumps(current_payload), status, json.dumps(audit_log), proposal_id],
    )
    db.commit()


def save_status_change(
    proposal_id: str,
    status:      str,
    audit_log:   List[Dict[str, Any]],
    executed_at: Any = _UNSET,
) -> None:
    """
    Persist a status change (dismiss / execute) plus the appended audit_log.

    executed_at is only written when explicitly passed — dismiss leaves it
    untouched, execute sets it.
    """
    db      = get_db()
    sets    = ["status = ?", "audit_log = ?"]
    params: List[Any] = [status, json.dumps(audit_log)]
    if executed_at is not _UNSET:
        sets.append("executed_at = ?")
        params.append(executed_at)
    params.append(proposal_id)
    db.execute(
        f"UPDATE action_proposals SET {', '.join(sets)} WHERE id = ?",
        params,
    )
    db.commit()
