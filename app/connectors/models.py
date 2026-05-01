# connectors/models.py
# Shared data models used by all platform connectors.
# ConversationDoc is the normalised representation of any chat thread —
# Teams DM, Teams channel, Slack channel, etc. — before it enters the
# ingestion pipeline.
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class MessageTurn:
    """One message in a conversation."""
    message_id:   str
    sender_id:    str
    sender_name:  str
    content:      str
    sent_at:      str            
    thread_id:    Optional[str] = None   


@dataclass
class ConversationDoc:
    """
    Normalised representation of a conversation thread.

    One ConversationDoc maps to one document in SeaweedFS and one group
    of LanceDB chunk rows.  The stable identity key is (platform, chat_id).

    Mapping from the app.py proof-of-concept:
      teams.chat_id         → chat_id
      teams member userId   → participants[n]
      teams member displayName → display_names[uid]
      teams channelName     → channel_name
      teams message body    → messages[n].content
    """
    platform:      str                   # "teams" | "slack"
    chat_id:       str                   # stable Graph/Slack ID
    participants:  List[str]             # list of user IDs
    display_names: Dict[str, str]        # uid → human name
    channel_name:  Optional[str]         # None for 1:1, human label for group
    messages:      List[MessageTurn]     # ordered by sent_at ascending
    fetched_at:    str                   # ISO-8601 UTC — when this snapshot was made
    delta_link:    Optional[str] = None  # Graph delta URL for next incremental pull

    # ── Helpers ───────────────────────────────────────────────────────────────

    def participants_str(self) -> str:
        """Human-readable participant names, sorted for stable output."""
        return " ↔ ".join(
            sorted(self.display_names.get(p, p) for p in self.participants)
        )

    def to_plain_text(self) -> str:
        """
        Full conversation as plain text.
        Format: [Name | YYYY-MM-DD HH:MM] message content
        This is what gets stored in SeaweedFS for future re-indexing.
        """
        lines = []
        for m in self.messages:
            name = self.display_names.get(m.sender_id, m.sender_name)
            lines.append(f"[{name} | {m.sent_at[:16]}] {m.content}")
        return "\n".join(lines)

    def document_key(self) -> str:
        """Stable unique key — used as the basis for file_id hashing."""
        return f"{self.platform}_{self.chat_id}"

    def to_dict(self) -> dict:
        """Serialisable dict for JSON storage in SeaweedFS."""
        return {
            "platform":      self.platform,
            "chat_id":       self.chat_id,
            "participants":  self.participants,
            "display_names": self.display_names,
            "channel_name":  self.channel_name,
            "fetched_at":    self.fetched_at,
            "message_count": len(self.messages),
            "messages": [
                {
                    "message_id":  m.message_id,
                    "sender_id":   m.sender_id,
                    "sender_name": m.sender_name,
                    "content":     m.content,
                    "sent_at":     m.sent_at,
                    "thread_id":   m.thread_id,
                }
                for m in self.messages
            ],
        }
