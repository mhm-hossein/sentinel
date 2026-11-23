"""Telethon command handlers for the Quick Variables module (owner only).

Two handlers are registered, both restricted to the self account
(``outgoing=True``):

    * **management** — ``.var add|edit|remove|show|list`` manages aliases.
    * **alias executor** — a catch-all ``.<keyword>`` (single token, no args)
      that, when the token matches a stored alias, replaces the command message
      in place with the stored value. Unknown tokens are a silent no-op, so the
      executor never shadows real commands (``.help``, ``.stats``, ``.save`` …);
      reserved verbs are additionally blocked at creation time.
"""

from __future__ import annotations

import logging
import re

from telethon import TelegramClient, events

from .manager import VariableError, VariablesManager

logger = logging.getLogger("sentinel.variables.commands")


def help_section() -> str:
    """Return the compact Variables help block for the ``.help`` menu."""
    return "\n".join(
        [
            "🔖 Variables",
            ".var add <keyword> <value>",
            ".var edit <keyword> <value>",
            ".var remove <keyword> | show <keyword>",
            ".var list   ( .<keyword> → sends value )",
        ]
    )


class VariablesCommands:
    """Register and serve the ``.var`` command and the alias executor."""

    def __init__(
        self,
        client: TelegramClient,
        manager: VariablesManager,
        *,
        prefix: str = ".",
    ) -> None:
        """Initialise the handler.

        Args:
            client: The Telethon client.
            manager: The variables manager.
            prefix: Command prefix (default ``"."``).
        """
        self._client = client
        self._manager = manager
        self._prefix = prefix

    def register(self) -> None:
        """Attach the management and alias-executor handlers (owner only)."""
        p = re.escape(self._prefix)
        # ``.var …`` — value group captures newlines for multiline support.
        self._client.add_event_handler(
            self._on_var,
            events.NewMessage(
                outgoing=True, pattern=rf"(?i)^{p}var(?:\s+([\s\S]*))?$"
            ),
        )
        # ``.<keyword>`` — single token, no arguments, no newlines.
        self._client.add_event_handler(
            self._on_alias,
            events.NewMessage(
                outgoing=True, pattern=rf"^{p}([A-Za-z0-9_]+)$"
            ),
        )
        logger.info("Variables commands registered (prefix=%r)", self._prefix)

    # ------------------------------------------------------------------ #
    # Management: .var …
    # ------------------------------------------------------------------ #
    async def _on_var(self, event: events.NewMessage.Event) -> None:
        """Dispatch a ``.var`` subcommand, never crashing."""
        if not event.out:
            return
        try:
            raw = event.pattern_match.group(1) or ""
            stripped = raw.strip()
            if not stripped:
                await self._edit(event, self._usage())
                return

            sub = stripped.split(maxsplit=1)[0].lower()
            # Remainder after the subcommand keyword, kept verbatim (multiline).
            rest = raw[raw.lower().find(sub) + len(sub):].lstrip()

            if sub == "add":
                await self._cmd_add(event, rest)
            elif sub == "edit":
                await self._cmd_edit(event, rest)
            elif sub in ("remove", "rm", "del", "delete"):
                await self._cmd_remove(event, rest)
            elif sub == "show":
                await self._cmd_show(event, rest)
            elif sub == "list":
                await self._cmd_list(event)
            else:
                await self._edit(event, self._usage())
        except VariableError as exc:
            await self._edit(event, f"⚠ {exc}")
        except Exception:  # noqa: BLE001 - a bad command must not crash the bot
            logger.exception("Variables command failed")
            await self._edit(event, "⚠ Variables Error")

    async def _cmd_add(self, event: events.NewMessage.Event, rest: str) -> None:
        """Handle ``.var add <keyword> <value>`` (value may be multiline)."""
        keyword, value = self._split_kw_value(rest)
        variable = await self._manager.add(keyword, value)
        await self._edit(event, f"✓ Added alias: {variable.keyword}")

    async def _cmd_edit(self, event: events.NewMessage.Event, rest: str) -> None:
        """Handle ``.var edit <keyword> <value>``."""
        keyword, value = self._split_kw_value(rest)
        variable = await self._manager.edit(keyword, value)
        await self._edit(event, f"✓ Updated alias: {variable.keyword}")

    async def _cmd_remove(self, event: events.NewMessage.Event, rest: str) -> None:
        """Handle ``.var remove <keyword>``."""
        keyword = rest.split(maxsplit=1)[0] if rest.strip() else ""
        removed = await self._manager.remove(keyword)
        await self._edit(event, f"🗑 Removed alias: {removed}")

    async def _cmd_show(self, event: events.NewMessage.Event, rest: str) -> None:
        """Handle ``.var show <keyword>`` (prints the stored value)."""
        keyword = rest.split(maxsplit=1)[0] if rest.strip() else ""
        variable = await self._manager.get(keyword)
        if variable is None:
            await self._edit(event, "⚠ Unknown alias")
            return
        await self._send_long(event, variable.value)

    async def _cmd_list(self, event: events.NewMessage.Event) -> None:
        """Handle ``.var list`` (keywords only — values never shown)."""
        keywords = await self._manager.list_keywords()
        if not keywords:
            await self._edit(event, "🔖 Variables\n\nNone")
            return
        lines = ["🔖 Variables", ""]
        lines += [f"• {kw}" for kw in keywords]
        lines += ["", f"Total: {len(keywords)}"]
        await self._edit(event, "\n".join(lines))

    # ------------------------------------------------------------------ #
    # Alias execution: .<keyword>
    # ------------------------------------------------------------------ #
    async def _on_alias(self, event: events.NewMessage.Event) -> None:
        """Replace ``.<keyword>`` with its stored value, if one exists.

        Unknown tokens are a deliberate no-op so this never interferes with
        real commands handled by other modules.
        """
        if not event.out:
            return
        try:
            keyword = event.pattern_match.group(1)
            value = await self._manager.lookup_value(keyword)
            if value is None:
                return  # not an alias — let other handlers deal with it
            await self._send_long(event, value)
        except Exception:  # noqa: BLE001 - alias execution must never crash
            logger.exception("Alias execution failed")

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _split_kw_value(rest: str) -> tuple[str, str]:
        """Split ``"<keyword> <value>"`` into ``(keyword, value)``.

        The value retains everything after the first whitespace run verbatim,
        so multiline values are preserved exactly.
        """
        parts = rest.split(maxsplit=1)
        if not parts:
            return "", ""
        keyword = parts[0]
        value = parts[1] if len(parts) > 1 else ""
        return keyword, value

    @staticmethod
    def _usage() -> str:
        """Return the ``.var`` usage block."""
        return "\n".join(
            [
                "Usage:",
                ".var add <keyword> <value>",
                ".var edit <keyword> <value>",
                ".var remove <keyword>",
                ".var show <keyword>",
                ".var list",
            ]
        )

    @staticmethod
    async def _edit(event: events.NewMessage.Event, text: str) -> None:
        """Edit the command message, swallowing edit errors."""
        try:
            await event.edit(text, link_preview=False)
        except Exception:  # noqa: BLE001 - editing is best-effort
            logger.debug("Failed to edit .var message")

    #: Telegram per-message cap (values longer than this cannot be edited in).
    _VALUE_LIMIT = 4096

    async def _send_long(self, event: events.NewMessage.Event, value: str) -> None:
        """Show a value: edit in place when short, else chunk to Saved Messages.

        Values longer than the Telegram edit limit would fail silently, so
        long values are delivered as chunked messages to Saved Messages and
        the command message just points there.
        """
        if len(value or "") <= self._VALUE_LIMIT:
            await self._edit(event, value)
            return
        chunks = [
            value[i: i + self._VALUE_LIMIT]
            for i in range(0, len(value), self._VALUE_LIMIT)
        ]
        try:
            for chunk in chunks:
                await self._client.send_message("me", chunk, link_preview=False)
            await self._edit(
                event, f"📏 Value too long ({len(value)} chars) → sent {len(chunks)} msgs to Saved Messages"
            )
        except Exception:  # noqa: BLE001 - delivery is best-effort
            logger.exception("Long variable delivery failed")
