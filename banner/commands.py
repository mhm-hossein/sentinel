"""Telethon command handlers and output formatting for the Banner system.

All commands are restricted to the self account (``outgoing=True``) and edit
the originating command message instead of sending new messages, keeping the
chat free of bot chatter.

Supported commands::

    .banner <time>                 create a forward banner (reply required)
    .banner copy <time>            create a copy banner (reply required)
    .banner list                   list active/paused banners
    .banner info <id>              show details for one banner
    .banner edit <id> <time>       change interval
    .banner mode <id> forward|copy change send mode
    .banner pause <id>             pause a banner
    .banner resume <id>            resume a banner
    .banner stop <id>              stop a banner
    .banner stopall                stop all banners
    .banner send <id>              send a banner immediately
    .banner status / .status       global status overview
    .banner help / .help           help menu
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from telethon import TelegramClient, events

from .banner_manager import BannerError, BannerManager
from .models import BannerMode, BannerStatus
from .utils import (
    format_ago,
    format_countdown,
    format_interval,
    format_timestamp,
    format_uptime,
    parse_interval,
)

logger = logging.getLogger("banner.commands")

#: Status -> UI glyph used in info output.
_STATUS_ICON = {
    BannerStatus.ACTIVE: "▶",
    BannerStatus.PAUSED: "⏸",
    BannerStatus.STOPPED: "✗",
}


@dataclass(frozen=True, slots=True)
class HelpEntry:
    """One line in the dynamically generated help menu.

    Attributes:
        usage: The command usage snippet (e.g. ``".banner edit <id> <time>"``).
        group: Logical grouping key used to insert blank lines between blocks.
    """

    usage: str
    group: str


#: Registry driving the dynamic help menu. Ordering and grouping here directly
#: determine the rendered ``.help`` output.
HELP_ENTRIES: tuple[HelpEntry, ...] = (
    HelpEntry(".banner <time>", "create"),
    HelpEntry(".banner copy <time>", "create"),
    HelpEntry(".banner list", "view"),
    HelpEntry(".banner info <id>", "view"),
    HelpEntry(".banner status", "view"),
    HelpEntry(".banner edit <id> <time>", "edit"),
    HelpEntry(".banner mode <id> copy|forward", "edit"),
    HelpEntry(".banner pause <id>", "state"),
    HelpEntry(".banner resume <id>", "state"),
    HelpEntry(".banner stop <id>", "stop"),
    HelpEntry(".banner stopall", "stop"),
    HelpEntry(".banner del <id>", "delete"),
    HelpEntry(".banner delall", "delete"),
    HelpEntry(".banner send <id>", "send"),
)


#: Extra help blocks contributed by other feature modules (e.g. the AI module).
#: Each entry is a pre-formatted, multi-line string appended to ``.help``.
EXTRA_HELP_SECTIONS: list[str] = []


def register_help_section(text: str) -> None:
    """Register an additional block to be appended to the ``.help`` menu.

    Lets optional feature modules contribute their own help without coupling
    this module to them. Idempotent: identical sections are not duplicated.

    Args:
        text: A pre-formatted, multi-line help block.
    """
    if text and text not in EXTRA_HELP_SECTIONS:
        EXTRA_HELP_SECTIONS.append(text)


class CommandHandler:
    """Register and serve all banner-related Telethon command handlers."""

    def __init__(
        self,
        client: TelegramClient,
        manager: BannerManager,
        *,
        prefix: str = ".",
        plan: object | None = None,
    ) -> None:
        """Initialise the handler set.

        Args:
            client: The Telethon client to attach handlers to.
            manager: The banner manager providing business logic.
            prefix: Command prefix (default ``"."``).
            plan: Optional subscription plan for quota enforcement. When
                ``None`` (single-user / tests) no limits are applied, so
                behavior is unchanged.
        """
        self._client = client
        self._manager = manager
        self._prefix = prefix
        self._plan = plan

    # ------------------------------------------------------------------ #
    # Registration
    # ------------------------------------------------------------------ #
    def register(self) -> None:
        """Attach all command handlers to the client."""
        p = self._escaped_prefix()
        self._client.add_event_handler(
            self._on_banner,
            events.NewMessage(outgoing=True, pattern=rf"(?i)^{p}banner(?:\s+(.*))?$"),
        )
        self._client.add_event_handler(
            self._on_help,
            events.NewMessage(outgoing=True, pattern=rf"(?i)^{p}help$"),
        )
        self._client.add_event_handler(
            self._on_status,
            events.NewMessage(outgoing=True, pattern=rf"(?i)^{p}status$"),
        )
        logger.info("Command handlers registered (prefix=%r)", self._prefix)

    def _escaped_prefix(self) -> str:
        """Return the prefix escaped for safe use inside a regex pattern."""
        return "".join("\\" + c if c in r".^$*+?()[]{}|\\" else c for c in self._prefix)

    # ------------------------------------------------------------------ #
    # Top-level handlers
    # ------------------------------------------------------------------ #
    async def _on_banner(self, event: events.NewMessage.Event) -> None:
        """Dispatch a ``.banner …`` command to its subcommand handler."""
        if not event.out:  # extra guard: only the self account
            return
        raw = (event.pattern_match.group(1) or "").strip()
        args = raw.split()
        try:
            await self._dispatch(event, args)
        except BannerError as exc:
            await self._reply(event, f"⚠ {exc}")
        except Exception:  # noqa: BLE001 - a bad command must not crash the bot
            logger.exception("Unhandled error in .banner command")
            await self._reply(event, "⚠ Command failed")

    async def _on_help(self, event: events.NewMessage.Event) -> None:
        """Handle the ``.help`` alias."""
        if not event.out:
            return
        await self._reply(event, self._render_help())

    async def _on_status(self, event: events.NewMessage.Event) -> None:
        """Handle the ``.status`` alias."""
        if not event.out:
            return
        await self._show_status(event)

    # ------------------------------------------------------------------ #
    # Subcommand dispatch
    # ------------------------------------------------------------------ #
    async def _dispatch(
        self, event: events.NewMessage.Event, args: list[str]
    ) -> None:
        """Route parsed ``.banner`` arguments to the right operation.

        Args:
            event: The triggering message event.
            args: Whitespace-split arguments following ``.banner``.
        """
        if not args:
            await self._reply(event, self._render_help())
            return

        keyword = args[0].lower()
        rest = args[1:]

        handlers: dict[str, Callable[[events.NewMessage.Event, list[str]], Awaitable[None]]] = {
            "help": lambda e, a: self._reply(e, self._render_help()),
            "list": self._cmd_list,
            "status": lambda e, a: self._show_status(e),
            "info": self._cmd_info,
            "edit": self._cmd_edit,
            "mode": self._cmd_mode,
            "pause": self._cmd_pause,
            "resume": self._cmd_resume,
            "stop": self._cmd_stop,
            "stopall": self._cmd_stopall,
            "del": self._cmd_delete,
            "delete": self._cmd_delete,
            "delall": self._cmd_delete_all,
            "send": self._cmd_send,
            "copy": self._cmd_create_copy,
        }

        handler = handlers.get(keyword)
        if handler is not None:
            await handler(event, rest)
            return

        # Otherwise treat the first token as an interval -> forward banner.
        await self._cmd_create(event, args, mode=BannerMode.FORWARD)

    # ------------------------------------------------------------------ #
    # Create
    # ------------------------------------------------------------------ #
    async def _cmd_create_copy(
        self, event: events.NewMessage.Event, args: list[str]
    ) -> None:
        """Handle ``.banner copy <time>``."""
        await self._cmd_create(event, args, mode=BannerMode.COPY)

    async def _cmd_create(
        self,
        event: events.NewMessage.Event,
        args: list[str],
        *,
        mode: BannerMode,
    ) -> None:
        """Create a banner from a reply.

        Args:
            event: The command event (must be a reply to the target message).
            args: Arguments containing the interval as the first token.
            mode: Forward or copy.
        """
        if not args:
            raise BannerError("Usage: .banner [copy] <time>")
        if not event.is_reply:
            raise BannerError("Reply to a message to create a banner")

        interval = self._parse_interval(args[0])
        reply_id = event.reply_to_msg_id
        if reply_id is None:
            raise BannerError("Reply to a message to create a banner")

        await self._enforce_banner_limit()

        banner = await self._manager.create_banner(
            chat_id=event.chat_id,
            source_message_id=reply_id,
            interval=interval,
            mode=mode,
        )
        await self._reply(
            event,
            f"✓ Banner #{banner.id} | {format_interval(banner.interval)} | "
            f"{banner.mode.short}",
        )

    async def _enforce_banner_limit(self) -> None:
        """Raise :class:`BannerError` if creating a banner exceeds the plan.

        No-op when no plan is attached (single-user / tests), preserving the
        original behavior exactly.
        """
        if self._plan is None:
            return
        try:
            from core.plan import LimitExceeded, plan_limit_text
        except Exception:  # noqa: BLE001 - plan module optional
            return
        try:
            current = len(await self._manager.list_banners())
            self._plan.check("max_banners", current)  # type: ignore[attr-defined]
        except LimitExceeded as exc:
            raise BannerError(plan_limit_text(exc)) from exc

    # ------------------------------------------------------------------ #
    # View
    # ------------------------------------------------------------------ #
    async def _cmd_list(
        self, event: events.NewMessage.Event, args: list[str]
    ) -> None:
        """Handle ``.banner list``."""
        banners = await self._manager.list_banners()
        if not banners:
            await self._reply(event, "📢 Banners\n\nNone")
            return

        lines = ["📢 Banners", ""]
        for banner in banners:
            link = await self._manager.build_link(banner)
            lines.append(
                f"#{banner.id} | {format_interval(banner.interval)} | "
                f"{banner.mode.short} | {banner.sent_count}x"
            )
            lines.append(f"Next: {format_countdown(banner.next_run)}")
            if link:
                lines.append(f"🔗 {link}")
            lines.append("")
        lines.append(f"Total: {len(banners)}")
        await self._reply(event, "\n".join(lines))

    async def _cmd_info(
        self, event: events.NewMessage.Event, args: list[str]
    ) -> None:
        """Handle ``.banner info <id>``."""
        banner_id = self._parse_id(args)
        banner = await self._manager.get_banner(banner_id)
        link = await self._manager.build_link(banner)

        icon = _STATUS_ICON.get(banner.status, "")
        lines = [
            f"#{banner.id} {icon} {banner.status.value}",
            "",
            f"Mode: {banner.mode.title}",
            f"Interval: {format_interval(banner.interval)}",
            f"Sent: {banner.sent_count}",
            f"Created: {format_timestamp(banner.created_at)}",
            f"Last: {format_ago(banner.last_run)}",
            f"Next: {format_countdown(banner.next_run)}",
        ]
        if link:
            lines += ["", f"🔗 {link}"]
        await self._reply(event, "\n".join(lines))

    async def _show_status(self, event: events.NewMessage.Event) -> None:
        """Render the global status overview."""
        try:
            data = await self._manager.status()
        except Exception:  # noqa: BLE001 - status must never crash the bot
            logger.exception("Status command failed")
            await self._reply(event, "⚠ Status Unavailable")
            return

        db_line = "DB: OK" if data["db_ok"] else "DB: ERROR"
        text = (
            "📊 Status\n\n"
            f"Active: {data['active']}\n"
            f"Paused: {data['paused']}\n"
            f"Total: {data['total']}\n\n"
            f"Sent: {data['total_sent']}\n"
            f"Uptime: {format_uptime(float(data['uptime']))}\n\n"
            f"{db_line}"
        )
        await self._reply(event, text)

    # ------------------------------------------------------------------ #
    # Edit / mode
    # ------------------------------------------------------------------ #
    async def _cmd_edit(
        self, event: events.NewMessage.Event, args: list[str]
    ) -> None:
        """Handle ``.banner edit <id> <time>``."""
        if len(args) < 2:
            raise BannerError("Usage: .banner edit <id> <time>")
        banner_id = self._parse_id(args)
        interval = self._parse_interval(args[1])
        banner = await self._manager.edit_interval(banner_id, interval)
        await self._reply(
            event, f"✓ Banner #{banner.id} | {format_interval(banner.interval)}"
        )

    async def _cmd_mode(
        self, event: events.NewMessage.Event, args: list[str]
    ) -> None:
        """Handle ``.banner mode <id> forward|copy``."""
        if len(args) < 2:
            raise BannerError("Usage: .banner mode <id> forward|copy")
        banner_id = self._parse_id(args)
        try:
            mode = BannerMode.parse(args[1])
        except ValueError as exc:
            raise BannerError(str(exc)) from exc
        banner = await self._manager.change_mode(banner_id, mode)
        await self._reply(event, f"✓ Banner #{banner.id} | {banner.mode.title}")

    # ------------------------------------------------------------------ #
    # State transitions
    # ------------------------------------------------------------------ #
    async def _cmd_pause(
        self, event: events.NewMessage.Event, args: list[str]
    ) -> None:
        """Handle ``.banner pause <id>``."""
        banner = await self._manager.pause(self._parse_id(args))
        await self._reply(event, f"⏸ Banner #{banner.id}")

    async def _cmd_resume(
        self, event: events.NewMessage.Event, args: list[str]
    ) -> None:
        """Handle ``.banner resume <id>``."""
        banner = await self._manager.resume(self._parse_id(args))
        await self._reply(event, f"▶ Banner #{banner.id}")

    async def _cmd_stop(
        self, event: events.NewMessage.Event, args: list[str]
    ) -> None:
        """Handle ``.banner stop <id>``."""
        banner = await self._manager.stop_banner(self._parse_id(args))
        await self._reply(event, f"✗ Banner #{banner.id}")

    async def _cmd_stopall(
        self, event: events.NewMessage.Event, args: list[str]
    ) -> None:
        """Handle ``.banner stopall``."""
        count = await self._manager.stop_all()
        await self._reply(event, f"✗ Stopped {count} banner(s)")

    async def _cmd_delete(
        self, event: events.NewMessage.Event, args: list[str]
    ) -> None:
        """Handle ``.banner del <id>`` / ``.banner delete <id>``."""
        banner_id = self._parse_id(args)
        await self._manager.delete_banner(banner_id)
        await self._reply(event, f"🗑 Banner #{banner_id}")

    async def _cmd_delete_all(
        self, event: events.NewMessage.Event, args: list[str]
    ) -> None:
        """Handle ``.banner delall``."""
        count = await self._manager.delete_all()
        await self._reply(event, f"🗑 Deleted: {count}")

    async def _cmd_send(
        self, event: events.NewMessage.Event, args: list[str]
    ) -> None:
        """Handle ``.banner send <id>`` (manual immediate send)."""
        banner = await self._manager.manual_send(self._parse_id(args))
        await self._reply(
            event, f"✓ Banner #{banner.id} | Sent ({banner.sent_count}x)"
        )

    # ------------------------------------------------------------------ #
    # Help rendering
    # ------------------------------------------------------------------ #
    def _render_help(self) -> str:
        """Build the compact help menu from :data:`HELP_ENTRIES`.

        Blank lines are inserted between distinct ``group`` values so the menu
        reads as logical blocks while staying generated from the registry.

        Returns:
            The full help text.
        """
        lines = ["🛰 Sentinel", "", "🚩 Banner"]
        prev_group: object = None
        for entry in HELP_ENTRIES:
            if prev_group is not None and entry.group != prev_group:
                lines.append("")
            lines.append(entry.usage)
            prev_group = entry.group

        # Append any feature-module help blocks (AI, Tracking, Logger, …).
        for section in EXTRA_HELP_SECTIONS:
            lines.append("")
            lines.append(section)

        # Core/global commands.
        lines += [
            "",
            "⚙ Core",
            ".help | .stats | .status",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Shared helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_id(args: list[str]) -> int:
        """Parse a banner ID from the first argument.

        Args:
            args: Argument list whose first element should be the ID.

        Returns:
            The parsed integer ID.

        Raises:
            BannerError: If no valid integer ID is present.
        """
        if not args:
            raise BannerError("Missing banner id")
        try:
            return int(args[0])
        except ValueError as exc:
            raise BannerError(f"Invalid banner id: {args[0]!r}") from exc

    @staticmethod
    def _parse_interval(text: str) -> int:
        """Parse an interval string, raising :class:`BannerError` on failure."""
        try:
            return parse_interval(text)
        except ValueError as exc:
            raise BannerError(str(exc)) from exc

    @staticmethod
    async def _reply(event: events.NewMessage.Event, text: str) -> None:
        """Edit the command message in place, swallowing edit errors.

        Args:
            event: The command event to edit.
            text: The text to display.
        """
        try:
            await event.edit(text, link_preview=False)
        except Exception:  # noqa: BLE001 - editing is best-effort UX
            logger.exception("Failed to edit command message")
