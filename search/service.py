"""Live global search service.

Wraps Telethon's ``messages.SearchGlobalRequest`` with pagination, building
rich results (chat, sender, date, preview, message link). It queries Telegram
directly every time — no archive, cache or export data is consulted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from telethon import TelegramClient
from telethon import utils as tl_utils
from telethon.errors import FloodWaitError
from telethon.tl.functions.messages import SearchGlobalRequest
from telethon.tl.types import (
    InputMessagesFilterEmpty,
    InputMessagesFilterPhotoVideo,
    InputPeerEmpty,
)

logger = logging.getLogger("sentinel.search.service")

#: Default and maximum number of results.
DEFAULT_LIMIT = 50
MAX_LIMIT = 500

#: Per-request page size for SearchGlobal.
_PAGE_SIZE = 100


@dataclass(slots=True)
class SearchResult:
    """A single search hit."""

    chat_name: str
    chat_id: int
    message_id: int
    sender: str
    date: Optional[str]
    preview: str
    link: str


class SearchService:
    """Executes paginated live global searches."""

    def __init__(self, client: TelegramClient) -> None:
        """Initialise the service.

        Args:
            client: The Telethon client.
        """
        self._client = client

    async def search(
        self, query: str, *, limit: int = DEFAULT_LIMIT, media_only: bool = False
    ) -> list[SearchResult]:
        """Run a global search and return up to ``limit`` results.

        Args:
            query: The search query.
            limit: Maximum results (clamped to :data:`MAX_LIMIT`).
            media_only: Restrict to photo/video messages.

        Returns:
            A list of :class:`SearchResult` (possibly empty).
        """
        limit = max(1, min(limit, MAX_LIMIT))
        msg_filter = InputMessagesFilterPhotoVideo() if media_only else InputMessagesFilterEmpty()

        results: list[SearchResult] = []
        offset_rate = 0
        offset_peer: object = InputPeerEmpty()
        offset_id = 0

        while len(results) < limit:
            page = min(_PAGE_SIZE, limit - len(results))
            try:
                resp = await self._client(
                    SearchGlobalRequest(
                        q=query,
                        filter=msg_filter,
                        min_date=None,
                        max_date=None,
                        offset_rate=offset_rate,
                        offset_peer=offset_peer,
                        offset_id=offset_id,
                        limit=page,
                    )
                )
            except FloodWaitError as exc:
                logger.warning("Search FloodWait %ss", exc.seconds)
                break
            except Exception:  # noqa: BLE001 - search must not crash
                logger.exception("Global search request failed")
                break

            messages = list(getattr(resp, "messages", []))
            if not messages:
                break

            entities = self._index_entities(resp)
            for message in messages:
                result = self._build_result(message, entities)
                if result is not None:
                    results.append(result)

            # Prepare next page; stop if there is no continuation.
            next_rate = getattr(resp, "next_rate", None)
            last = messages[-1]
            offset_id = int(last.id)
            offset_peer = self._peer_for(last, entities)
            if next_rate is None or len(messages) < page:
                break
            offset_rate = next_rate

        return results[:limit]

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _index_entities(resp: object) -> dict[int, object]:
        """Build a ``{raw_id: entity}`` map from a response's chats and users."""
        index: dict[int, object] = {}
        for collection in (getattr(resp, "chats", []), getattr(resp, "users", [])):
            for entity in collection:
                ent_id = getattr(entity, "id", None)
                if ent_id is not None:
                    index[int(ent_id)] = entity
        return index

    def _build_result(
        self, message: object, entities: dict[int, object]
    ) -> Optional[SearchResult]:
        """Construct a :class:`SearchResult` from a message, or ``None``."""
        peer = getattr(message, "peer_id", None)
        if peer is None:
            return None
        chat_entity = self._entity_for_peer(peer, entities)
        try:
            chat_id = tl_utils.get_peer_id(peer)
        except Exception:  # noqa: BLE001
            return None
        chat_name = (
            tl_utils.get_display_name(chat_entity) if chat_entity else None
        ) or str(chat_id)

        sender_entity = self._sender_entity(message, entities)
        sender = self._sender_label(sender_entity, message)

        date = getattr(message, "date", None)
        text = getattr(message, "message", None) or ""
        preview = (text[:80] + "…") if len(text) > 80 else (text or "<media>")

        return SearchResult(
            chat_name=chat_name,
            chat_id=chat_id,
            message_id=int(message.id),
            sender=sender,
            date=date.isoformat() if date else None,
            preview=preview.replace("\n", " "),
            link=self._build_link(chat_entity, chat_id, int(message.id)),
        )

    @staticmethod
    def _entity_for_peer(peer: object, entities: dict[int, object]) -> Optional[object]:
        """Resolve a peer to its entity from the response index."""
        for attr in ("channel_id", "chat_id", "user_id"):
            raw = getattr(peer, attr, None)
            if raw is not None:
                return entities.get(int(raw))
        return None

    @staticmethod
    def _peer_for(message: object, entities: dict[int, object]) -> object:
        """Return an input peer usable as the next-page offset."""
        peer = getattr(message, "peer_id", None)
        entity = SearchService._entity_for_peer(peer, entities) if peer else None
        if entity is not None:
            try:
                return tl_utils.get_input_peer(entity)
            except Exception:  # noqa: BLE001
                pass
        return InputPeerEmpty()

    @staticmethod
    def _sender_entity(message: object, entities: dict[int, object]) -> Optional[object]:
        """Resolve the message's sender entity from the index."""
        from_id = getattr(message, "from_id", None)
        for attr in ("user_id", "channel_id", "chat_id"):
            raw = getattr(from_id, attr, None)
            if raw is not None:
                return entities.get(int(raw))
        return None

    @staticmethod
    def _sender_label(sender: Optional[object], message: object) -> str:
        """Build a readable sender label."""
        if sender is not None:
            username = getattr(sender, "username", None)
            if username:
                return f"@{username}"
            name = tl_utils.get_display_name(sender)
            if name:
                return name
        return "Unknown"

    @staticmethod
    def _build_link(chat_entity: Optional[object], chat_id: int, message_id: int) -> str:
        """Build a best-effort ``t.me`` link to the message.

        Priority: public ``t.me/<username>/<id>`` -> private
        ``t.me/c/<internal>/<id>`` -> ``chat_id:message_id`` fallback.
        """
        username = getattr(chat_entity, "username", None) if chat_entity else None
        if username:
            return f"https://t.me/{username}/{message_id}"
        text = str(chat_id)
        if text.startswith("-100"):
            return f"https://t.me/c/{text[4:]}/{message_id}"
        return f"{chat_id}:{message_id}"
