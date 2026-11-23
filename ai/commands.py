"""Telethon command handlers for the AI Assistant (the ``.ai`` command).

Two handlers are registered:

    * **owner** (``outgoing=True``) -- full access, including model and trust
      management; replies by editing the command message (short responses) or
      sending new messages (long responses).
    * **trusted** (``incoming=True``) -- AI queries only, gated by the trusted
      user table and rate limiter; replies as new messages.

All failures are converted to short, safe messages — the bot never crashes and
never leaks secrets.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from telethon import TelegramClient, events

from .assistant import AIAssistant, INTENT_KEYWORDS
from .errors import AccessDeniedError, AIError

logger = logging.getLogger("banner.ai.commands")

#: Responses up to this length are delivered by editing the command message;
#: longer ones are sent as new messages (Telegram edit/readability limits).
_EDIT_THRESHOLD = 1000

#: Telegram's hard per-message character limit.
_TELEGRAM_LIMIT = 4096


def ai_help_text() -> str:
    """Return the full ``.ai`` help/guide text (English, compact)."""
    return "\n".join(
        [
            "🤖 AI Assistant (Gemini)",
            "",
            ".ai <question>          ask anything",
            ".ai  (reply)            smart auto-analyze",
            ".ai summarize  (reply)",
            ".ai explain    (reply)",
            ".ai analyze    (reply)",
            ".ai describe   (reply image)",
            ".ai review     (reply code)",
            ".ai translate  (reply/text)",
            ".ai rewrite    (reply/text)",
            "",
            ".ai model               show / current",
            ".ai model <name>        switch model",
            ".ai trust add <id>",
            ".ai trust remove <id>",
            ".ai trust list",
            "",
            "Files: images, PDF, TXT, MD, DOCX",
        ]
    )


def ai_help_section() -> str:
    """Return a compact AI block for embedding in the main ``.help`` menu."""
    return "\n".join(
        [
            "🤖 AI",
            ".ai <question> | .ai (reply) smart",
            ".ai summarize|explain|analyze|describe|review|translate|rewrite",
            ".ai model [name] | .ai trust add|remove|list <id>",
        ]
    )


class AICommandHandler:
    """Register and serve the ``.ai`` command for owner and trusted users."""

    def __init__(
        self,
        client: TelegramClient,
        assistant: AIAssistant,
        *,
        prefix: str = ".",
    ) -> None:
        """Initialise the handler.

        Args:
            client: The Telethon client.
            assistant: The AI assistant providing business logic.
            prefix: Command prefix (default ``"."``).
        """
        self._client = client
        self._assistant = assistant
        self._db = assistant._db  # shared persistence (intentional reuse)
        self._prefix = prefix

    # ------------------------------------------------------------------ #
    # Registration
    # ------------------------------------------------------------------ #
    def register(self) -> None:
        """Attach owner and trusted handlers and contribute help text."""
        pattern = rf"(?i)^{re.escape(self._prefix)}ai(?:\s+([\s\S]*))?$"
        self._client.add_event_handler(
            self._on_command, events.NewMessage(outgoing=True, pattern=pattern)
        )
        self._client.add_event_handler(
            self._on_command, events.NewMessage(incoming=True, pattern=pattern)
        )

        # Contribute the AI block to the banner help menu (best-effort).
        try:
            from banner.commands import register_help_section

            register_help_section(ai_help_section())
        except Exception:  # noqa: BLE001 - help integration is optional
            logger.debug("Could not register AI help section")

        logger.info("AI command handlers registered (prefix=%r)", self._prefix)

    # ------------------------------------------------------------------ #
    # Top-level dispatch
    # ------------------------------------------------------------------ #
    async def _on_command(self, event: events.NewMessage.Event) -> None:
        """Route an ``.ai`` command, converting any error to a safe message."""
        try:
            await self._route(event)
        except AIError as exc:
            await self._deliver(event, str(exc))
        except Exception:  # noqa: BLE001 - never crash the bot
            logger.exception("Unhandled error in .ai command")
            await self._deliver(event, "⚠ AI Error")

    async def _route(self, event: events.NewMessage.Event) -> None:
        """Parse and dispatch the command to the correct sub-handler."""
        is_owner = bool(event.out)
        sender_id = int(event.sender_id or 0)
        raw = (event.pattern_match.group(1) or "").strip()
        low = raw.lower()

        # Help: ".ai help" or a bare ".ai" with no reply.
        if low == "help" or low.startswith("help "):
            await self._require_access(is_owner, sender_id)
            await self._deliver(event, ai_help_text())
            return

        # Owner-only management subcommands (first-token match only).
        if low == "model" or low.startswith("model "):
            self._require_owner(is_owner)
            await self._cmd_model(event, raw[len("model"):].strip())
            return
        if low == "trust" or low.startswith("trust "):
            self._require_owner(is_owner)
            await self._cmd_trust(event, raw[len("trust"):].strip())
            return

        # AI query path (owner or trusted user).
        await self._require_access(is_owner, sender_id)

        replied = await event.get_reply_message() if event.is_reply else None
        if not raw and replied is None:
            await self._deliver(event, ai_help_text())
            return

        if not is_owner:
            self._assistant.enforce_rate_limit(sender_id)

        intent, text_arg = self._parse_intent(raw)

        # Show a lightweight "thinking" state on the owner's own message.
        if is_owner:
            await self._safe_edit(event, "🤖 …")

        answer = await self._assistant.process(
            user_id=sender_id,
            intent=intent,
            text_arg=text_arg,
            replied_message=replied,
        )
        await self._deliver(event, answer)

    # ------------------------------------------------------------------ #
    # Permission helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _require_owner(is_owner: bool) -> None:
        """Raise :class:`AccessDeniedError` unless the caller is the owner."""
        if not is_owner:
            raise AccessDeniedError()

    async def _require_access(self, is_owner: bool, sender_id: int) -> None:
        """Allow the owner; otherwise require the sender to be a trusted user.

        Raises:
            AccessDeniedError: If the non-owner sender is not trusted.
        """
        if is_owner:
            return
        if not await self._db.is_trusted(sender_id):
            raise AccessDeniedError()

    # ------------------------------------------------------------------ #
    # Parsing
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_intent(raw: str) -> tuple[Optional[str], str]:
        """Split a query into ``(intent, text_arg)``.

        If the first word is a recognised intent keyword it becomes ``intent``
        and the remainder is ``text_arg``; otherwise ``intent`` is ``None`` and
        the whole string is treated as a direct question.

        Args:
            raw: The text after ``.ai``.

        Returns:
            A ``(intent, text_arg)`` tuple.
        """
        if not raw:
            return None, ""
        parts = raw.split(maxsplit=1)
        first = parts[0].lower()
        if first in INTENT_KEYWORDS:
            return first, (parts[1].strip() if len(parts) > 1 else "")
        return None, raw

    # ------------------------------------------------------------------ #
    # Model management
    # ------------------------------------------------------------------ #
    async def _cmd_model(self, event: events.NewMessage.Event, arg: str) -> None:
        """Handle ``.ai model`` and ``.ai model <name|index>``."""
        if not arg:
            current = await self._assistant.get_model()
            models = self._assistant.list_models()
            fallbacks = self._assistant.fallback_chain()
            lines = [f"🤖 Current: {current}", ""]
            if fallbacks:
                lines += ["Fallback chain:", current + " → " + " → ".join(fallbacks), ""]
            lines.append("Configured:")
            lines += [f"{i}. {m}" for i, m in enumerate(models, 1)]
            await self._deliver(event, "\n".join(lines))
            return

        choice = arg.split()[0]
        models = self._assistant.list_models()
        # Allow selecting by 1-based index as a convenience.
        if choice.isdigit():
            index = int(choice) - 1
            if not (0 <= index < len(models)):
                from .errors import ModelNotFoundError

                raise ModelNotFoundError()
            choice = models[index]

        model = await self._assistant.set_model(choice)
        await self._deliver(event, f"✓ Model: {model}")

    # ------------------------------------------------------------------ #
    # Trusted users
    # ------------------------------------------------------------------ #
    async def _cmd_trust(self, event: events.NewMessage.Event, arg: str) -> None:
        """Handle ``.ai trust add|remove|list``."""
        parts = arg.split()
        sub = parts[0].lower() if parts else "list"
        rest = parts[1:]

        if sub == "add":
            user_id = self._parse_user_id(rest)
            added = await self._db.add_trusted(user_id)
            logger.info("Trusted user added: %s (new=%s)", user_id, added)
            await self._deliver(
                event, "✓ Trusted User Added" if added else "✓ Already Trusted"
            )
        elif sub in ("remove", "rm", "del"):
            user_id = self._parse_user_id(rest)
            removed = await self._db.remove_trusted(user_id)
            logger.info("Trusted user removed: %s (existed=%s)", user_id, removed)
            await self._deliver(
                event,
                "✓ Trusted User Removed" if removed else "⚠ Not a trusted user",
            )
        elif sub == "list":
            ids = await self._db.list_trusted()
            lines = ["Trusted Users", ""]
            lines += [str(uid) for uid in ids] or ["(none)"]
            lines += ["", f"Total: {len(ids)}"]
            await self._deliver(event, "\n".join(lines))
        else:
            await self._deliver(event, "Usage: .ai trust add|remove|list <id>")

    @staticmethod
    def _parse_user_id(rest: list[str]) -> int:
        """Parse a Telegram user ID argument.

        Raises:
            AIError: If the ID is missing or not an integer.
        """
        if not rest:
            raise AIError("⚠ Missing user id")
        try:
            return int(rest[0])
        except ValueError as exc:
            raise AIError("⚠ Invalid user id") from exc

    # ------------------------------------------------------------------ #
    # Output delivery
    # ------------------------------------------------------------------ #
    async def _deliver(self, event: events.NewMessage.Event, text: str) -> None:
        """Send ``text`` back, editing for short owner responses else new msgs.

        Long responses are chunked to respect Telegram's per-message limit.

        Args:
            event: The triggering command event.
            text: The response text.
        """
        is_owner = bool(event.out)
        chunks = self._chunk(text, _TELEGRAM_LIMIT)

        if is_owner and len(text) <= _EDIT_THRESHOLD and len(chunks) == 1:
            await self._safe_edit(event, text)
            return

        if is_owner:
            # Replace the command/"thinking" message with a short pointer.
            await self._safe_edit(event, "🤖 Response below ⬇")
            for chunk in chunks:
                await self._safe_send(event.respond, chunk)
        else:
            for chunk in chunks:
                await self._safe_send(event.reply, chunk)

    @staticmethod
    def _chunk(text: str, limit: int) -> list[str]:
        """Split ``text`` into chunks no longer than ``limit`` characters.

        Tries to break on newlines for readability, falling back to hard slices.

        Args:
            text: The text to split.
            limit: Maximum chunk length.

        Returns:
            A list of chunks (at least one).
        """
        text = text or ""
        if len(text) <= limit:
            return [text]
        chunks: list[str] = []
        remaining = text
        while len(remaining) > limit:
            split_at = remaining.rfind("\n", 0, limit)
            if split_at <= 0:
                split_at = limit
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:].lstrip("\n")
        if remaining:
            chunks.append(remaining)
        return chunks

    @staticmethod
    async def _safe_edit(event: events.NewMessage.Event, text: str) -> None:
        """Edit the command message, swallowing edit errors."""
        try:
            await event.edit(text, link_preview=False)
        except Exception:  # noqa: BLE001 - editing is best-effort
            logger.debug("AI: failed to edit message")

    @staticmethod
    async def _safe_send(send_func, text: str) -> None:
        """Send a message via ``send_func``, swallowing send errors."""
        try:
            await send_func(text, link_preview=False)
        except Exception:  # noqa: BLE001 - sending is best-effort
            logger.debug("AI: failed to send message")
