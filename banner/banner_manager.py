"""High-level orchestration for the Banner system.

:class:`BannerManager` ties together the Telethon client, the SQLite
persistence layer and the async scheduler. It exposes the operations used by
the command handlers (create, list, info, edit, mode, pause, resume, stop,
stop-all, manual send, status) and owns startup restoration of active banners.

The manager raises :class:`BannerError` for user-facing failures (invalid IDs,
etc.); command handlers translate those into compact messages.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from telethon import TelegramClient
from telethon import errors as tg_errors
from telethon.tl.custom import Message
from telethon.tl.types import MessageMediaWebPage

from .database import Database
from .models import Banner, BannerMode, BannerStatus
from .scheduler import BannerFatalError, BannerScheduler
from .utils import build_message_link

logger = logging.getLogger("banner.manager")

#: Telethon errors that mean a banner can never succeed again.
_FATAL_ERRORS: tuple[type[Exception], ...] = (
    tg_errors.MessageIdInvalidError,
    tg_errors.MessageDeleteForbiddenError,
    tg_errors.ChannelPrivateError,
    tg_errors.ChatWriteForbiddenError,
    tg_errors.ChatAdminRequiredError,
    tg_errors.UserBannedInChannelError,
    tg_errors.PeerIdInvalidError,
)

#: Spread restored banners that are due immediately a few seconds apart so
#: restart does not produce a thundering herd of sends.
_RESTORE_STAGGER_SECONDS = 3


class BannerError(Exception):
    """A user-facing banner operation error (e.g. unknown banner ID)."""


class BannerManager:
    """Coordinate persistence, scheduling and Telegram sends for banners."""

    def __init__(self, client: TelegramClient, database: Database) -> None:
        """Initialise the manager.

        Args:
            client: A connected (or to-be-connected) Telethon client.
            database: The persistence layer.
        """
        self._client = client
        self._db = database
        self._scheduler = BannerScheduler(database, self._perform_send)
        self._started_at: float = time.time()
        #: Cache of chat_id -> public username (or ``None``) for link building.
        self._username_cache: dict[int, Optional[str]] = {}

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """Connect the database and restore all active banners."""
        self._started_at = time.time()
        await self._db.connect()
        await self._restore()

    async def stop(self) -> None:
        """Shut the scheduler down and close the database."""
        await self._scheduler.shutdown()
        await self._db.close()

    async def _restore(self) -> None:
        """Recreate scheduler tasks for every active banner in the database."""
        active = await self._db.list_banners(statuses=[BannerStatus.ACTIVE])
        now = time.time()
        restored = 0
        for index, banner in enumerate(active):
            # Re-anchor due/overdue banners with a small stagger.
            if banner.next_run is None or banner.next_run <= now:
                banner.next_run = now + _RESTORE_STAGGER_SECONDS * (index + 1)
                await self._db.update_banner(banner)
            self._scheduler.schedule(banner)
            restored += 1
        logger.info("Restored %d active banner(s)", restored)

    @property
    def uptime_seconds(self) -> float:
        """Seconds since the manager was last started."""
        return time.time() - self._started_at

    # ------------------------------------------------------------------ #
    # Banner operations
    # ------------------------------------------------------------------ #
    async def create_banner(
        self,
        *,
        chat_id: int,
        source_message_id: int,
        interval: int,
        mode: BannerMode = BannerMode.FORWARD,
    ) -> Banner:
        """Create, persist and schedule a new banner.

        Args:
            chat_id: Chat the banner posts into.
            source_message_id: Message to re-post.
            interval: Interval in seconds.
            mode: Forward (default) or copy.

        Returns:
            The newly created, scheduled banner.
        """
        now = time.time()
        banner = Banner(
            chat_id=chat_id,
            source_message_id=source_message_id,
            interval=interval,
            mode=mode,
            status=BannerStatus.ACTIVE,
            created_at=now,
            next_run=now + interval,
        )
        banner = await self._db.create_banner(banner)
        self._scheduler.schedule(banner)
        logger.info(
            "Banner created #%s chat=%s msg=%s interval=%ss mode=%s",
            banner.id,
            banner.chat_id,
            banner.source_message_id,
            banner.interval,
            banner.mode.value,
        )
        return banner

    async def get_banner(self, banner_id: int) -> Banner:
        """Fetch a banner by ID or raise :class:`BannerError`."""
        banner = await self._db.get_banner(banner_id)
        if banner is None:
            raise BannerError(f"Banner #{banner_id} not found")
        return banner

    async def list_banners(self, *, include_stopped: bool = False) -> list[Banner]:
        """List banners (active + paused by default).

        Args:
            include_stopped: If ``True`` stopped banners are included too.

        Returns:
            Banners ordered by ID.
        """
        statuses = [BannerStatus.ACTIVE, BannerStatus.PAUSED]
        if include_stopped:
            statuses.append(BannerStatus.STOPPED)
        return await self._db.list_banners(statuses=statuses)

    async def edit_interval(self, banner_id: int, interval: int) -> Banner:
        """Change a banner's interval and reschedule its next run.

        Args:
            banner_id: Target banner.
            interval: New interval in seconds.

        Returns:
            The updated banner.
        """
        banner = await self.get_banner(banner_id)
        banner.interval = interval
        if banner.is_active:
            banner.next_run = time.time() + interval
        await self._db.update_banner(banner)
        if banner.is_active:
            self._scheduler.reschedule(banner)
        logger.info("Banner #%s interval changed to %ss", banner_id, interval)
        return banner

    async def change_mode(self, banner_id: int, mode: BannerMode) -> Banner:
        """Change a banner's send mode.

        Args:
            banner_id: Target banner.
            mode: New mode.

        Returns:
            The updated banner.
        """
        banner = await self.get_banner(banner_id)
        banner.mode = mode
        await self._db.update_banner(banner)
        logger.info("Banner #%s mode changed to %s", banner_id, mode.value)
        return banner

    async def pause(self, banner_id: int) -> Banner:
        """Pause an active banner (idempotent)."""
        banner = await self.get_banner(banner_id)
        if banner.status is BannerStatus.STOPPED:
            raise BannerError(f"Banner #{banner_id} is stopped")
        banner.status = BannerStatus.PAUSED
        banner.next_run = None
        self._scheduler.unschedule(banner_id)
        await self._db.update_banner(banner)
        logger.info("Banner #%s paused", banner_id)
        return banner

    async def resume(self, banner_id: int) -> Banner:
        """Resume a paused banner."""
        banner = await self.get_banner(banner_id)
        if banner.status is BannerStatus.STOPPED:
            raise BannerError(f"Banner #{banner_id} is stopped")
        banner.status = BannerStatus.ACTIVE
        banner.next_run = time.time() + banner.interval
        await self._db.update_banner(banner)
        self._scheduler.schedule(banner)
        logger.info("Banner #%s resumed", banner_id)
        return banner

    async def stop_banner(self, banner_id: int) -> Banner:
        """Stop a banner permanently (retained for statistics)."""
        banner = await self.get_banner(banner_id)
        banner.status = BannerStatus.STOPPED
        banner.next_run = None
        self._scheduler.unschedule(banner_id)
        await self._db.update_banner(banner)
        logger.info("Banner #%s stopped", banner_id)
        return banner

    async def stop_all(self) -> int:
        """Stop every active or paused banner.

        Returns:
            The number of banners stopped.
        """
        banners = await self.list_banners()
        for banner in banners:
            assert banner.id is not None
            banner.status = BannerStatus.STOPPED
            banner.next_run = None
            self._scheduler.unschedule(banner.id)
            await self._db.update_banner(banner)
        logger.info("Stopped all banners (%d)", len(banners))
        return len(banners)

    async def delete_banner(self, banner_id: int) -> Banner:
        """Permanently delete a banner: cancel its task and remove the row.

        Unlike :meth:`stop_banner` (which keeps the record so it can be
        resumed), a deleted banner is gone for good and cannot be resumed.
        The send-statistics counters in the ``stats`` table are preserved.

        The scheduler task is cancelled first (O(1) registry removal), then the
        row is hard-deleted. If a send is racing the delete, the cancelled task
        settles harmlessly and any late ``record_send`` simply updates zero
        rows.

        Args:
            banner_id: The banner to delete.

        Returns:
            The banner as it existed immediately before deletion.

        Raises:
            BannerError: If no banner with that ID exists.
        """
        banner = await self.get_banner(banner_id)  # raises BannerError if absent
        self._scheduler.unschedule(banner_id)
        await self._db.delete_banner(banner_id)
        logger.info("Banner #%s deleted", banner_id)
        return banner

    async def delete_all(self) -> int:
        """Delete every banner: cancel all tasks and clear the table.

        Global statistics (total sent/created) are kept intact. Scheduler
        registry is emptied so no zombie tasks remain.

        Returns:
            The number of banners deleted.
        """
        self._scheduler.unschedule_all()
        count = await self._db.delete_all_banners()
        logger.info("Deleted all banners (%d)", count)
        return count

    async def manual_send(self, banner_id: int) -> Banner:
        """Send a banner immediately, advancing its counters.

        Does not disturb the regular schedule timing for active banners beyond
        resetting the next run relative to now.

        Args:
            banner_id: Target banner.

        Returns:
            The banner with updated statistics.

        Raises:
            BannerError: If the banner is missing or the send fails fatally.
        """
        banner = await self.get_banner(banner_id)
        try:
            await self._perform_send(banner)
        except BannerFatalError as exc:
            raise BannerError(str(exc)) from exc

        now = time.time()
        banner.sent_count += 1
        banner.last_run = now
        if banner.is_active:
            banner.next_run = now + banner.interval
            self._scheduler.reschedule(banner)
        await self._db.record_send(banner)
        logger.info("Banner #%s manually sent (count=%d)", banner_id, banner.sent_count)
        return banner

    async def status(self) -> dict[str, object]:
        """Return an aggregate status snapshot for the status command.

        Uses optimized aggregate SQL rather than iterating banners. Never
        raises: on failure it returns ``{"db_ok": False}`` with zeroed counts.

        Returns:
            A mapping with ``active``, ``paused``, ``stopped``, ``total``,
            ``total_sent``, ``uptime`` (seconds) and ``db_ok`` (bool).
        """
        db_ok = await self._db.healthy()
        if not db_ok:
            return {
                "active": 0,
                "paused": 0,
                "stopped": 0,
                "total": 0,
                "total_sent": 0,
                "uptime": self.uptime_seconds,
                "db_ok": False,
            }
        counts = await self._db.status_counts()
        counts["uptime"] = self.uptime_seconds
        counts["db_ok"] = True
        return counts

    # ------------------------------------------------------------------ #
    # Link helpers
    # ------------------------------------------------------------------ #
    async def build_link(self, banner: Banner) -> str:
        """Build a ``t.me`` link to a banner's source message.

        Resolves and caches the chat's public username (if any) to produce a
        canonical link; falls back to the private ``/c/`` form. Never raises.

        Args:
            banner: The banner whose source message should be linked.

        Returns:
            A clickable URL, or ``""`` if none can be produced.
        """
        username = await self._resolve_username(banner.chat_id)
        return build_message_link(
            banner.chat_id, banner.source_message_id, username=username
        )

    async def _resolve_username(self, chat_id: int) -> Optional[str]:
        """Return the cached/public username for ``chat_id`` (``None`` if any)."""
        if chat_id in self._username_cache:
            return self._username_cache[chat_id]
        username: Optional[str] = None
        try:
            entity = await self._client.get_entity(chat_id)
            username = getattr(entity, "username", None)
        except Exception:  # noqa: BLE001 - link building is best-effort
            logger.debug("Could not resolve username for chat %s", chat_id)
        self._username_cache[chat_id] = username
        return username

    # ------------------------------------------------------------------ #
    # Sending (shared by scheduler and manual send)
    # ------------------------------------------------------------------ #
    async def _perform_send(self, banner: Banner) -> None:
        """Deliver one instance of a banner's message.

        Forward mode uses Telegram's native forward; copy mode re-sends the
        content (text, single media, or media group) as a fresh message.

        Args:
            banner: The banner to send.

        Raises:
            FloodWaitError: Propagated unchanged for the scheduler to handle.
            BannerFatalError: If the source/chat is gone or posting is
                forbidden.
        """
        try:
            source = await self._client.get_messages(
                banner.chat_id, ids=banner.source_message_id
            )
            if source is None:
                raise BannerFatalError("source message deleted")

            if banner.mode is BannerMode.FORWARD:
                await self._client.forward_messages(
                    banner.chat_id, source.id, from_peer=banner.chat_id
                )
            else:
                await self._copy_message(banner.chat_id, source)
        except tg_errors.FloodWaitError:
            raise  # handled by the scheduler
        except _FATAL_ERRORS as exc:
            raise BannerFatalError(str(exc)) from exc

    async def _copy_message(self, chat_id: int, message: Message) -> None:
        """Re-send the content of ``message`` as a fresh message.

        Handles plain text, single media (photo/video/document) and media
        groups (albums). Web-page-preview "media" is treated as text.

        Args:
            chat_id: Destination chat.
            message: The source message to copy.
        """
        # Media group / album: gather sibling messages and send together.
        if message.grouped_id:
            album = await self._fetch_album(chat_id, message)
            media = [m.media for m in album if m.media]
            caption = next((m.message for m in album if m.message), "")
            if media:
                await self._client.send_file(chat_id, media, caption=caption)
                return

        has_media = message.media and not isinstance(
            message.media, MessageMediaWebPage
        )
        if has_media:
            await self._client.send_file(
                chat_id, message.media, caption=message.message or ""
            )
        else:
            await self._client.send_message(chat_id, message.message or "")

    async def _fetch_album(self, chat_id: int, message: Message) -> list[Message]:
        """Fetch all messages belonging to ``message``'s media group.

        Albums are posted as consecutive messages, so a small window around the
        anchor message is scanned and filtered by ``grouped_id``.

        Args:
            chat_id: Chat containing the album.
            message: Any message belonging to the album.

        Returns:
            The album's messages sorted by ID (at minimum the anchor message).
        """
        window_ids = list(range(message.id - 9, message.id + 10))
        try:
            fetched = await self._client.get_messages(chat_id, ids=window_ids)
        except Exception:  # noqa: BLE001 - fall back to the single message
            return [message]
        album = [
            m for m in fetched if m is not None and m.grouped_id == message.grouped_id
        ]
        album.sort(key=lambda m: m.id)
        return album or [message]
