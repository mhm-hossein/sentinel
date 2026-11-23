"""Self-serve control bot — inline onboarding wizard + management panel.

A Telethon **bot-mode** client (`CONTROL_BOT_TOKEN`) presents an inline-button
panel. A *panel user* (the person DMing the bot, identified by `sender_id`) owns
one or more *selfbot instances* (each a separately-authorized Telegram account).

Surface:
    * **Registration wizard** — /start → Welcome → Terms → Accept → Phone → OTP →
      2FA (if set) → instance created → worker started.
    * **Account panel** — 👤 My Account · 📊 My Plan · 🤖 My Selfbot · 🔑 Gemini
      Keys · 📈 Usage · ⚙ Settings · 🆘 Support.
    * **Selfbot (per-instance)** — ▶ Start · ⏹ Stop · 🔄 Restart · 📋 Status;
      ➕ Add Instance (plan-gated by `max_instances`).
    * **Gemini keys** — Add · List Active · List Disabled · Enable · Disable ·
      Test (real probe). Keys are never auto-deleted.

Persistence: all wizard/panel-input state lives in the central `bot_state` table,
so onboarding survives a control-plane restart. Security: OTP codes and 2FA
passwords and API keys are never logged and never persisted; the message
carrying such a secret is deleted after it is read.

> ⚠ Collecting login codes / 2FA via a bot is the account-sharing pattern
> Telegram warns against; sessions are secrets at rest. Inherent to self-serve.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from telethon import Button, TelegramClient, events
from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)

from core.central import (
    AUTH_ACTIVE,
    AUTH_CODE_SENT,
    CentralDatabase,
    INST_RUNNING,
    FLOW_AWAITING_KEY,
    FLOW_AWAITING_PREFIX,
    FLOW_ONBOARDING,
    PERM_ADMINS,
    PERM_AUDIT,
    PERM_BROADCAST,
    PERM_FEATURES,
    PERM_INSTANCE_CONTROL,
    PERM_MONITORING,
    PERM_PLANS,
    PERM_REQUESTS,
    PERM_STATS,
    PERM_TICKETS,
    PERM_USERS,
    REQ_APPROVED,
    REQ_PENDING,
    REQ_REJECTED,
    TICKET_CLOSED,
    TICKET_OPEN,
    USER_ACTIVE,
    USER_SUSPENDED,
)
from core.feature_gate import FeatureGate
from core.plan import gemini_key_limit, instance_limit
from sentinel.monitoring import gather_overview, instance_metrics
from sentinel.paths import InstancePaths
from sentinel.usage import file_size, fmt_bytes, read_usage

#: User-side ticket/support flow keys (persisted in bot_state).
_FLOW_TICKET = "ticket_input"

logger = logging.getLogger("sentinel.control_bot")

# Onboarding phases (persisted in bot_state.phase).
_PHASE_PHONE = "phone"
_PHASE_CODE = "code"
_PHASE_PASSWORD = "password"

#: Admin free-text input flow (bot_state.flow); phase = action, payload = target.
_FLOW_ADMIN = "admin_input"

#: Subscription duration presets offered on approval (label → days; 0 = custom).
_DURATIONS: tuple[tuple[str, int], ...] = (
    ("30 days", 30), ("60 days", 60), ("90 days", 90),
    ("180 days", 180), ("365 days", 365), ("Custom", 0),
)

#: Human labels for usage counters shown in the Usage panel.
_USAGE_LABELS: tuple[tuple[str, str], ...] = (
    ("banners_active", "Active Banners"),
    ("banners_total", "Total Banners"),
    ("ai_requests", "AI Requests"),
    ("saved_media", "Saved Media"),
    ("messages_logged", "Messages Logged"),
    ("exports_generated", "Exports"),
    ("backups_created", "Backups"),
)


def _admin_env_ids() -> set[int]:
    """Parse ``SENTINEL_ADMIN_IDS`` into a set of ints."""
    raw = os.environ.get("SENTINEL_ADMIN_IDS", "")
    ids: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


def _mask_key(api_key: str) -> str:
    """Mask an API key for display (never show the whole secret)."""
    if len(api_key) <= 8:
        return "•" * len(api_key)
    return f"{api_key[:4]}…{api_key[-4:]}"


class ControlBot:
    """Inline onboarding + management panel over the Bot API."""

    def __init__(self, central: CentralDatabase, *, api_id: int, api_hash: str, token: str) -> None:
        """Initialise the control bot.

        Args:
            central: A connected central database.
            api_id: Shared platform API id.
            api_hash: Shared platform API hash.
            token: Bot token from @BotFather.
        """
        self._central = central
        self._api_id = api_id
        self._api_hash = api_hash
        self._token = token
        self._bot = TelegramClient("control_bot", api_id, api_hash)
        self._admins = _admin_env_ids()
        self._default_plan = os.environ.get("SENTINEL_DEFAULT_PLAN", "bronze")
        self._support = os.environ.get("SENTINEL_SUPPORT_CONTACT", "@your_support")
        self._tos_url = os.environ.get("SENTINEL_TOS_URL", "")
        self._default_model = os.environ.get("DEFAULT_AI_MODEL", "gemini-2.5-flash")
        try:
            self._owner_id = int(os.environ.get("SENTINEL_OWNER_ID", "0"))
        except (TypeError, ValueError):
            self._owner_id = 0
        self._gate = FeatureGate(central)
        #: Optional AlertManager (set by the platform entry) for backup alerts.
        self._alerts: object = None
        #: control-chat id -> live onboarding login client (not persistable).
        self._logins: dict[int, TelegramClient] = {}

    # ------------------------------------------------------------------ #
    # Authorization helpers (RBAC — DB-driven, owner always full access)
    # ------------------------------------------------------------------ #
    async def _is_staff(self, sender_id: int) -> bool:
        """Return ``True`` if the sender is the owner or any admin."""
        if sender_id == self._owner_id:
            return True
        return await self._central.is_admin(sender_id)

    async def _can(self, sender_id: int, permission: str) -> bool:
        """Return ``True`` if the sender has ``permission`` (owner ⇒ always)."""
        if sender_id == self._owner_id:
            return True
        return await self._central.has_permission(sender_id, permission)

    async def _is_owner(self, sender_id: int) -> bool:
        """Return ``True`` if the sender is the platform owner."""
        if sender_id == self._owner_id:
            return True
        async with self._central._db.execute(  # read-only role lookup
            "SELECT role FROM admins WHERE telegram_user_id = ?;", (sender_id,)
        ) as cur:
            row = await cur.fetchone()
        return bool(row and row["role"] == "owner")

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """Start the bot, seed admins, attach handlers, rehydrate wizards."""
        await self._central.seed_admins(self._admins)
        await self._bot.start(bot_token=self._token)
        self._register()
        await self._rehydrate()
        logger.info("Control bot online (env-admins=%s)", sorted(self._admins) or "none")

    async def run(self) -> None:
        """Start and run until disconnected."""
        await self.start()
        await self._bot.run_until_disconnected()

    async def stop(self) -> None:
        """Disconnect the bot and any live onboarding login clients."""
        for chat_id in list(self._logins):
            await self._drop_login(chat_id)
        await self._bot.disconnect()

    async def send_message(self, chat_id: int, text: str) -> None:
        """Send a message via the bot (used as the AlertManager owner sender)."""
        await self._bot.send_message(chat_id, text)

    async def _rehydrate(self) -> None:
        """Reconnect login clients for onboarding flows that were mid-flight.

        Panel-input flows (key/prefix) are stateless and need no rehydration.
        A stale OTP (`phone_code_hash` expired at Telegram) is best-effort: the
        user is prompted to restart that step.
        """
        for row in await self._central.list_bot_state(flow=FLOW_ONBOARDING):
            chat_id = row["chat_id"]
            session_path = row["session_path"]
            if not session_path:
                continue
            try:
                client = TelegramClient(
                    session_path[: -len(".session")] if session_path.endswith(".session")
                    else session_path,
                    self._api_id, self._api_hash,
                )
                await client.connect()
                self._logins[chat_id] = client
                logger.info("Rehydrated onboarding for chat %s (phase=%s)", chat_id, row["phase"])
            except Exception:  # noqa: BLE001 - best-effort resume
                logger.debug("Could not rehydrate onboarding for chat %s", chat_id)

    # ------------------------------------------------------------------ #
    # Handler registration
    # ------------------------------------------------------------------ #
    def _register(self) -> None:
        """Attach command, callback and free-text handlers."""
        b = self._bot
        b.add_event_handler(self._on_start, events.NewMessage(pattern=r"^/start$"))
        b.add_event_handler(self._on_cancel, events.NewMessage(pattern=r"^/cancel$"))
        b.add_event_handler(self._on_admin, events.NewMessage(pattern=r"^/admin$"))
        b.add_event_handler(self._on_callback, events.CallbackQuery())
        b.add_event_handler(
            self._on_message,
            events.NewMessage(func=lambda e: e.raw_text and not e.raw_text.startswith("/")),
        )

    # ------------------------------------------------------------------ #
    # Entry
    # ------------------------------------------------------------------ #
    async def _on_start(self, event: events.NewMessage.Event) -> None:
        """/start — greet + show Terms (first time) or the account panel."""
        user = await self._central.get_user(event.sender_id)
        if user is None:
            user = await self._central.create_user(
                event.sender_id,
                username=getattr(await event.get_sender(), "username", None),
                plan_name=self._default_plan, status=USER_ACTIVE,
            )
        if not user.tos_accepted_at:
            await self._show_welcome(event)
        else:
            await self._show_panel(event)

    async def _on_cancel(self, event: events.NewMessage.Event) -> None:
        """/cancel — abort any in-flight input flow."""
        await self._clear_flow(event.sender_id)
        await event.respond("✖ Cancelled.", buttons=self._panel_buttons())

    # ------------------------------------------------------------------ #
    # Welcome + Terms
    # ------------------------------------------------------------------ #
    async def _show_welcome(self, event) -> None:
        """Render the welcome + Terms-of-Service acceptance screen."""
        tos = (
            "🛰 **Welcome to Sentinel**\n\n"
            "Sentinel runs a personal Telegram automation selfbot on your account.\n\n"
            "**Terms of Service (summary)**\n"
            "• Automating a user account may violate Telegram's ToS; use at your own risk.\n"
            "• You authorize Sentinel to store your session to operate your selfbot.\n"
            "• You are responsible for how you use the automation.\n"
        )
        if self._tos_url:
            tos += f"\nFull terms: {self._tos_url}\n"
        await self._reply(event, tos, buttons=[
            [Button.inline("✅ Accept & Continue", b"tos:accept")],
            [Button.inline("❌ Decline", b"tos:decline")],
        ])

    # ------------------------------------------------------------------ #
    # Callback router
    # ------------------------------------------------------------------ #
    async def _on_callback(self, event: events.CallbackQuery.Event) -> None:
        """Route an inline-button tap to its handler (never crashes the bot)."""
        try:
            data = (event.data or b"").decode("utf-8", "ignore")
            if data == "tos:accept":
                await self._tos_accept(event)
            elif data == "tos:decline":
                await event.edit("You declined. Send /start any time to reconsider.")
            elif data == "menu:home":
                await self._render_panel(event)
            elif data == "menu:account":
                await self._panel_account(event)
            elif data == "menu:plan":
                await self._panel_plan(event)
            elif data == "menu:selfbot":
                await self._panel_selfbot(event)
            elif data == "menu:keys":
                await self._panel_keys(event)
            elif data == "menu:usage":
                await self._panel_usage(event)
            elif data == "menu:settings":
                await self._panel_settings(event)
            elif data == "menu:support":
                await self._panel_support(event)
            elif data == "inst:add":
                await self._instance_add(event)
            elif data.startswith("inst:status:"):
                await self._instance_status(event, int(data.rsplit(":", 1)[1]))
            elif data.startswith("inst:start:"):
                await self._instance_set_enabled(event, int(data.rsplit(":", 1)[1]), True)
            elif data.startswith("inst:stop:"):
                await self._instance_set_enabled(event, int(data.rsplit(":", 1)[1]), False)
            elif data.startswith("inst:restart:"):
                await self._instance_restart(event, int(data.rsplit(":", 1)[1]))
            elif data.startswith("inst:delete:"):
                await self._instance_delete_confirm(event, int(data.rsplit(":", 1)[1]))
            elif data.startswith("inst:delok:"):
                await self._instance_delete_perform(event, int(data.rsplit(":", 1)[1]))
            elif data == "gk:add":
                await self._key_add_prompt(event)
            elif data == "gk:active":
                await self._panel_keys(event, enabled=True)
            elif data == "gk:disabled":
                await self._panel_keys(event, enabled=False)
            elif data.startswith("gk:enable:"):
                await self._key_set_enabled(event, int(data.rsplit(":", 1)[1]), True)
            elif data.startswith("gk:disable:"):
                await self._key_set_enabled(event, int(data.rsplit(":", 1)[1]), False)
            elif data.startswith("gk:test:"):
                await self._key_test(event, int(data.rsplit(":", 1)[1]))
            elif data.startswith("gk:del:"):
                await self._key_delete(event, int(data.rsplit(":", 1)[1]))
            elif data == "set:prefix":
                await self._prefix_prompt(event)
            elif data == "menu:plans":
                await self._user_plans(event)
            elif data.startswith("plan:req:"):
                await self._user_request_plan(event, int(data.rsplit(":", 1)[1]))
            elif data == "menu:tickets":
                await self._user_tickets(event)
            elif data == "tk:new":
                await self._user_ticket_new(event)
            elif data.startswith("tk:view:"):
                await self._user_ticket_view(event, int(data.rsplit(":", 1)[1]))
            elif data.startswith("tk:reply:"):
                await self._user_ticket_reply_prompt(event, int(data.rsplit(":", 1)[1]))
            elif data.startswith("tk:close:"):
                await self._user_ticket_close(event, int(data.rsplit(":", 1)[1]))
            elif data.startswith("adm:"):
                await self._on_admin_callback(event, data)
            else:
                await event.answer()
        except Exception:  # noqa: BLE001 - a bad tap must not crash the bot
            logger.exception("Callback failed")
            try:
                await event.answer("⚠ Something went wrong.", alert=True)
            except Exception:  # noqa: BLE001
                pass

    async def _tos_accept(self, event) -> None:
        """Record ToS acceptance and start the first registration."""
        await self._central.set_tos_accepted(event.sender_id)
        await self._begin_onboarding(event)

    # ------------------------------------------------------------------ #
    # Account panel
    # ------------------------------------------------------------------ #
    def _panel_buttons(self) -> list:
        """Return the account-panel inline keyboard."""
        return [
            [Button.inline("👤 My Account", b"menu:account"),
             Button.inline("📊 My Plan", b"menu:plan")],
            [Button.inline("🤖 My Selfbot", b"menu:selfbot"),
             Button.inline("🔑 Gemini Keys", b"menu:keys")],
            [Button.inline("📦 Plans", b"menu:plans"),
             Button.inline("📈 Usage", b"menu:usage")],
            [Button.inline("🎫 Support", b"menu:tickets"),
             Button.inline("⚙ Settings", b"menu:settings")],
            [Button.inline("🆘 Help", b"menu:support")],
        ]

    def _back_button(self) -> list:
        """A single 'back to panel' row."""
        return [[Button.inline("⬅ Back", b"menu:home")]]

    async def _show_panel(self, event) -> None:
        """Send the account panel as a new message."""
        await self._reply(event, "🛰 **Sentinel — Account Panel**", buttons=self._panel_buttons())

    async def _render_panel(self, event) -> None:
        """Edit the current message into the account panel."""
        await event.edit("🛰 **Sentinel — Account Panel**", buttons=self._panel_buttons())

    async def _panel_account(self, event) -> None:
        """👤 My Account overview."""
        user = await self._central.get_user(event.sender_id)
        if user is None:
            await event.edit("Send /start first.", buttons=self._back_button())
            return
        instances = await self._central.list_instances(user.id)
        is_admin = await self._central.is_admin(event.sender_id)
        text = (
            "👤 **My Account**\n\n"
            f"ID: `{user.telegram_user_id}`\n"
            f"Status: {user.status}\n"
            f"Plan: {user.plan_name or '—'}\n"
            f"Instances: {len(instances)}\n"
            f"Prefix: `{user.prefix}`\n"
            f"Admin: {'yes' if is_admin else 'no'}"
        )
        await event.edit(text, buttons=self._back_button())

    async def _panel_plan(self, event) -> None:
        """📊 My Plan — plan, expiration, features, instances used/allowed."""
        user = await self._central.get_user(event.sender_id)
        plan = await self._central.plan_for_user(event.sender_id)
        if user is None or plan is None:
            await event.edit(
                "No plan assigned. Tap 📦 Plans to request one.",
                buttons=[[Button.inline("📦 Plans", b"menu:plans")], *self._back_button()],
            )
            return
        sub = await self._central.get_active_subscription(user.id)
        features = sorted(await self._gate.features_for(event.sender_id))
        used = await self._central.count_instances(user.id)
        allowed = instance_limit(plan)
        max_i = "∞" if allowed < 0 else allowed
        expiry = "—"
        if sub is not None and sub["expires_at"]:
            expiry = _fmt_date(sub["expires_at"])
        elif sub is not None:
            expiry = "never"
        lines = [
            f"📊 **My Plan — {plan.name}**",
            "",
            f"Status: {sub['status'] if sub else 'no active subscription'}",
            f"Expiration: {expiry}",
            f"Instances Used: {used} / {max_i}",
            "",
            "**Features Included**",
            (", ".join(features) if features else "none"),
        ]
        await event.edit("\n".join(lines), buttons=[
            [Button.inline("⬆ Request Upgrade", b"menu:plans")],
            *self._back_button(),
        ])

    # ------------------------------------------------------------------ #
    # Selfbot (instances)
    # ------------------------------------------------------------------ #
    async def _panel_selfbot(self, event) -> None:
        """🤖 My Selfbot — list instances with controls + Add Instance."""
        user = await self._central.get_user(event.sender_id)
        if user is None:
            await event.edit("Send /start first.", buttons=self._back_button())
            return
        instances = await self._central.list_instances(user.id)
        plan = await self._central.plan_for_user(event.sender_id)
        limit = instance_limit(plan)

        lines = ["🤖 **My Selfbot**", ""]
        buttons: list = []
        if not instances:
            lines.append("No instances yet. Add one to get started.")
        for inst in instances:
            running = inst.status == "running"
            dot = "🟢" if running else "⚪"
            acct = f"@{inst.account_id}" if inst.account_id else "unlinked"
            lines.append(f"{dot} **{inst.name}** ({acct}) — {inst.status}")
            row = [
                Button.inline("▶", f"inst:start:{inst.id}".encode()),
                Button.inline("⏹", f"inst:stop:{inst.id}".encode()),
                Button.inline("🔄", f"inst:restart:{inst.id}".encode()),
                Button.inline("📋", f"inst:status:{inst.id}".encode()),
                Button.inline("🗑", f"inst:delete:{inst.id}".encode()),
            ]
            buttons.append(row)

        can_add = limit < 0 or len(instances) < limit
        if can_add:
            buttons.append([Button.inline("➕ Add Instance", b"inst:add")])
        else:
            lines.append(f"\nInstance limit reached ({len(instances)}/{limit}). Upgrade your plan.")
        buttons.append([Button.inline("⬅ Back", b"menu:home")])
        await event.edit("\n".join(lines), buttons=buttons)

    async def _instance_status(self, event, instance_id: int) -> None:
        """📋 Worker status panel: running/uptime/memory/tasks/banners."""
        inst = await self._owned_instance(event, instance_id)
        if inst is None:
            return
        import time as _time

        running = inst.status == "running"
        uptime = "—"
        if running and inst.started_at:
            uptime = _fmt_duration(_time.time() - inst.started_at)
        mem = _process_memory_mb(inst.pid) if running and inst.pid else None
        text = (
            f"📋 **{inst.name} — Status**\n\n"
            f"State: {'🟢 Running' if running else '⚪ ' + inst.status.title()}\n"
            f"Account: {('@' + str(inst.account_id)) if inst.account_id else 'unlinked'}\n"
            f"Uptime: {uptime}\n"
            f"Memory: {f'{mem:.1f} MB' if mem is not None else '—'}\n"
            f"Active Tasks: {inst.active_tasks}\n"
            f"Active Banners: {inst.active_banners}\n"
            f"Restarts: {inst.restart_count}\n"
            f"Auth: {inst.auth_state}"
        )
        buttons = [
            [Button.inline("▶ Start", f"inst:start:{inst.id}".encode()),
             Button.inline("⏹ Stop", f"inst:stop:{inst.id}".encode()),
             Button.inline("🔄 Restart", f"inst:restart:{inst.id}".encode())],
            [Button.inline("🗑 Delete", f"inst:delete:{inst.id}".encode())],
            [Button.inline("🔃 Refresh", f"inst:status:{inst.id}".encode()),
             Button.inline("⬅ Back", b"menu:selfbot")],
        ]
        await event.edit(text, buttons=buttons)

    async def _instance_set_enabled(self, event, instance_id: int, enabled: bool) -> None:
        """▶ Start / ⏹ Stop an instance (flip desired run-state)."""
        inst = await self._owned_instance(event, instance_id)
        if inst is None:
            return
        await self._central.set_instance_enabled(instance_id, enabled)
        await event.answer("Starting…" if enabled else "Stopping…")
        await self._panel_selfbot(event)

    async def _instance_restart(self, event, instance_id: int) -> None:
        """🔄 Restart an instance (supervisor honors the flag next tick)."""
        inst = await self._owned_instance(event, instance_id)
        if inst is None:
            return
        await self._central.set_instance_enabled(instance_id, True)
        await self._central.request_restart(instance_id)
        await event.answer("Restart requested")
        await self._instance_status(event, instance_id)

    async def _instance_add(self, event) -> None:
        """➕ Add Instance — plan-gated; begins onboarding for a new account."""
        await self._begin_onboarding(event)

    async def _instance_delete_confirm(self, event, instance_id: int) -> None:
        """🗑 Delete — show the confirmation screen (ownership re-validated).

        Refuses when the instance is still running or enabled (would be
        respawned): the user must stop it first.
        """
        inst = await self._owned_instance(event, instance_id)  # DB-validated ownership
        if inst is None:
            return
        if inst.status == INST_RUNNING or inst.enabled:
            await event.edit(
                "⚠ Please stop the instance before deleting it.",
                buttons=[[Button.inline("⬅ Back", f"inst:status:{inst.id}".encode())]],
            )
            return
        acct = f"@{inst.account_id}" if inst.account_id else "unlinked"
        text = (
            "⚠️ **Delete Instance**\n\n"
            f"{inst.name}\n"
            f"Account ID: {inst.account_id if inst.account_id else '—'}\n"
            f"Account: {acct}\n"
            f"Status: {inst.status.title()}\n\n"
            "This will permanently remove:\n"
            "• Session file\n"
            "• Instance database\n"
            "• Exports\n"
            "• Backups\n"
            "• Cache\n"
            "• Worker registration\n\n"
            "**This action cannot be undone.**"
        )
        buttons = [[
            Button.inline("❌ Cancel", f"inst:status:{inst.id}".encode()),
            Button.inline("🗑 Confirm Delete", f"inst:delok:{inst.id}".encode()),
        ]]
        await event.edit(text, buttons=buttons)

    async def _instance_delete_perform(self, event, instance_id: int) -> None:
        """Permanently delete an instance: files → DB row → audit (ownership re-checked).

        Ownership and run-state are re-validated from the database (never from
        the callback payload) so a stale button can't delete a running or
        foreign instance.
        """
        inst = await self._owned_instance(event, instance_id)  # re-validate ownership
        if inst is None:
            return
        if inst.status == INST_RUNNING or inst.enabled:
            await event.answer("Stop the instance before deleting it.", alert=True)
            await self._instance_status(event, instance_id)
            return

        # Resolve the owner's Telegram id from the DB for the storage path.
        owner = await self._central.get_user_by_pk(inst.user_id)
        owner_tg = owner.telegram_user_id if owner else inst.user_id

        # 1) Filesystem cleanup — recursive, missing-safe, logged.
        try:
            from sentinel.paths import InstancePaths

            paths = InstancePaths.for_instance(owner_tg, inst.id)
            removed = paths.purge()
            logger.info(
                "Instance %s files purged at %s (gone=%s)", inst.id, paths.root, removed
            )
        except Exception:  # noqa: BLE001 - never let cleanup failure abort delete
            logger.exception("Instance %s file cleanup failed", inst.id)

        # 2) Database cleanup — atomic (row + related bot_state).
        await self._central.delete_instance(inst.id)

        # 3) Audit trail.
        await self._audit(
            event.sender_id, "INSTANCE_DELETED",
            target_user=owner_tg, target_instance=inst.id,
            details=f"account_id={inst.account_id}",
        )
        logger.info(
            "Instance %s deleted by user %s (account_id=%s)",
            inst.id, event.sender_id, inst.account_id,
        )
        await event.answer("Instance deleted")
        await self._panel_selfbot(event)

    # ------------------------------------------------------------------ #
    # Gemini keys
    # ------------------------------------------------------------------ #
    async def _panel_keys(self, event, *, enabled: Optional[bool] = None) -> None:
        """🔑 Gemini Keys — list active/disabled with per-key controls."""
        user = await self._central.get_user(event.sender_id)
        if user is None:
            await event.edit("Send /start first.", buttons=self._back_button())
            return
        rows = await self._central.list_gemini_keys(user.id, enabled=enabled)
        scope = "Active" if enabled else ("Disabled" if enabled is False else "All")
        lines = [f"🔑 **Gemini Keys — {scope}**", ""]
        buttons: list = []
        if not rows:
            lines.append("No keys in this view.")
        for r in rows:
            state = "🟢" if r["enabled"] else "⚪"
            lines.append(f"{state} #{r['id']} {_mask_key(r['api_key'])} "
                         f"(fail={r['failure_count']})")
            ctrl = [Button.inline("🧪 Test", f"gk:test:{r['id']}".encode())]
            if r["enabled"]:
                ctrl.append(Button.inline("⏸ Disable", f"gk:disable:{r['id']}".encode()))
            else:
                ctrl.append(Button.inline("▶ Enable", f"gk:enable:{r['id']}".encode()))
            ctrl.append(Button.inline("🗑", f"gk:del:{r['id']}".encode()))
            buttons.append(ctrl)
        buttons.append([Button.inline("➕ Add Key", b"gk:add")])
        buttons.append([Button.inline("Active", b"gk:active"),
                        Button.inline("Disabled", b"gk:disabled")])
        buttons.append([Button.inline("⬅ Back", b"menu:home")])
        await event.edit("\n".join(lines), buttons=buttons)

    async def _key_add_prompt(self, event) -> None:
        """Prompt for a new Gemini key — enforcing the plan's key limit."""
        user = await self._central.get_user(event.sender_id)
        if user is None:
            await event.edit("Send /start first.", buttons=self._back_button())
            return
        plan = await self._central.plan_for_user(event.sender_id)
        limit = gemini_key_limit(plan)
        current = await self._central.count_gemini_keys(user.id)
        if 0 <= limit <= current:
            await event.edit(
                f"⚠ Key limit reached ({current}/{limit}) on plan **{plan.name if plan else '—'}**.\n"
                "Upgrade your plan to add more keys.",
                buttons=[[Button.inline("📦 Plans", b"menu:plans")],
                         [Button.inline("⬅ Back", b"menu:keys")]],
            )
            return
        await self._central.upsert_bot_state(
            event.sender_id, flow=FLOW_AWAITING_KEY, phase="key"
        )
        cap = "∞" if limit < 0 else limit
        await event.edit(
            f"🔑 Send your **Gemini API key** as a message. ({current}/{cap} used)\n"
            "_It will be stored securely and the message deleted._",
            buttons=[[Button.inline("⬅ Cancel", b"menu:keys")]],
        )

    async def _key_set_enabled(self, event, key_id: int, enabled: bool) -> None:
        """Enable/disable a key (never deletes)."""
        user = await self._central.get_user(event.sender_id)
        if user is not None:
            await self._central.set_gemini_key_enabled(user.id, key_id, enabled)
        await event.answer("Enabled" if enabled else "Disabled")
        await self._panel_keys(event)

    async def _key_delete(self, event, key_id: int) -> None:
        """Delete a key (explicit user action only; never automatic)."""
        user = await self._central.get_user(event.sender_id)
        if user is not None:
            await self._central.delete_gemini_key(user.id, key_id)
        await event.answer("Deleted")
        await self._panel_keys(event)

    async def _key_test(self, event, key_id: int) -> None:
        """Test a key with a real, tiny Gemini probe call."""
        user = await self._central.get_user(event.sender_id)
        if user is None:
            return
        rows = await self._central.list_gemini_keys(user.id)
        target = next((r for r in rows if r["id"] == key_id), None)
        if target is None:
            await event.answer("Key not found", alert=True)
            return
        await event.answer("Testing…")
        ok, detail = await self._probe_key(target["api_key"])
        if not ok:
            await self._central.mark_gemini_failure(user.id, target["api_key"])
        await event.edit(
            f"🧪 **Key #{key_id}** {_mask_key(target['api_key'])}\n\n"
            f"Result: {'✅ Valid' if ok else '❌ ' + detail}",
            buttons=[[Button.inline("⬅ Back", b"menu:keys")]],
        )

    async def _probe_key(self, api_key: str) -> tuple[bool, str]:
        """Return ``(ok, detail)`` from a minimal Gemini generate() call."""
        try:
            from ai.gemini import GeminiClient

            client = GeminiClient(api_key, timeout=20)
            await client.generate(self._default_model, [GeminiClient.text_part("ping")])
            return True, "ok"
        except Exception as exc:  # noqa: BLE001 - invalid/unavailable/etc.
            return False, type(exc).__name__

    # ------------------------------------------------------------------ #
    # Usage / Settings / Support
    # ------------------------------------------------------------------ #
    async def _panel_usage(self, event) -> None:
        """📈 Usage — aggregate counters across the user's instances."""
        user = await self._central.get_user(event.sender_id)
        if user is None:
            await event.edit("Send /start first.", buttons=self._back_button())
            return
        instances = await self._central.list_instances(user.id)
        totals: dict[str, int] = {}
        for inst in instances:
            for k, v in read_usage(inst.db_path).items():
                totals[k] = totals.get(k, 0) + int(v)
        lines = ["📈 **Usage** (all instances)", ""]
        if not instances:
            lines.append("No instances yet.")
        for key, label in _USAGE_LABELS:
            lines.append(f"• {label}: {totals.get(key, 0)}")
        await event.edit("\n".join(lines), buttons=self._back_button())

    async def _panel_settings(self, event) -> None:
        """⚙ Settings — command prefix."""
        user = await self._central.get_user(event.sender_id)
        prefix = user.prefix if user else "."
        await event.edit(
            f"⚙ **Settings**\n\nCommand prefix: `{prefix}`",
            buttons=[
                [Button.inline("✏ Change Prefix", b"set:prefix")],
                [Button.inline("⬅ Back", b"menu:home")],
            ],
        )

    async def _prefix_prompt(self, event) -> None:
        """Prompt for a new command prefix (next message consumed)."""
        await self._central.upsert_bot_state(
            event.sender_id, flow=FLOW_AWAITING_PREFIX, phase="prefix"
        )
        await event.edit(
            "✏ Send the new command prefix (1–3 non-space characters, e.g. `.`)",
            buttons=[[Button.inline("⬅ Cancel", b"menu:settings")]],
        )

    async def _panel_support(self, event) -> None:
        """🆘 Support — static help."""
        await event.edit(
            "🆘 **Support**\n\n"
            f"Contact: {self._support}\n\n"
            "• /start — open the panel\n"
            "• /cancel — abort the current input\n"
            "Use the buttons to manage your selfbot, plan and keys.",
            buttons=self._back_button(),
        )

    # ------------------------------------------------------------------ #
    # Onboarding wizard (persisted)
    # ------------------------------------------------------------------ #
    async def _begin_onboarding(self, event) -> None:
        """Create a pending instance (plan-gated) and ask for the phone number."""
        user = await self._central.get_user(event.sender_id)
        if user is None:
            user = await self._central.create_user(
                event.sender_id, plan_name=self._default_plan, status=USER_ACTIVE
            )
        # Block instance creation when the account is suspended or the
        # subscription has lapsed (premium gate), unless a legacy plan applies.
        if user.status != USER_ACTIVE:
            await self._reply(event, "⚠ Your account is suspended.", buttons=self._back_button())
            return
        active_sub = await self._central.get_active_subscription(user.id)
        latest = await self._central.get_latest_subscription(user.id)
        if active_sub is None and latest is not None:
            await self._reply(
                event,
                "⚠ Your subscription has expired. Tap 📦 Plans to renew before adding instances.",
                buttons=[[Button.inline("📦 Plans", b"menu:plans")], *self._back_button()],
            )
            return

        plan = await self._central.plan_for_user(event.sender_id)
        limit = instance_limit(plan)
        count = await self._central.count_instances(user.id)
        if 0 <= limit <= count:
            await self._reply(
                event,
                f"⚠ Instance limit reached ({count}/{limit}). Upgrade your plan to add more.",
                buttons=[[Button.inline("📦 Plans", b"menu:plans")], *self._back_button()],
            )
            return

        name = f"Instance #{count + 1}"
        # Create the row first to obtain its id, then compute per-instance paths.
        inst = await self._central.create_instance(
            user.id, name, session_path="", db_path=""
        )
        paths = InstancePaths.for_instance(user.telegram_user_id, inst.id).ensure()
        await self._central.set_instance_paths(inst.id, paths.session, paths.db_path)

        client = TelegramClient(paths.session_name, self._api_id, self._api_hash)
        await client.connect()
        self._logins[event.sender_id] = client
        await self._central.upsert_bot_state(
            event.sender_id, flow=FLOW_ONBOARDING, phase=_PHASE_PHONE,
            session_path=paths.session, target_instance_id=inst.id, tos_accepted=True,
        )
        await self._reply(
            event,
            f"📱 **{name}** — send the account's phone number in international "
            "format, e.g. `+15551234567`",
            buttons=[[Button.inline("⬅ Cancel", b"menu:selfbot")]],
        )

    async def _on_message(self, event: events.NewMessage.Event) -> None:
        """Route a non-command message by the persisted flow (bot_state)."""
        state = await self._central.get_bot_state(event.sender_id)
        if state is None:
            return
        flow = state["flow"]
        try:
            if flow == _FLOW_ADMIN:
                await self._consume_admin_input(event, state)
                return
            if flow == _FLOW_TICKET:
                await self._consume_ticket_input(event, state)
                return
            if flow == FLOW_AWAITING_KEY:
                await self._consume_key(event)
            elif flow == FLOW_AWAITING_PREFIX:
                await self._consume_prefix(event)
            elif flow == FLOW_ONBOARDING:
                await self._consume_onboarding(event, state)
        except FloodWaitError as exc:
            await event.respond(f"⏳ Rate limited. Wait {int(exc.seconds)}s and retry.")
        except Exception:  # noqa: BLE001 - never crash on user input
            logger.exception("Input handling failed for %s", event.sender_id)
            await self._clear_flow(event.sender_id)
            await event.respond("⚠ Something went wrong. Send /start to reopen the panel.")

    async def _consume_key(self, event) -> None:
        """Store a submitted Gemini key and delete the message."""
        user = await self._central.get_user(event.sender_id)
        api_key = event.raw_text.strip()
        await self._delete_message(event)  # secret: remove immediately
        if user is not None and api_key:
            await self._central.add_gemini_key(user.id, api_key)
        await self._central.delete_bot_state(event.sender_id)
        await event.respond("✅ Key added.", buttons=[[Button.inline("🔑 Keys", b"menu:keys")]])

    async def _consume_prefix(self, event) -> None:
        """Validate + store a new command prefix."""
        prefix = event.raw_text.strip()
        if not (1 <= len(prefix) <= 3) or any(c.isspace() for c in prefix):
            await event.respond("⚠ Prefix must be 1–3 non-space characters. Try again or /cancel.")
            return
        await self._central.set_user_prefix(event.sender_id, prefix)
        await self._central.delete_bot_state(event.sender_id)
        await event.respond(
            f"✅ Prefix set to `{prefix}`. Restart your selfbot to apply.",
            buttons=[[Button.inline("⬅ Panel", b"menu:home")]],
        )

    async def _consume_onboarding(self, event, state) -> None:
        """Advance the phone → code → 2FA wizard using persisted state."""
        phase = state["phase"]
        client = self._logins.get(event.sender_id)
        if client is None:
            await event.respond("Session expired. Tap ➕ Add Instance to restart.")
            await self._clear_flow(event.sender_id)
            return
        if phase == _PHASE_PHONE:
            await self._ob_phone(event, state, client)
        elif phase == _PHASE_CODE:
            await self._ob_code(event, state, client)
        elif phase == _PHASE_PASSWORD:
            await self._ob_password(event, state, client)

    async def _ob_phone(self, event, state, client) -> None:
        """Send the login code to the submitted phone number."""
        phone = event.raw_text.strip()
        try:
            sent = await client.send_code_request(phone)
        except PhoneNumberInvalidError:
            await event.respond("⚠ Invalid phone number. Send it in international format.")
            return
        await self._central.upsert_bot_state(
            event.sender_id, flow=FLOW_ONBOARDING, phase=_PHASE_CODE,
            phone=phone, phone_code_hash=sent.phone_code_hash,
            session_path=state["session_path"],
            target_instance_id=state["target_instance_id"], tos_accepted=True,
        )
        await self._central.set_auth_state(state["target_instance_id"], AUTH_CODE_SENT)
        await event.respond(
            "💬 Enter the login code Telegram sent.\n"
            "Add spaces (e.g. `1 2 3 4 5`) so Telegram doesn't invalidate it."
        )

    async def _ob_code(self, event, state, client) -> None:
        """Sign in with the OTP code; branch to 2FA if required."""
        code = event.raw_text.replace(" ", "").strip()
        await self._delete_message(event)  # secret: remove the code message
        try:
            await client.sign_in(
                phone=state["phone"], code=code, phone_code_hash=state["phone_code_hash"]
            )
        except SessionPasswordNeededError:
            await self._central.upsert_bot_state(
                event.sender_id, flow=FLOW_ONBOARDING, phase=_PHASE_PASSWORD,
                target_instance_id=state["target_instance_id"], tos_accepted=True,
            )
            await self._central.set_auth_state(state["target_instance_id"], "awaiting_2fa")
            await event.respond("🔐 Two-step verification is on. Send your 2FA password.")
            return
        except (PhoneCodeInvalidError, PhoneCodeExpiredError):
            await event.respond("⚠ Code invalid/expired. Tap ➕ Add Instance to restart.")
            await self._clear_flow(event.sender_id)
            return
        await self._finish_onboarding(event, state, client)

    async def _ob_password(self, event, state, client) -> None:
        """Complete sign-in using the 2FA password."""
        password = event.raw_text
        await self._delete_message(event)  # secret: remove the password message
        try:
            await client.sign_in(password=password)
        except Exception:  # noqa: BLE001 - wrong password etc.
            await event.respond("⚠ Incorrect password. Send it again, or /cancel.")
            return
        await self._finish_onboarding(event, state, client)

    async def _finish_onboarding(self, event, state, client) -> None:
        """Finalize: link the account, activate the instance, clean up state."""
        instance_id = state["target_instance_id"]
        me = await client.get_me()
        account_id = int(me.id)
        await client.disconnect()
        self._logins.pop(event.sender_id, None)

        await self._central.set_instance_account(instance_id, account_id, auth_state=AUTH_ACTIVE)
        await self._central.set_instance_enabled(instance_id, True)
        await self._central.delete_bot_state(event.sender_id)
        logger.info("Onboarded instance %s (account=%s)", instance_id, account_id)
        await event.respond(
            f"✅ **Session created** for @{getattr(me, 'username', None) or account_id}!\n"
            "Your selfbot worker is starting…",
            buttons=[[Button.inline("🤖 My Selfbot", b"menu:selfbot")]],
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    async def _owned_instance(self, event, instance_id: int):
        """Return the instance if it belongs to the caller, else answer + None."""
        user = await self._central.get_user(event.sender_id)
        inst = await self._central.get_instance(instance_id)
        if user is None or inst is None or inst.user_id != user.id:
            await event.answer("Not your instance.", alert=True)
            return None
        return inst

    async def _clear_flow(self, chat_id: int) -> None:
        """Drop any persisted flow + live login client for a chat."""
        await self._central.delete_bot_state(chat_id)
        await self._drop_login(chat_id)

    async def _drop_login(self, chat_id: int) -> None:
        """Disconnect + forget a chat's live onboarding login client."""
        client = self._logins.pop(chat_id, None)
        if client is not None:
            try:
                if client.is_connected():
                    await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def _delete_message(self, event) -> None:
        """Delete the user's message (used for OTP / 2FA / API-key secrets)."""
        try:
            await event.delete()
        except Exception:  # noqa: BLE001 - best-effort
            pass

    async def _reply(self, event, text: str, *, buttons=None) -> None:
        """Reply to a NewMessage or CallbackQuery uniformly (new message)."""
        try:
            await event.respond(text, buttons=buttons, link_preview=False)
        except Exception:  # noqa: BLE001
            logger.debug("reply failed")

    # ================================================================== #
    # USER — Plans catalog + subscription request
    # ================================================================== #
    async def _user_plans(self, event) -> None:
        """📦 Plans — list active plans; tapping one files an upgrade request."""
        user = await self._central.get_user(event.sender_id)
        if user is None:
            await event.edit("Send /start first.", buttons=self._back_button())
            return
        plans = await self._central.list_plans(active_only=True)
        lines = ["📦 **Plans**", "", "Tap a plan to request it (an admin approves)."]
        buttons: list = []
        for plan in plans:
            keys = await self._central.get_plan_feature_keys(plan.id)
            mi = plan.limit_for("max_instances")
            lines.append(f"\n**{plan.name}** — {len(keys)} features, "
                         f"{'∞' if mi < 0 else mi} instance(s)")
            buttons.append([Button.inline(f"Request {plan.name}", f"plan:req:{plan.id}".encode())])
        buttons.append([Button.inline("⬅ Back", b"menu:home")])
        await event.edit("\n".join(lines), buttons=buttons)

    async def _user_request_plan(self, event, plan_id: int) -> None:
        """Create a pending subscription request for the chosen plan."""
        user = await self._central.get_user(event.sender_id)
        plan = await self._central.get_plan_by_id(plan_id)
        if user is None or plan is None:
            await event.answer("Plan unavailable.", alert=True)
            return
        await self._central.create_request(user.id, plan_id)
        logger.info("User %s requested plan %s", user.telegram_user_id, plan.name)
        await event.edit(
            f"✅ Request submitted for **{plan.name}**.\n"
            "An admin will review it shortly.",
            buttons=self._back_button(),
        )
        await self._notify_admins_new_request(user, plan.name)

    async def _notify_admins_new_request(self, user, plan_name: str) -> None:
        """Best-effort push to admins that a new request arrived."""
        try:
            for row in await self._central.list_admins():
                try:
                    await self._bot.send_message(
                        int(row["telegram_user_id"]),
                        f"📝 New request: @{user.username or user.telegram_user_id} → {plan_name}\n"
                        "Open /admin → 📝 Requests to review.",
                    )
                except Exception:  # noqa: BLE001 - admin may not have DM'd the bot
                    continue
        except Exception:  # noqa: BLE001
            logger.debug("admin notify failed")

    # ================================================================== #
    # ADMIN PANEL
    # ================================================================== #
    async def _on_admin(self, event: events.NewMessage.Event) -> None:
        """/admin — open the admin panel (staff only)."""
        if not await self._is_staff(event.sender_id):
            await event.respond("⛔ Admins only.")
            return
        await self._admin_menu(event, new=True)

    async def _admin_menu(self, event, *, new: bool = False) -> None:
        """Render the main admin menu."""
        pending = await self._central.count_pending_requests()
        open_tickets = await self._central.count_open_tickets()
        text = "🛠 **Admin Panel**"
        buttons = [
            [Button.inline("📊 Platform Overview", b"adm:dash")],
            [Button.inline("👥 Users", b"adm:users"),
             Button.inline("📦 Plans", b"adm:plans")],
            [Button.inline(f"📝 Requests ({pending})", b"adm:requests"),
             Button.inline("⚙ Features", b"adm:features")],
            [Button.inline(f"🎫 Support ({open_tickets})", b"adm:tickets"),
             Button.inline("📜 Audit Logs", b"adm:audit")],
            [Button.inline("👑 Admins", b"adm:admins"),
             Button.inline("📊 Statistics", b"adm:stats")],
            [Button.inline("📢 Broadcast", b"adm:broadcast")],
        ]
        if new:
            await self._reply(event, text, buttons=buttons)
        else:
            await event.edit(text, buttons=buttons)

    async def _on_admin_callback(self, event, data: str) -> None:
        """Route ``adm:*`` callbacks with per-action permission checks."""
        if not await self._is_staff(event.sender_id):
            await event.answer("⛔ Admins only.", alert=True)
            return
        parts = data.split(":")  # adm:<section>[:<arg>...]
        section = parts[1] if len(parts) > 1 else "home"

        if section == "home":
            await self._admin_menu(event)
        elif section == "users":
            await self._require(event, PERM_USERS, self._admin_users)
        elif section == "user":
            await self._require(event, PERM_USERS, lambda e: self._admin_user_view(e, int(parts[2])))
        elif section == "ususpend":
            await self._require(event, PERM_USERS, lambda e: self._admin_user_status(e, int(parts[2]), False))
        elif section == "uresume":
            await self._require(event, PERM_USERS, lambda e: self._admin_user_status(e, int(parts[2]), True))
        elif section == "uextend":
            await self._require(event, PERM_USERS, lambda e: self._admin_user_extend(e, int(parts[2]), int(parts[3])))
        elif section == "uplan":
            await self._require(event, PERM_USERS, lambda e: self._admin_user_change_plan(e, int(parts[2]), int(parts[3])))
        elif section == "plans":
            await self._require(event, PERM_PLANS, self._admin_plans)
        elif section == "plan":
            await self._require(event, PERM_PLANS, lambda e: self._admin_plan_view(e, int(parts[2])))
        elif section == "pnew":
            await self._require(event, PERM_PLANS, self._admin_plan_new)
        elif section == "prename":
            await self._require(event, PERM_PLANS, lambda e: self._admin_plan_input(e, "rename", int(parts[2])))
        elif section == "pmax":
            await self._require(event, PERM_PLANS, lambda e: self._admin_plan_input(e, "maxinst", int(parts[2])))
        elif section == "pactive":
            await self._require(event, PERM_PLANS, lambda e: self._admin_plan_toggle_active(e, int(parts[2])))
        elif section == "pdel":
            await self._require(event, PERM_PLANS, lambda e: self._admin_plan_delete(e, int(parts[2])))
        elif section == "pfeat":
            await self._require(event, PERM_PLANS, lambda e: self._admin_plan_features(e, int(parts[2])))
        elif section == "pftog":
            await self._require(event, PERM_PLANS, lambda e: self._admin_plan_feature_toggle(e, int(parts[2]), parts[3]))
        elif section == "requests":
            await self._require(event, PERM_REQUESTS, self._admin_requests)
        elif section == "req":
            await self._require(event, PERM_REQUESTS, lambda e: self._admin_request_view(e, int(parts[2])))
        elif section == "rapp":
            await self._require(event, PERM_REQUESTS, lambda e: self._admin_request_durations(e, int(parts[2])))
        elif section == "rdur":
            await self._require(event, PERM_REQUESTS, lambda e: self._admin_request_approve(e, int(parts[2]), int(parts[3])))
        elif section == "rrej":
            await self._require(event, PERM_REQUESTS, lambda e: self._admin_request_reject(e, int(parts[2])))
        elif section == "features":
            await self._require(event, PERM_FEATURES, self._admin_features)
        elif section == "admins":
            await self._require(event, PERM_ADMINS, self._admin_admins)
        elif section == "aadd":
            await self._require(event, PERM_ADMINS, self._admin_add_prompt)
        elif section == "arem":
            await self._require(event, PERM_ADMINS, lambda e: self._admin_remove(e, int(parts[2])))
        elif section == "arole":
            await self._require(event, PERM_ADMINS, lambda e: self._admin_set_role(e, int(parts[2]), parts[3]))
        elif section == "stats":
            await self._require(event, PERM_STATS, self._admin_stats)
        elif section == "broadcast":
            await self._require(event, PERM_BROADCAST, self._admin_broadcast_prompt)
        # ---- Phase 1: dashboard ----
        elif section == "dash":
            await self._require(event, PERM_MONITORING, self._admin_dashboard)
        # ---- Phase 2/3: live instance monitor + remote control ----
        elif section == "imon":
            await self._require(event, PERM_MONITORING, lambda e: self._admin_instance_monitor(e, int(parts[2])))
        elif section == "istart":
            await self._require(event, PERM_INSTANCE_CONTROL, lambda e: self._admin_instance_control(e, int(parts[2]), "start"))
        elif section == "istop":
            await self._require(event, PERM_INSTANCE_CONTROL, lambda e: self._admin_instance_control(e, int(parts[2]), "stop"))
        elif section == "irestart":
            await self._require(event, PERM_INSTANCE_CONTROL, lambda e: self._admin_instance_control(e, int(parts[2]), "restart"))
        elif section == "ibackup":
            await self._require(event, PERM_INSTANCE_CONTROL, lambda e: self._admin_instance_backup(e, int(parts[2])))
        elif section == "iexport":
            await self._require(event, PERM_INSTANCE_CONTROL, lambda e: self._admin_instance_export(e, int(parts[2])))
        elif section == "ilogs":
            await self._require(event, PERM_INSTANCE_CONTROL, lambda e: self._admin_instance_logs(e, int(parts[2])))
        # ---- Phase 4: support center ----
        elif section == "tickets":
            await self._require(event, PERM_TICKETS, self._admin_tickets)
        elif section == "tclosed":
            await self._require(event, PERM_TICKETS, lambda e: self._admin_tickets(e, closed=True))
        elif section == "tview":
            await self._require(event, PERM_TICKETS, lambda e: self._admin_ticket_view(e, int(parts[2])))
        elif section == "treply":
            await self._require(event, PERM_TICKETS, lambda e: self._admin_ticket_reply_prompt(e, int(parts[2])))
        elif section == "tclose":
            await self._require(event, PERM_TICKETS, lambda e: self._admin_ticket_status(e, int(parts[2]), TICKET_CLOSED))
        elif section == "treopen":
            await self._require(event, PERM_TICKETS, lambda e: self._admin_ticket_status(e, int(parts[2]), TICKET_OPEN))
        elif section == "tassign":
            await self._require(event, PERM_TICKETS, lambda e: self._admin_ticket_assign(e, int(parts[2])))
        # ---- Phase 6: audit logs ----
        elif section == "audit":
            await self._require(event, PERM_AUDIT, self._admin_audit)
        else:
            await event.answer()

    async def _require(self, event, permission: str, handler) -> None:
        """Run ``handler`` only if the caller has ``permission``."""
        if not await self._can(event.sender_id, permission):
            await event.answer("⛔ Insufficient permission.", alert=True)
            return
        await handler(event)

    # ---- Users -------------------------------------------------------- #
    async def _admin_users(self, event) -> None:
        """List users with plan + subscription status + instance count."""
        users = await self._central.list_users()
        lines = ["👥 **Users**", ""]
        buttons: list = []
        for u in users[:25]:
            sub = await self._central.get_active_subscription(u.id)
            sub_state = sub["status"] if sub else "none"
            n = await self._central.count_instances(u.id)
            lines.append(f"`{u.telegram_user_id}` @{u.username or '-'} | {u.plan_name or '-'} "
                         f"| {u.status}/{sub_state} | {n} inst")
            buttons.append([Button.inline(f"View {u.telegram_user_id}", f"adm:user:{u.telegram_user_id}".encode())])
        buttons.append([Button.inline("⬅ Back", b"adm:home")])
        await event.edit("\n".join(lines), buttons=buttons)

    async def _admin_user_view(self, event, tg_id: int) -> None:
        """Show one user with lifecycle actions."""
        u = await self._central.get_user(tg_id)
        if u is None:
            await event.answer("No such user.", alert=True)
            return
        sub = await self._central.get_active_subscription(u.id)
        n = await self._central.count_instances(u.id)
        expiry = _fmt_date(sub["expires_at"]) if sub and sub["expires_at"] else ("never" if sub else "—")
        lines = [
            f"👤 **User `{u.telegram_user_id}`**",
            f"Username: @{u.username or '-'}",
            f"Plan: {u.plan_name or '-'}",
            f"Account: {u.status}",
            f"Subscription: {sub['status'] if sub else 'none'} (exp {expiry})",
            f"Instances: {n}",
        ]
        plans = await self._central.list_plans(active_only=True)
        buttons = [
            [Button.inline("⏸ Suspend", f"adm:ususpend:{tg_id}".encode()),
             Button.inline("▶ Unsuspend", f"adm:uresume:{tg_id}".encode())],
            [Button.inline("+30d", f"adm:uextend:{tg_id}:30".encode()),
             Button.inline("+90d", f"adm:uextend:{tg_id}:90".encode()),
             Button.inline("+365d", f"adm:uextend:{tg_id}:365".encode())],
        ]
        # Change-plan buttons (one per active plan).
        plan_row: list = []
        for p in plans:
            plan_row.append(Button.inline(p.name, f"adm:uplan:{tg_id}:{p.id}".encode()))
            if len(plan_row) == 3:
                buttons.append(plan_row); plan_row = []
        if plan_row:
            buttons.append(plan_row)
        # Per-instance monitor buttons (Users → User → Instances).
        for inst in await self._central.list_instances(u.id):
            dot = "🟢" if inst.status == "running" else "⚪"
            buttons.append([Button.inline(f"{dot} {inst.name} (#{inst.id})",
                                          f"adm:imon:{inst.id}".encode())])
        buttons.append([Button.inline("⬅ Back", b"adm:users")])
        await event.edit("\n".join(lines), buttons=buttons)

    async def _admin_user_status(self, event, tg_id: int, active: bool) -> None:
        """Suspend/unsuspend a user (account + subscription)."""
        await self._central.set_user_status(tg_id, USER_ACTIVE if active else USER_SUSPENDED)
        u = await self._central.get_user(tg_id)
        if u is not None:
            if active:
                await self._central.resume_user_subscription(u.id)
            else:
                await self._central.suspend_user_subscription(u.id)
                for inst in await self._central.list_instances(u.id):
                    await self._central.set_instance_enabled(inst.id, False)
        self._gate.invalidate(tg_id)
        await event.answer("Updated")
        await self._admin_user_view(event, tg_id)

    async def _admin_user_extend(self, event, tg_id: int, days: int) -> None:
        """Extend a user's subscription by ``days``."""
        u = await self._central.get_user(tg_id)
        if u is not None:
            ok = await self._central.extend_subscription(u.id, days)
            self._gate.invalidate(tg_id)
            await event.answer(f"Extended +{days}d" if ok else "No subscription to extend", alert=not ok)
        await self._admin_user_view(event, tg_id)

    async def _admin_user_change_plan(self, event, tg_id: int, plan_id: int) -> None:
        """Change a user's plan immediately (creates a perpetual subscription)."""
        u = await self._central.get_user(tg_id)
        if u is not None:
            await self._central.create_subscription(
                u.id, plan_id, duration_days=None, approved_by=event.sender_id
            )
            self._gate.invalidate(tg_id)
            await self._audit(event.sender_id, "plan.change", target_user=tg_id,
                              details=f"plan_id={plan_id}")
        await event.answer("Plan changed")
        await self._admin_user_view(event, tg_id)

    # ---- Plans + editor ---------------------------------------------- #
    async def _admin_plans(self, event) -> None:
        """List plans with create + per-plan view."""
        plans = await self._central.list_plans()
        lines = ["📦 **Plans**", ""]
        buttons: list = []
        for p in plans:
            row = await self._central.get_plan_row(p.id)
            active = "🟢" if (row and row["active"]) else "⚪"
            lines.append(f"{active} **{p.name}** — {p.limit_for('max_instances') if p.limit_for('max_instances')>=0 else '∞'} inst")
            buttons.append([Button.inline(f"Edit {p.name}", f"adm:plan:{p.id}".encode())])
        buttons.append([Button.inline("➕ Create Plan", b"adm:pnew")])
        buttons.append([Button.inline("⬅ Back", b"adm:home")])
        await event.edit("\n".join(lines), buttons=buttons)

    async def _admin_plan_view(self, event, plan_id: int) -> None:
        """The plan editor (rename / max_instances / active / features / delete)."""
        row = await self._central.get_plan_row(plan_id)
        if row is None:
            await event.answer("No such plan.", alert=True)
            return
        keys = await self._central.get_plan_feature_keys(plan_id)
        mi = row["max_instances"]
        lines = [
            f"✏ **Plan: {row['name']}**",
            f"Description: {row['description'] or '—'}",
            f"Max instances: {mi if mi is not None and mi >= 0 else '∞'}",
            f"Active: {'yes' if row['active'] else 'no'}",
            f"Features ({len(keys)}): {', '.join(sorted(keys)) or 'none'}",
        ]
        buttons = [
            [Button.inline("✏ Rename", f"adm:prename:{plan_id}".encode()),
             Button.inline("🔢 Max Instances", f"adm:pmax:{plan_id}".encode())],
            [Button.inline("🔀 Toggle Active", f"adm:pactive:{plan_id}".encode()),
             Button.inline("⚙ Features", f"adm:pfeat:{plan_id}".encode())],
            [Button.inline("🗑 Delete", f"adm:pdel:{plan_id}".encode())],
            [Button.inline("⬅ Back", b"adm:plans")],
        ]
        await event.edit("\n".join(lines), buttons=buttons)

    async def _admin_plan_new(self, event) -> None:
        """Prompt for a new plan name (admin input flow)."""
        await self._central.upsert_bot_state(
            event.sender_id, flow=_FLOW_ADMIN, phase="plan_new"
        )
        await event.edit("➕ Send the new plan's **name**.",
                         buttons=[[Button.inline("⬅ Cancel", b"adm:plans")]])

    async def _admin_plan_input(self, event, action: str, plan_id: int) -> None:
        """Prompt for plan rename / max-instances (admin input flow)."""
        prompt = {"rename": "Send the new plan **name**.",
                  "maxinst": "Send the new **max instances** (integer, -1 = unlimited)."}[action]
        await self._central.upsert_bot_state(
            event.sender_id, flow=_FLOW_ADMIN, phase=f"plan_{action}", payload=str(plan_id)
        )
        await event.edit(f"✏ {prompt}",
                         buttons=[[Button.inline("⬅ Cancel", f"adm:plan:{plan_id}".encode())]])

    async def _admin_plan_toggle_active(self, event, plan_id: int) -> None:
        """Toggle a plan's active flag."""
        row = await self._central.get_plan_row(plan_id)
        if row is not None:
            await self._central.set_plan_active(plan_id, not row["active"])
        await event.answer("Toggled")
        await self._admin_plan_view(event, plan_id)

    async def _admin_plan_delete(self, event, plan_id: int) -> None:
        """Delete a plan (refuses when still assigned to users/subs)."""
        refs = await self._central.plan_in_use(plan_id)
        if sum(refs.values()) > 0:
            detail = ", ".join(f"{t}={n}" for t, n in refs.items() if n)
            await event.answer(
                f"Plan is in use ({detail}). Reassign first.", alert=True,
            )
            return
        await self._central.delete_plan(plan_id)
        await self._audit(event.sender_id, "plan.delete", details=f"plan_id={plan_id}")
        await event.answer("Deleted")
        await self._admin_plans(event)

    async def _admin_plan_features(self, event, plan_id: int) -> None:
        """Attach/detach features to a plan (checkbox-style toggles)."""
        row = await self._central.get_plan_row(plan_id)
        if row is None:
            await event.answer("No such plan.", alert=True)
            return
        granted = await self._central.get_plan_feature_keys(plan_id)
        features = await self._central.list_features()
        lines = [f"⚙ **Features for {row['name']}**", "", "Tap to toggle:"]
        buttons: list = []
        rowbuf: list = []
        for f in features:
            mark = "✅" if f["key"] in granted else "▫"
            rowbuf.append(Button.inline(f"{mark} {f['key']}", f"adm:pftog:{plan_id}:{f['key']}".encode()))
            if len(rowbuf) == 2:
                buttons.append(rowbuf); rowbuf = []
        if rowbuf:
            buttons.append(rowbuf)
        buttons.append([Button.inline("⬅ Back", f"adm:plan:{plan_id}".encode())])
        await event.edit("\n".join(lines), buttons=buttons)

    async def _admin_plan_feature_toggle(self, event, plan_id: int, feature_key: str) -> None:
        """Toggle one feature on a plan."""
        granted = await self._central.get_plan_feature_keys(plan_id)
        if feature_key in granted:
            await self._central.detach_feature(plan_id, feature_key)
            action = "feature.detach"
        else:
            await self._central.attach_feature(plan_id, feature_key)
            action = "feature.attach"
        self._gate.invalidate()  # plan membership changed
        await self._audit(event.sender_id, action, details=f"plan_id={plan_id} feature={feature_key}")
        await self._admin_plan_features(event, plan_id)

    # ---- Requests + approval ----------------------------------------- #
    async def _admin_requests(self, event) -> None:
        """List pending requests."""
        reqs = await self._central.list_requests(status=REQ_PENDING)
        lines = ["📝 **Pending Requests**", ""]
        buttons: list = []
        if not reqs:
            lines.append("None.")
        for r in reqs[:25]:
            u = await self._central.get_user_by_pk(r["user_id"])
            p = await self._central.get_plan_by_id(r["requested_plan_id"])
            who = u.telegram_user_id if u else r["user_id"]
            lines.append(f"#{r['id']} — user `{who}` → {p.name if p else r['requested_plan_id']}")
            buttons.append([Button.inline(f"Review #{r['id']}", f"adm:req:{r['id']}".encode())])
        buttons.append([Button.inline("⬅ Back", b"adm:home")])
        await event.edit("\n".join(lines), buttons=buttons)

    async def _admin_request_view(self, event, req_id: int) -> None:
        """Show a request with Approve/Reject."""
        r = await self._central.get_request(req_id)
        if r is None:
            await event.answer("No such request.", alert=True)
            return
        u = await self._central.get_user_by_pk(r["user_id"])
        p = await self._central.get_plan_by_id(r["requested_plan_id"])
        lines = [
            f"📝 **Request #{req_id}**",
            f"User: `{u.telegram_user_id if u else r['user_id']}` (@{u.username if u else '-'})",
            f"Plan: {p.name if p else r['requested_plan_id']}",
            f"Requested duration: {r['duration_days'] or 'admin choice'}",
            f"Status: {r['status']}",
        ]
        buttons = [
            [Button.inline("✅ Approve", f"adm:rapp:{req_id}".encode()),
             Button.inline("❌ Reject", f"adm:rrej:{req_id}".encode())],
            [Button.inline("⬅ Back", b"adm:requests")],
        ]
        await event.edit("\n".join(lines), buttons=buttons)

    async def _admin_request_durations(self, event, req_id: int) -> None:
        """Offer subscription duration presets for approval."""
        buttons: list = []
        rowbuf: list = []
        for label, days in _DURATIONS:
            rowbuf.append(Button.inline(label, f"adm:rdur:{req_id}:{days}".encode()))
            if len(rowbuf) == 3:
                buttons.append(rowbuf); rowbuf = []
        if rowbuf:
            buttons.append(rowbuf)
        buttons.append([Button.inline("⬅ Back", f"adm:req:{req_id}".encode())])
        await event.edit("⏳ Choose subscription duration:", buttons=buttons)

    async def _admin_request_approve(self, event, req_id: int, days: int) -> None:
        """Approve a request → create subscription (days=0 → custom prompt)."""
        r = await self._central.get_request(req_id)
        if r is None or r["status"] != REQ_PENDING:
            await event.answer("Already processed.", alert=True)
            return
        if days == 0:  # custom → ask for a number
            await self._central.upsert_bot_state(
                event.sender_id, flow=_FLOW_ADMIN, phase="req_custom", payload=str(req_id)
            )
            await event.edit("Send the custom duration in **days** (integer).",
                             buttons=[[Button.inline("⬅ Cancel", f"adm:req:{req_id}".encode())]])
            return
        await self._approve_request(req_id, days, event.sender_id)
        await event.answer(f"Approved for {days}d")
        await self._admin_requests(event)

    async def _approve_request(self, req_id: int, days: int, admin_id: int) -> None:
        """Shared approval: create subscription + mark request + notify user."""
        r = await self._central.get_request(req_id)
        if r is None:
            return
        u = await self._central.get_user_by_pk(r["user_id"])
        await self._central.create_subscription(
            r["user_id"], r["requested_plan_id"], duration_days=days, approved_by=admin_id
        )
        await self._central.set_request_status(req_id, REQ_APPROVED, processed_by=admin_id)
        await self._audit(admin_id, "subscription.approve",
                          target_user=r["user_id"], details=f"plan_id={r['requested_plan_id']} {days}d")
        if u is not None:
            self._gate.invalidate(u.telegram_user_id)
            p = await self._central.get_plan_by_id(r["requested_plan_id"])
            try:
                await self._bot.send_message(
                    u.telegram_user_id,
                    f"✅ Your subscription to **{p.name if p else 'your plan'}** is active "
                    f"for {days} day(s)!",
                )
            except Exception:  # noqa: BLE001
                pass

    async def _admin_request_reject(self, event, req_id: int) -> None:
        """Reject a request."""
        r = await self._central.get_request(req_id)
        if r and r["status"] == REQ_PENDING:
            await self._central.set_request_status(req_id, REQ_REJECTED, processed_by=event.sender_id)
            u = await self._central.get_user_by_pk(r["user_id"])
            if u is not None:
                try:
                    await self._bot.send_message(u.telegram_user_id, "❌ Your plan request was declined.")
                except Exception:  # noqa: BLE001
                    pass
        await event.answer("Rejected")
        await self._admin_requests(event)

    # ---- Features ----------------------------------------------------- #
    async def _admin_features(self, event) -> None:
        """List all registered features (read-only registry view)."""
        features = await self._central.list_features()
        lines = ["⚙ **Registered Features**", ""]
        for f in features:
            lines.append(f"• `{f['key']}` — {f['title']}")
        lines += ["", "Attach/detach features per plan via 📦 Plans → Features."]
        await event.edit("\n".join(lines), buttons=[[Button.inline("⬅ Back", b"adm:home")]])

    # ---- Admins (RBAC) ------------------------------------------------ #
    async def _admin_admins(self, event) -> None:
        """List admins; owner can add/remove/assign roles."""
        is_owner = await self._is_owner(event.sender_id)
        admins = await self._central.list_admins()
        roles = [r["name"] for r in await self._central.list_roles()]
        lines = ["👑 **Admins**", ""]
        buttons: list = []
        for a in admins:
            lines.append(f"`{a['telegram_user_id']}` — {a['role']}")
            if is_owner:
                brow = [Button.inline("🗑", f"adm:arem:{a['telegram_user_id']}".encode())]
                for role in roles:
                    brow.append(Button.inline(role, f"adm:arole:{a['telegram_user_id']}:{role}".encode()))
                buttons.append(brow[:4])
        if is_owner:
            buttons.append([Button.inline("➕ Add Admin", b"adm:aadd")])
        else:
            lines.append("\n_Only the owner can modify admins._")
        buttons.append([Button.inline("⬅ Back", b"adm:home")])
        await event.edit("\n".join(lines), buttons=buttons)

    async def _admin_add_prompt(self, event) -> None:
        """Prompt for a new admin's Telegram id (owner only)."""
        if not await self._is_owner(event.sender_id):
            await event.answer("⛔ Owner only.", alert=True)
            return
        await self._central.upsert_bot_state(event.sender_id, flow=_FLOW_ADMIN, phase="admin_add")
        await event.edit("Send the new admin's **Telegram user id**.",
                         buttons=[[Button.inline("⬅ Cancel", b"adm:admins")]])

    async def _admin_remove(self, event, tg_id: int) -> None:
        """Remove an admin (owner only; owner cannot be removed)."""
        if not await self._is_owner(event.sender_id):
            await event.answer("⛔ Owner only.", alert=True)
            return
        if tg_id == self._owner_id:
            await event.answer("Cannot remove the owner.", alert=True)
            return
        await self._central.remove_admin(tg_id)
        await self._audit(event.sender_id, "admin.remove", target_user=tg_id)
        await event.answer("Removed")
        await self._admin_admins(event)

    async def _admin_set_role(self, event, tg_id: int, role: str) -> None:
        """Assign a role to an admin (owner only)."""
        if not await self._is_owner(event.sender_id):
            await event.answer("⛔ Owner only.", alert=True)
            return
        await self._central.set_admin_role(tg_id, role)
        await self._audit(event.sender_id, "admin.set_role", target_user=tg_id, details=role)
        await event.answer(f"Role → {role}")
        await self._admin_admins(event)

    # ---- Statistics + Broadcast -------------------------------------- #
    async def _admin_stats(self, event) -> None:
        """Show platform statistics."""
        users = await self._central.list_users()
        instances = await self._central.list_instances()
        running = sum(1 for i in instances if i.status == "running")
        plans = await self._central.list_plans()
        pending = await self._central.count_pending_requests()
        lines = [
            "📊 **Platform Statistics**", "",
            f"Users: {len(users)}",
            f"Instances: {len(instances)} ({running} running)",
            f"Plans: {len(plans)}",
            f"Features: {len(await self._central.list_features())}",
            f"Pending requests: {pending}",
        ]
        await event.edit("\n".join(lines), buttons=[[Button.inline("⬅ Back", b"adm:home")]])

    async def _admin_broadcast_prompt(self, event) -> None:
        """Prompt for a broadcast message."""
        await self._central.upsert_bot_state(event.sender_id, flow=_FLOW_ADMIN, phase="broadcast")
        await event.edit("📢 Send the **message** to broadcast to all users.",
                         buttons=[[Button.inline("⬅ Cancel", b"adm:home")]])

    # ---- Admin free-text input consumer ------------------------------ #
    async def _consume_admin_input(self, event, state) -> None:
        """Handle a free-text reply for an admin flow (phase drives the action)."""
        phase = state["phase"]
        payload = state["payload"] if "payload" in state.keys() else None
        text = event.raw_text.strip()
        await self._central.delete_bot_state(event.sender_id)

        if phase == "plan_new":
            pid = await self._central.create_plan(text, description="")
            await event.respond(f"✅ Plan **{text}** created.",
                                buttons=[[Button.inline("Edit", f"adm:plan:{pid}".encode())]])
        elif phase == "plan_rename" and payload:
            await self._central.rename_plan(int(payload), text)
            await event.respond("✅ Renamed.", buttons=[[Button.inline("Back", f"adm:plan:{payload}".encode())]])
        elif phase == "plan_maxinst" and payload:
            try:
                await self._central.set_plan_max_instances(int(payload), int(text))
                await event.respond("✅ Updated.", buttons=[[Button.inline("Back", f"adm:plan:{payload}".encode())]])
            except ValueError:
                await event.respond("⚠ Send an integer.")
        elif phase == "req_custom" and payload:
            try:
                days = int(text)
            except ValueError:
                await event.respond("⚠ Send an integer number of days.")
                return
            await self._approve_request(int(payload), days, event.sender_id)
            await event.respond(f"✅ Approved for {days} days.")
        elif phase == "admin_add":
            if not text.lstrip("-").isdigit():
                await event.respond("⚠ Send a numeric Telegram id.")
                return
            await self._central.add_admin(int(text), role="admin", added_by=event.sender_id)
            await self._central.set_admin_role(int(text), "admin")
            await self._audit(event.sender_id, "admin.add", target_user=int(text))
            await event.respond(f"✅ Added admin `{text}`.")
        elif phase == "broadcast":
            sent = await self._broadcast(text)
            await self._audit(event.sender_id, "broadcast", details=f"{sent} recipients")
            await event.respond(f"📢 Broadcast delivered to {sent} user(s).")
        elif phase == "ticket_reply" and payload:
            t = await self._central.get_ticket(int(payload))
            if t is not None:
                await self._central.add_ticket_message(int(payload), event.sender_id, text, is_admin=True)
                await self._audit(event.sender_id, "ticket.reply", target_user=t["user_id"],
                                  details=f"ticket #{payload}")
                owner = await self._central.get_user_by_pk(t["user_id"])
                if owner is not None:
                    try:
                        await self._bot.send_message(
                            owner.telegram_user_id,
                            f"🎫 Support replied to your ticket #{payload}:\n{text}",
                        )
                    except Exception:  # noqa: BLE001
                        pass
                await event.respond("✅ Reply sent.",
                                    buttons=[[Button.inline("View", f"adm:tview:{payload}".encode())]])

    async def _broadcast(self, message: str) -> int:
        """Send ``message`` to every user's control chat; return delivery count."""
        sent = 0
        for u in await self._central.list_users():
            try:
                await self._bot.send_message(u.telegram_user_id, f"📢 {message}")
                sent += 1
            except Exception:  # noqa: BLE001 - user may not have started the bot
                continue
        return sent

    async def _audit(self, admin_id: int, action: str, **kw) -> None:
        """Write an audit entry (thin wrapper; never raises)."""
        await self._central.add_audit_log(admin_id, action, **kw)

    # ================================================================== #
    # PHASE 1 — Platform Overview dashboard
    # ================================================================== #
    async def _admin_dashboard(self, event) -> None:
        """📊 Platform Overview — all metrics live in one inline panel."""
        ov = await gather_overview(self._central)
        mem = f"{ov.avg_worker_memory_mb:.1f} MB" if ov.avg_worker_memory_mb is not None else "—"
        up = _fmt_duration(ov.avg_worker_uptime_seconds) if ov.avg_worker_uptime_seconds else "—"
        lines = [
            "📊 **Platform Overview**", "",
            f"Users: {ov.total_users}",
            f"Subscriptions: {ov.active_subscriptions} active / {ov.expired_subscriptions} expired",
            f"Workers: {ov.active_workers} active / {ov.stopped_workers} stopped",
            f"Instances: {ov.running_instances} running / {ov.total_instances} total",
            f"Gemini keys: {ov.enabled_gemini_keys} enabled / {ov.total_gemini_keys} total",
            f"Databases: {ov.total_databases}",
            f"Storage: {fmt_bytes(ov.total_storage_bytes)}",
            f"Avg worker memory: {mem}",
            f"Avg worker uptime: {up}",
        ]
        await event.edit("\n".join(lines), buttons=[
            [Button.inline("🔃 Refresh", b"adm:dash")],
            [Button.inline("⬅ Back", b"adm:home")],
        ])

    # ================================================================== #
    # PHASE 2/3 — Live instance monitor + remote control
    # ================================================================== #
    async def _admin_instance_monitor(self, event, instance_id: int) -> None:
        """Live per-instance metrics + remote-control buttons."""
        inst = await self._central.get_instance(instance_id)
        if inst is None:
            await event.answer("No such instance.", alert=True)
            return
        owner = await self._central.get_user_by_pk(inst.user_id)
        plan = await self._central.plan_for_user_pk(inst.user_id) if owner else None
        m = instance_metrics(inst)
        lines = [
            f"🔍 **Instance #{inst.id}**",
            f"Account: {m.account_id or '—'}  @{owner.username if owner else '-'}",
            f"Owner: `{owner.telegram_user_id if owner else '?'}`  Plan: {plan.name if plan else '—'}",
            f"Status: {m.status}",
            f"Uptime: {_fmt_duration(m.uptime_seconds) if m.uptime_seconds else '—'}",
            f"Memory: {f'{m.memory_mb:.1f} MB' if m.memory_mb is not None else '—'}",
            f"CPU: {f'{m.cpu_percent:.0f}%' if m.cpu_percent is not None else '—'}",
            f"Last heartbeat: {_fmt_ago(m.last_heartbeat)}",
            f"DB size: {fmt_bytes(m.db_size)}  Session: {fmt_bytes(m.session_size)}",
            f"Banners: {m.active_banners}  Archive: {m.archive_targets}  "
            f"Ghost: {m.ghost_targets}  Hunter: {m.hunter_targets}",
        ]
        buttons = [
            [Button.inline("▶ Start", f"adm:istart:{inst.id}".encode()),
             Button.inline("⏹ Stop", f"adm:istop:{inst.id}".encode()),
             Button.inline("🔄 Restart", f"adm:irestart:{inst.id}".encode())],
            [Button.inline("📦 Backup", f"adm:ibackup:{inst.id}".encode()),
             Button.inline("📤 Export DB", f"adm:iexport:{inst.id}".encode()),
             Button.inline("📋 Logs", f"adm:ilogs:{inst.id}".encode())],
            [Button.inline("🔃 Refresh", f"adm:imon:{inst.id}".encode()),
             Button.inline("⬅ Back", f"adm:user:{owner.telegram_user_id if owner else 0}".encode())],
        ]
        await event.edit("\n".join(lines), buttons=buttons)

    async def _admin_instance_control(self, event, instance_id: int, action: str) -> None:
        """Start/Stop/Restart any instance (supervisor reconciles)."""
        inst = await self._central.get_instance(instance_id)
        if inst is None:
            await event.answer("No such instance.", alert=True)
            return
        if action == "start":
            await self._central.set_instance_enabled(instance_id, True)
        elif action == "stop":
            await self._central.set_instance_enabled(instance_id, False)
        elif action == "restart":
            await self._central.set_instance_enabled(instance_id, True)
            await self._central.request_restart(instance_id)
        await self._audit(event.sender_id, f"instance.{action}",
                          target_user=inst.user_id, target_instance=instance_id)
        await event.answer(f"{action.title()} requested")
        await self._admin_instance_monitor(event, instance_id)

    async def _admin_instance_backup(self, event, instance_id: int) -> None:
        """Zip the instance (db + session) via the backup module → send to owner."""
        inst = await self._central.get_instance(instance_id)
        if inst is None or not inst.db_path:
            await event.answer("No such instance.", alert=True)
            return
        await event.answer("Backing up…")
        mgr = None
        try:
            from backup.manager import BackupConfig, BackupManager

            inst_dir = os.path.dirname(inst.db_path)
            session_name = (
                inst.session_path[: -len(".session")]
                if inst.session_path.endswith(".session")
                else inst.session_path
            )
            cfg = BackupConfig(
                backup_path=os.path.join(inst_dir, "backups"),
                db_path=inst.db_path,
                session_name=session_name,
                extra_paths=[],
            )
            # BackupManager needs a store (for its counter) + an opened history
            # connection. Reuse the module as-is with a no-op counter shim.
            mgr = BackupManager(_NullStore(), cfg)
            await mgr.start()
            record = await mgr.create()
            path = getattr(record, "path", None)
            if path and os.path.exists(path):
                await self._bot.send_file(
                    event.sender_id, path, caption=f"📦 Backup — instance #{instance_id}"
                )
                await self._audit(event.sender_id, "instance.backup",
                                  target_user=inst.user_id, target_instance=instance_id)
            else:
                await self._bot.send_message(event.sender_id, "⚠ Backup produced no file.")
                await self._alert_backup_failure(instance_id, "no file")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Instance backup failed")
            await self._bot.send_message(event.sender_id, f"⚠ Backup failed: {type(exc).__name__}")
            await self._alert_backup_failure(instance_id, str(exc))
        finally:
            if mgr is not None:
                try:
                    await mgr.close()
                except Exception:  # noqa: BLE001
                    pass

    async def _alert_backup_failure(self, instance_id: int, detail: str) -> None:
        """Emit a backup_failure alert if an AlertManager is wired."""
        if self._alerts is not None:
            try:
                await self._alerts.notify("backup_failure", f"Instance {instance_id} backup failed: {detail}")
            except Exception:  # noqa: BLE001
                pass

    async def _admin_instance_export(self, event, instance_id: int) -> None:
        """Send the instance's ``sentinel.db`` file to the requesting admin."""
        inst = await self._central.get_instance(instance_id)
        if inst is None or not inst.db_path or not os.path.exists(inst.db_path):
            await event.answer("Database not found.", alert=True)
            return
        await event.answer("Exporting…")
        try:
            await self._bot.send_file(event.sender_id, inst.db_path,
                                      caption=f"📤 sentinel.db — instance #{instance_id} "
                                              f"({fmt_bytes(file_size(inst.db_path))})")
            await self._audit(event.sender_id, "instance.export_db",
                              target_user=inst.user_id, target_instance=instance_id)
        except Exception:  # noqa: BLE001
            await self._bot.send_message(event.sender_id, "⚠ Export failed.")

    async def _admin_instance_logs(self, event, instance_id: int) -> None:
        """Send the tail of the instance's worker.log (or a status summary)."""
        inst = await self._central.get_instance(instance_id)
        if inst is None:
            await event.answer("No such instance.", alert=True)
            return
        await event.answer("Fetching logs…")
        log_path = os.path.join(os.path.dirname(inst.db_path), "worker.log") if inst.db_path else ""
        tail = ""
        if log_path and os.path.exists(log_path):
            try:
                with open(log_path, "rb") as fh:
                    fh.seek(0, os.SEEK_END)
                    size = fh.tell()
                    fh.seek(max(0, size - 4000))
                    tail = fh.read().decode("utf-8", "replace")
            except OSError:
                tail = ""
        summary = (
            f"📋 **Logs — instance #{instance_id}**\n"
            f"Status: {inst.status} | restarts: {inst.restart_count}\n"
            f"Last error: {inst.last_error or '—'}\n\n"
        )
        body = summary + (f"```\n{tail[-3500:]}\n```" if tail else "_No worker.log yet._")
        await event.edit(body, buttons=[[Button.inline("⬅ Back", f"adm:imon:{instance_id}".encode())]])
        await self._audit(event.sender_id, "instance.view_logs",
                          target_user=inst.user_id, target_instance=instance_id)

    # ================================================================== #
    # PHASE 4 — Support (user side)
    # ================================================================== #
    async def _user_tickets(self, event) -> None:
        """🎫 Support — user's tickets + create."""
        user = await self._central.get_user(event.sender_id)
        if user is None:
            await event.edit("Send /start first.", buttons=self._back_button())
            return
        tickets = await self._central.list_tickets(user_pk=user.id)
        lines = ["🎫 **Support**", ""]
        buttons: list = [[Button.inline("➕ New Ticket", b"tk:new")]]
        if not tickets:
            lines.append("You have no tickets.")
        for t in tickets[:20]:
            lines.append(f"#{t['id']} [{t['status']}] {t['subject'] or t['category']}")
            buttons.append([Button.inline(f"View #{t['id']}", f"tk:view:{t['id']}".encode())])
        buttons.append([Button.inline("⬅ Back", b"menu:home")])
        await event.edit("\n".join(lines), buttons=buttons)

    async def _user_ticket_new(self, event) -> None:
        """Prompt the user for a ticket subject + body (single message)."""
        await self._central.upsert_bot_state(event.sender_id, flow=_FLOW_TICKET, phase="create")
        await event.edit(
            "🎫 Describe your issue in one message.\n"
            "_First line = subject, the rest = details._",
            buttons=[[Button.inline("⬅ Cancel", b"menu:tickets")]],
        )

    async def _user_ticket_view(self, event, ticket_id: int) -> None:
        """Show a user's ticket thread with reply/close."""
        t = await self._owned_ticket(event, ticket_id)
        if t is None:
            return
        msgs = await self._central.get_ticket_messages(ticket_id)
        lines = [f"🎫 **Ticket #{ticket_id}** [{t['status']}]",
                 f"Subject: {t['subject'] or '—'}", ""]
        for m in msgs[-10:]:
            who = "🛠 admin" if m["is_admin"] else "you"
            lines.append(f"**{who}:** {m['body']}")
        buttons = []
        if t["status"] == TICKET_OPEN:
            buttons.append([Button.inline("💬 Reply", f"tk:reply:{ticket_id}".encode()),
                            Button.inline("✖ Close", f"tk:close:{ticket_id}".encode())])
        buttons.append([Button.inline("⬅ Back", b"menu:tickets")])
        await event.edit("\n".join(lines), buttons=buttons)

    async def _user_ticket_reply_prompt(self, event, ticket_id: int) -> None:
        """Prompt for a user reply to a ticket."""
        if await self._owned_ticket(event, ticket_id) is None:
            return
        await self._central.upsert_bot_state(
            event.sender_id, flow=_FLOW_TICKET, phase="reply", payload=str(ticket_id)
        )
        await event.edit("💬 Send your reply.",
                         buttons=[[Button.inline("⬅ Cancel", f"tk:view:{ticket_id}".encode())]])

    async def _user_ticket_close(self, event, ticket_id: int) -> None:
        """User closes their own ticket."""
        if await self._owned_ticket(event, ticket_id) is None:
            return
        await self._central.set_ticket_status(ticket_id, TICKET_CLOSED)
        await event.answer("Closed")
        await self._user_ticket_view(event, ticket_id)

    async def _owned_ticket(self, event, ticket_id: int):
        """Return the ticket if it belongs to the caller, else answer + None."""
        user = await self._central.get_user(event.sender_id)
        t = await self._central.get_ticket(ticket_id)
        if user is None or t is None or t["user_id"] != user.id:
            await event.answer("Not your ticket.", alert=True)
            return None
        return t

    async def _consume_ticket_input(self, event, state) -> None:
        """Handle user ticket create/reply free-text."""
        phase = state["phase"]
        payload = state["payload"] if "payload" in state.keys() else None
        text = event.raw_text.strip()
        user = await self._central.get_user(event.sender_id)
        await self._central.delete_bot_state(event.sender_id)
        if user is None or not text:
            return
        if phase == "create":
            subject = text.splitlines()[0][:80]
            tid = await self._central.create_ticket(user.id, subject, text)
            await event.respond(f"✅ Ticket #{tid} created.",
                                buttons=[[Button.inline("View", f"tk:view:{tid}".encode())]])
            await self._notify_admins_ticket(tid, user)
        elif phase == "reply" and payload:
            t = await self._central.get_ticket(int(payload))
            if t and t["user_id"] == user.id:
                await self._central.add_ticket_message(int(payload), user.telegram_user_id, text, is_admin=False)
                await event.respond("✅ Reply sent.",
                                    buttons=[[Button.inline("View", f"tk:view:{payload}".encode())]])
                await self._notify_admins_ticket(int(payload), user, reply=True)

    async def _notify_admins_ticket(self, ticket_id: int, user, *, reply: bool = False) -> None:
        """Best-effort push to admins about a new/updated ticket."""
        verb = "reply on" if reply else "New"
        try:
            for row in await self._central.list_admins():
                try:
                    await self._bot.send_message(
                        int(row["telegram_user_id"]),
                        f"🎫 {verb} ticket #{ticket_id} from @{user.username or user.telegram_user_id}.",
                    )
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            pass

    # ================================================================== #
    # PHASE 4 — Support Center (admin side)
    # ================================================================== #
    async def _admin_tickets(self, event, *, closed: bool = False) -> None:
        """🎫 Support Center — open/closed ticket lists."""
        status = TICKET_CLOSED if closed else TICKET_OPEN
        tickets = await self._central.list_tickets(status=status)
        lines = [f"🎫 **Support Center — {status}**", ""]
        buttons: list = []
        if not tickets:
            lines.append("None.")
        for t in tickets[:20]:
            owner = await self._central.get_user_by_pk(t["user_id"])
            who = owner.telegram_user_id if owner else t["user_id"]
            lines.append(f"#{t['id']} `{who}` [{t['priority']}] {t['subject'] or t['category']}")
            buttons.append([Button.inline(f"Open #{t['id']}", f"adm:tview:{t['id']}".encode())])
        toggle = ("🟢 Open", b"adm:tickets") if closed else ("⚪ Closed", b"adm:tclosed")
        buttons.append([Button.inline(*toggle), Button.inline("⬅ Back", b"adm:home")])
        await event.edit("\n".join(lines), buttons=buttons)

    async def _admin_ticket_view(self, event, ticket_id: int) -> None:
        """Show a ticket thread with reply/close/reopen/assign."""
        t = await self._central.get_ticket(ticket_id)
        if t is None:
            await event.answer("No such ticket.", alert=True)
            return
        owner = await self._central.get_user_by_pk(t["user_id"])
        msgs = await self._central.get_ticket_messages(ticket_id)
        lines = [
            f"🎫 **Ticket #{ticket_id}** [{t['status']}]",
            f"User: `{owner.telegram_user_id if owner else t['user_id']}`  "
            f"Category: {t['category']}  Priority: {t['priority']}",
            f"Assigned: {t['assigned_admin'] or '—'}", "",
        ]
        for m in msgs[-12:]:
            who = "🛠 admin" if m["is_admin"] else "user"
            lines.append(f"**{who}:** {m['body']}")
        row1 = [Button.inline("💬 Reply", f"adm:treply:{ticket_id}".encode()),
                Button.inline("🙋 Assign me", f"adm:tassign:{ticket_id}".encode())]
        if t["status"] == TICKET_OPEN:
            row1.append(Button.inline("✖ Close", f"adm:tclose:{ticket_id}".encode()))
        else:
            row1.append(Button.inline("↻ Reopen", f"adm:treopen:{ticket_id}".encode()))
        await event.edit("\n".join(lines), buttons=[row1, [Button.inline("⬅ Back", b"adm:tickets")]])

    async def _admin_ticket_reply_prompt(self, event, ticket_id: int) -> None:
        """Prompt an admin reply (admin_input flow)."""
        await self._central.upsert_bot_state(
            event.sender_id, flow=_FLOW_ADMIN, phase="ticket_reply", payload=str(ticket_id)
        )
        await event.edit("💬 Send your reply to the user.",
                         buttons=[[Button.inline("⬅ Cancel", f"adm:tview:{ticket_id}".encode())]])

    async def _admin_ticket_status(self, event, ticket_id: int, status: str) -> None:
        """Close/reopen a ticket (admin)."""
        await self._central.set_ticket_status(ticket_id, status)
        t = await self._central.get_ticket(ticket_id)
        await self._audit(event.sender_id, f"ticket.{'close' if status == TICKET_CLOSED else 'reopen'}",
                          target_user=t["user_id"] if t else None, details=f"ticket #{ticket_id}")
        await event.answer("Updated")
        await self._admin_ticket_view(event, ticket_id)

    async def _admin_ticket_assign(self, event, ticket_id: int) -> None:
        """Assign the ticket to the acting admin."""
        await self._central.assign_ticket(ticket_id, event.sender_id)
        await self._audit(event.sender_id, "ticket.assign", details=f"ticket #{ticket_id}")
        await event.answer("Assigned to you")
        await self._admin_ticket_view(event, ticket_id)

    # ================================================================== #
    # PHASE 6 — Audit logs
    # ================================================================== #
    async def _admin_audit(self, event) -> None:
        """📜 Audit Logs — recent entries + filter hints."""
        rows = await self._central.list_audit_logs(limit=25)
        lines = ["📜 **Audit Logs** (recent)", ""]
        if not rows:
            lines.append("No entries yet.")
        for r in rows:
            when = _fmt_datetime(r["created_at"])
            tgt = ""
            if r["target_user"]:
                tgt += f" user={r['target_user']}"
            if r["target_instance"]:
                tgt += f" inst={r['target_instance']}"
            det = f" — {r['details']}" if r["details"] else ""
            lines.append(f"`{when}` admin={r['admin_id']} **{r['action']}**{tgt}{det}")
        await event.edit("\n".join(lines)[:4000], buttons=[
            [Button.inline("🔃 Refresh", b"adm:audit")],
            [Button.inline("⬅ Back", b"adm:home")],
        ])


class _NullStore:
    """Minimal ``SharedStore``-compatible no-op used for one-off admin backups.

    The backup module increments a statistics counter via ``store.incr``; when an
    admin triggers a one-off backup of another user's instance there is no live
    per-instance store, so this shim satisfies the interface without persisting.
    """

    async def incr(self, *_a, **_k) -> None:  # noqa: D401 - interface stub
        """No-op counter increment."""
        return None

    async def get_counter(self, *_a, **_k) -> int:
        """Return 0 (no counters tracked)."""
        return 0


def _fmt_ago(ts) -> str:
    """Format a timestamp as a relative 'x ago' string."""
    if not ts:
        return "—"
    import time as _t
    delta = int(_t.time() - float(ts))
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _fmt_datetime(ts: float) -> str:
    """Format a POSIX timestamp as a compact UTC datetime."""
    from datetime import datetime, timezone

    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return "—"


def _fmt_date(ts: float) -> str:
    """Format a POSIX timestamp as a UTC date (YYYY-MM-DD)."""
    from datetime import datetime, timezone

    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return "—"


def _fmt_duration(seconds: float) -> str:
    """Format an uptime duration compactly (e.g. ``3h 12m``)."""
    seconds = int(max(0, seconds))
    d, rem = divmod(seconds, 86_400)
    h, rem = divmod(rem, 3_600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m"
    return f"{seconds}s"


def _process_memory_mb(pid: int) -> Optional[float]:
    """Return a process's RSS in MB via psutil, or ``None`` if unavailable."""
    try:
        import psutil  # optional dependency

        return psutil.Process(pid).memory_info().rss / (1024 * 1024)
    except Exception:  # noqa: BLE001 - psutil missing / process gone
        return None
