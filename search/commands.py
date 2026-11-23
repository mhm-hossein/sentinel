"""``.search`` command handler for live global search (owner only).

Forms::

    .search <query>                live search (default 50 results)
    .search limit <count> <query>  custom result cap (max 500)
    .search media <query>          photo/video messages only
    .search export <query>         export results to TXT+JSON in Saved Messages
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re

from telethon import TelegramClient, events

from core.store import COUNTER_SEARCH_EXPORTS, COUNTER_SEARCH_QUERIES, SharedStore

from .service import DEFAULT_LIMIT, MAX_LIMIT, SearchResult, SearchService

logger = logging.getLogger("sentinel.search.commands")

_SAVED = "me"
_TELEGRAM_LIMIT = 4096


def help_section() -> str:
    """Return the compact Search help block for the ``.help`` menu."""
    return "\n".join(
        [
            "🔎 Search",
            ".search <query>",
            ".search limit <n> <query>",
            ".search media <query> | export <query>",
        ]
    )


class SearchCommands:
    """Register and serve the ``.search`` command."""

    def __init__(
        self,
        client: TelegramClient,
        service: SearchService,
        store: SharedStore,
        *,
        prefix: str = ".",
        exports_dir: str = os.path.join("data", "exports"),
    ) -> None:
        """Initialise the handler.

        Args:
            client: The Telethon client.
            service: The live search service.
            store: Shared store (search counters).
            prefix: Command prefix.
            exports_dir: Directory for search export files.
        """
        self._client = client
        self._service = service
        self._store = store
        self._prefix = prefix
        self._exports_dir = exports_dir

    def register(self) -> None:
        """Attach the ``.search`` handler (owner only)."""
        p = re.escape(self._prefix)
        self._client.add_event_handler(
            self._on_search,
            events.NewMessage(outgoing=True, pattern=rf"(?i)^{p}search(?:\s+([\s\S]*))?$"),
        )
        logger.info("Search .search command registered")

    async def _on_search(self, event: events.NewMessage.Event) -> None:
        """Parse, run and render a search, never crashing."""
        try:
            raw = (event.pattern_match.group(1) or "").strip()
            mode, limit, media_only, query = self._parse(raw)
            if not query:
                await self._edit(event, "Usage: .search [limit <n>|media|export] <query>")
                return

            await self._edit(event, "🔎 Searching…")
            results = await self._service.search(query, limit=limit, media_only=media_only)
            await self._store.incr(COUNTER_SEARCH_QUERIES)

            if not results:
                await self._edit(event, f"🔎 No results for: {query}")
                return

            if mode == "export":
                await self._export(event, query, results)
            else:
                await self._render(event, query, results)
        except Exception:  # noqa: BLE001 - search must not crash the bot
            logger.exception("Search command failed")
            await self._edit(event, "⚠ Search Error")

    def _parse(self, raw: str) -> tuple[str, int, bool, str]:
        """Parse raw args into ``(mode, limit, media_only, query)``."""
        parts = raw.split()
        if not parts:
            return "search", DEFAULT_LIMIT, False, ""
        first = parts[0].lower()
        if first == "limit" and len(parts) >= 2 and parts[1].isdigit():
            return "search", min(int(parts[1]), MAX_LIMIT), False, " ".join(parts[2:])
        if first == "media":
            return "search", DEFAULT_LIMIT, True, " ".join(parts[1:])
        if first == "export":
            return "export", MAX_LIMIT, False, " ".join(parts[1:])
        return "search", DEFAULT_LIMIT, False, raw

    async def _render(
        self, event: events.NewMessage.Event, query: str, results: list[SearchResult]
    ) -> None:
        """Render search results, editing or sending new messages as needed."""
        lines = [f"🔎 {len(results)} results for: {query}", ""]
        for r in results:
            lines += [
                f"{r.chat_name} [{r.chat_id}]",
                f"msg {r.message_id} | {r.sender} | {r.date or '-'}",
                r.preview,
                r.link,
                "",
            ]
        await self._deliver(event, "\n".join(lines))

    async def _export(
        self, event: events.NewMessage.Event, query: str, results: list[SearchResult]
    ) -> None:
        """Write results to TXT+JSON and deliver them to Saved Messages."""
        os.makedirs(self._exports_dir, exist_ok=True)
        slug = re.sub(r"[^\w-]", "_", query)[:40] or "query"
        txt_path = os.path.join(self._exports_dir, f"search_export_{slug}.txt")
        json_path = os.path.join(self._exports_dir, f"search_export_{slug}.json")

        await asyncio.to_thread(self._write_txt, txt_path, query, results)
        await asyncio.to_thread(self._write_json, json_path, query, results)

        await self._client.send_file(
            _SAVED, [txt_path, json_path], caption=f"🔎 Search export: {query} ({len(results)})"
        )
        await self._store.incr(COUNTER_SEARCH_EXPORTS)
        await self._edit(event, f"✓ Exported {len(results)} results → Saved Messages")

    @staticmethod
    def _write_txt(path: str, query: str, results: list[SearchResult]) -> None:
        """Write search results to a TXT file."""
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"Sentinel Search Export — {query}\n")
            fh.write("=" * 50 + "\n\n")
            for r in results:
                fh.write(f"{r.chat_name} [{r.chat_id}] msg {r.message_id}\n")
                fh.write(f"{r.sender} | {r.date or '-'}\n{r.preview}\n{r.link}\n\n")

    @staticmethod
    def _write_json(path: str, query: str, results: list[SearchResult]) -> None:
        """Write search results to a JSON file."""
        payload = {
            "query": query,
            "count": len(results),
            "results": [
                {
                    "chat_name": r.chat_name,
                    "chat_id": r.chat_id,
                    "message_id": r.message_id,
                    "sender": r.sender,
                    "date": r.date,
                    "preview": r.preview,
                    "link": r.link,
                }
                for r in results
            ],
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)

    async def _deliver(self, event: events.NewMessage.Event, text: str) -> None:
        """Edit for short output; send chunked new messages for long output."""
        if len(text) <= 1000:
            await self._edit(event, text)
            return
        await self._edit(event, "🔎 Results below ⬇")
        for chunk in _chunk(text, _TELEGRAM_LIMIT):
            try:
                await event.respond(chunk, link_preview=False)
            except Exception:  # noqa: BLE001 - best-effort
                logger.debug("Failed to send search chunk")

    async def _edit(self, event: events.NewMessage.Event, text: str) -> None:
        """Edit the command message, swallowing edit errors."""
        try:
            await event.edit(text, link_preview=False)
        except Exception:  # noqa: BLE001 - editing is best-effort
            logger.debug("Failed to edit .search message")


def _chunk(text: str, limit: int) -> list[str]:
    """Split text into chunks no longer than ``limit``, preferring newlines."""
    if len(text) <= limit:
        return [text]
    chunks, remaining = [], text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks
