# connectors/base.py
# Abstract interface every connector must implement.
# Adding Slack, Google Chat, or any other platform means:
#   1. Subclass ConnectorBase
#   2. Implement the three abstract methods
#   3. Register the new connector in api/connector_routes.py
# Nothing else in the codebase needs to change.
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from connectors.models import ConversationDoc


class ConnectorBase(ABC):

    @abstractmethod
    def platform_name(self) -> str:
        """Short slug used as the URL segment and DB platform field.
        Examples: "teams", "slack"
        """
        ...

    @abstractmethod
    async def authenticate(self, user_id: str = "default") -> str | None:
        """
        Return a valid, non-expired access token for *user_id*.
        Must silently refresh the token if it has expired.
        Returns None if the user has never authenticated or re-auth is required.
        """
        ...

    @abstractmethod
    async def fetch_conversations(
        self,
        delta: bool = True,
        user_id: str = "default",
    ) -> List[ConversationDoc]:
        """
        Fetch all conversations this user has access to.

        delta=True  → only return messages newer than the last sync
                       (uses stored delta links / cursors)
        delta=False → full historical pull (expensive; use for initial sync)

        Each ConversationDoc represents one 1:1 chat, group chat, or channel
        and contains all messages fetched in this run.
        """
        ...
