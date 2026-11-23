"""Central control-plane database for the multi-user, multi-instance platform.

Owns the cross-tenant tables in ``data/central.db`` (separate from each
instance's per-tenant ``sentinel.db``):

    * ``plans``       -- named limit bundles (JSON limits, incl. ``max_instances``).
    * ``users``       -- one row per *panel user* (the person at the control bot);
      ``status`` is the desired account state; ``prefix``/``tos_accepted_at`` are
      per-user settings.
    * ``instances``   -- one row per *selfbot instance* a user owns (many-per-user).
      ``enabled`` is the desired run-state, ``status`` the actual one the
      supervisor reconciles; runtime columns (pid, heartbeat, active_tasks,
      active_banners, restart_requested) drive the worker-status panel.
    * ``gemini_keys`` -- per-user Gemini keys with an ``enabled`` flag (never
      auto-deleted).
    * ``bot_state``   -- persisted control-bot wizard/panel-input state so
      onboarding survives a restart.
    * ``admins``      -- platform admins (seeded from env) for the admin panel.

Depends only on ``aiosqlite`` + ``core.plan``; never imports a feature module.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

import aiosqlite

from .plan import DEFAULT_PLANS, Plan

logger = logging.getLogger("sentinel.central")

# Desired user (account) states.
USER_PENDING = "pending"
USER_ACTIVE = "active"
USER_SUSPENDED = "suspended"

# Instance auth states (Telethon session lifecycle).
AUTH_PENDING = "pending"
AUTH_CODE_SENT = "code_sent"
AUTH_2FA = "awaiting_2fa"
AUTH_ACTIVE = "active"
AUTH_REVOKED = "revoked"

# Instance actual (runtime) states.
INST_STARTING = "starting"
INST_RUNNING = "running"
INST_STOPPED = "stopped"
INST_CRASHED = "crashed"

# Bot-state flows (persisted wizard / panel-input kinds).
FLOW_ONBOARDING = "onboarding"
FLOW_AWAITING_KEY = "awaiting_key"
FLOW_AWAITING_PREFIX = "awaiting_prefix"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    limits_json TEXT NOT NULL DEFAULT '{}',
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id INTEGER NOT NULL UNIQUE,
    username         TEXT,
    first_name       TEXT,
    status           TEXT NOT NULL DEFAULT 'active',
    plan_id          INTEGER REFERENCES plans(id),
    prefix           TEXT NOT NULL DEFAULT '.',
    tos_accepted_at  REAL,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS instances (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL REFERENCES users(id),
    account_id        INTEGER,
    name              TEXT NOT NULL,
    session_path      TEXT NOT NULL,
    db_path           TEXT NOT NULL,
    auth_state        TEXT NOT NULL DEFAULT 'pending',
    enabled           INTEGER NOT NULL DEFAULT 1,
    status            TEXT NOT NULL DEFAULT 'stopped',
    pid               INTEGER,
    started_at        REAL,
    last_heartbeat    REAL,
    active_tasks      INTEGER NOT NULL DEFAULT 0,
    active_banners    INTEGER NOT NULL DEFAULT 0,
    restart_count     INTEGER NOT NULL DEFAULT 0,
    restart_requested REAL,
    last_error        TEXT,
    created_at        REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS gemini_keys (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    api_key       TEXT NOT NULL,
    label         TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    failure_count INTEGER NOT NULL DEFAULT 0,
    last_used     REAL,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_state (
    chat_id            INTEGER PRIMARY KEY,
    flow               TEXT NOT NULL,
    phase              TEXT,
    phone              TEXT,
    phone_code_hash    TEXT,
    session_path       TEXT,
    target_instance_id INTEGER,
    tos_accepted       INTEGER NOT NULL DEFAULT 0,
    updated_at         REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS admins (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id INTEGER NOT NULL UNIQUE,
    role             TEXT NOT NULL DEFAULT 'admin',
    role_id          INTEGER,
    added_by         INTEGER,
    created_at       REAL NOT NULL
);

-- Dynamic feature registry: every capability is a row (auto-created at startup).
CREATE TABLE IF NOT EXISTS features (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT NOT NULL UNIQUE,
    title       TEXT,
    description TEXT,
    created_at  REAL NOT NULL
);

-- Many-to-many: which features a plan grants.
CREATE TABLE IF NOT EXISTS plan_features (
    plan_id    INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    feature_id INTEGER NOT NULL REFERENCES features(id) ON DELETE CASCADE,
    PRIMARY KEY (plan_id, feature_id)
);

-- A user's subscription to a plan (time-boxed, admin-approved).
CREATE TABLE IF NOT EXISTS subscriptions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    plan_id     INTEGER NOT NULL REFERENCES plans(id),
    starts_at   REAL NOT NULL,
    expires_at  REAL,
    status      TEXT NOT NULL DEFAULT 'active',
    approved_by INTEGER,
    approved_at REAL,
    created_at  REAL NOT NULL
);

-- A user's request for a plan, awaiting admin approval.
CREATE TABLE IF NOT EXISTS subscription_requests (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users(id),
    requested_plan_id INTEGER NOT NULL REFERENCES plans(id),
    duration_days    INTEGER,
    status           TEXT NOT NULL DEFAULT 'pending',
    created_at       REAL NOT NULL,
    processed_at     REAL,
    processed_by     INTEGER
);

-- Admin RBAC.
CREATE TABLE IF NOT EXISTS admin_roles (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS admin_permissions (
    role_id        INTEGER NOT NULL REFERENCES admin_roles(id) ON DELETE CASCADE,
    permission_key TEXT NOT NULL,
    PRIMARY KEY (role_id, permission_key)
);

-- Support tickets + their message thread (survive restart; DB-backed).
CREATE TABLE IF NOT EXISTS tickets (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(id),
    subject        TEXT,
    category       TEXT NOT NULL DEFAULT 'general',
    priority       TEXT NOT NULL DEFAULT 'normal',
    status         TEXT NOT NULL DEFAULT 'open',
    assigned_admin INTEGER,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS ticket_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id  INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    sender_id  INTEGER NOT NULL,
    is_admin   INTEGER NOT NULL DEFAULT 0,
    body       TEXT NOT NULL,
    created_at REAL NOT NULL
);

-- Immutable audit trail of privileged actions.
CREATE TABLE IF NOT EXISTS audit_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id        INTEGER,
    action          TEXT NOT NULL,
    target_user     INTEGER,
    target_instance INTEGER,
    details         TEXT,
    created_at      REAL NOT NULL
);

-- Per-alert-kind rate-limit state (survives restart).
CREATE TABLE IF NOT EXISTS alert_state (
    kind      TEXT PRIMARY KEY,
    last_sent REAL NOT NULL,
    count     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_users_status ON users (status);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets (status);
CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets (user_id);
CREATE INDEX IF NOT EXISTS idx_ticketmsg_ticket ON ticket_messages (ticket_id);
CREATE INDEX IF NOT EXISTS idx_audit_admin ON audit_logs (admin_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs (action);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs (created_at);
CREATE INDEX IF NOT EXISTS idx_instances_user ON instances (user_id);
CREATE INDEX IF NOT EXISTS idx_instances_enabled ON instances (enabled);
CREATE INDEX IF NOT EXISTS idx_gemini_user ON gemini_keys (user_id);
CREATE INDEX IF NOT EXISTS idx_plan_features_plan ON plan_features (plan_id);
CREATE INDEX IF NOT EXISTS idx_subs_user ON subscriptions (user_id);
CREATE INDEX IF NOT EXISTS idx_subs_status ON subscriptions (status);
CREATE INDEX IF NOT EXISTS idx_subreq_status ON subscription_requests (status);
"""

#: Canonical admin permission keys (used by the admin panel + RBAC seeding).
PERM_USERS = "users.manage"
PERM_PLANS = "plans.manage"
PERM_FEATURES = "features.manage"
PERM_REQUESTS = "requests.manage"
PERM_ADMINS = "admins.manage"
PERM_BROADCAST = "broadcast"
PERM_STATS = "stats.view"
# Admin Operations & Monitoring permissions (this phase).
PERM_MONITORING = "monitoring"
PERM_INSTANCE_CONTROL = "instance_control"
PERM_TICKETS = "ticket_management"
PERM_AUDIT = "audit_view"
ALL_PERMISSIONS: tuple[str, ...] = (
    PERM_USERS, PERM_PLANS, PERM_FEATURES, PERM_REQUESTS,
    PERM_ADMINS, PERM_BROADCAST, PERM_STATS,
    PERM_MONITORING, PERM_INSTANCE_CONTROL, PERM_TICKETS, PERM_AUDIT,
)

#: Default role → permissions seeding. ``seed_roles`` inserts missing rows
#: idempotently, so adding a permission here grants it to existing roles on the
#: next startup (additive; never gates code paths — the DB does).
DEFAULT_ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "owner": ALL_PERMISSIONS,
    "admin": (
        PERM_USERS, PERM_PLANS, PERM_FEATURES, PERM_REQUESTS, PERM_BROADCAST, PERM_STATS,
        PERM_MONITORING, PERM_INSTANCE_CONTROL, PERM_TICKETS, PERM_AUDIT,
    ),
    "support": (PERM_USERS, PERM_REQUESTS, PERM_STATS, PERM_TICKETS, PERM_MONITORING),
}

# Ticket statuses.
TICKET_OPEN = "open"
TICKET_CLOSED = "closed"

# Subscription statuses.
SUB_ACTIVE = "active"
SUB_EXPIRED = "expired"
SUB_SUSPENDED = "suspended"
SUB_CANCELLED = "cancelled"

# Subscription-request statuses.
REQ_PENDING = "pending"
REQ_APPROVED = "approved"
REQ_REJECTED = "rejected"


@dataclass(slots=True)
class UserRecord:
    """A panel-user row joined with its plan name."""

    id: int
    telegram_user_id: int
    username: Optional[str]
    first_name: Optional[str]
    status: str
    plan_id: Optional[int]
    plan_name: Optional[str]
    prefix: str
    tos_accepted_at: Optional[float]
    created_at: float
    updated_at: float


@dataclass(slots=True)
class InstanceRecord:
    """A single selfbot instance owned by a user."""

    id: int
    user_id: int
    account_id: Optional[int]
    name: str
    session_path: str
    db_path: str
    auth_state: str
    enabled: bool
    status: str
    pid: Optional[int]
    started_at: Optional[float]
    last_heartbeat: Optional[float]
    active_tasks: int
    active_banners: int
    restart_count: int
    restart_requested: Optional[float]
    last_error: Optional[str]
    created_at: float


class CentralDatabase:
    """Async persistence for the platform's cross-tenant control plane."""

    def __init__(self, path: str) -> None:
        """Initialise the database.

        Args:
            path: Path to the central SQLite file (``data/central.db``).
        """
        self._path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        """Open the connection, create/upgrade tables and seed default plans."""
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA foreign_keys=ON;")
        await self._conn.executescript(_SCHEMA)
        await self._ensure_columns()
        await self._conn.commit()
        await self._seed_plans()
        logger.info("Central database ready at %s", self._path)

    async def _ensure_columns(self) -> None:
        """Add newer columns to pre-existing tables (idempotent, additive)."""
        wanted = {
            "users": {
                "prefix": "TEXT NOT NULL DEFAULT '.'",
                "tos_accepted_at": "REAL",
            },
            "instances": {
                "active_tasks": "INTEGER NOT NULL DEFAULT 0",
                "active_banners": "INTEGER NOT NULL DEFAULT 0",
                "restart_requested": "REAL",
                "db_path": "TEXT NOT NULL DEFAULT ''",
                "account_id": "INTEGER",
                "name": "TEXT NOT NULL DEFAULT 'Instance'",
                "auth_state": "TEXT NOT NULL DEFAULT 'pending'",
            },
            "plans": {
                "description": "TEXT",
                "max_instances": "INTEGER NOT NULL DEFAULT 1",
                "active": "INTEGER NOT NULL DEFAULT 1",
                "updated_at": "REAL",
            },
            "admins": {
                "role_id": "INTEGER",
            },
            "bot_state": {
                "payload": "TEXT",
            },
        }
        for table, cols in wanted.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
                continue
            async with self._db.execute(f"PRAGMA table_info({table});") as cur:
                existing = {row["name"] for row in await cur.fetchall()}
            for col, ddl in cols.items():
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", col):
                    continue
                if col not in existing:
                    try:
                        await self._db.execute(
                            f"ALTER TABLE {table} ADD COLUMN {col} {ddl};"
                        )
                    except Exception:  # noqa: BLE001 - column may race; ignore
                        logger.debug("Column %s.%s already present", table, col)

    async def close(self) -> None:
        """Close the connection if open."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("CentralDatabase is not connected")
        return self._conn

    async def healthy(self) -> bool:
        """Return ``True`` if the database responds (never raises)."""
        try:
            async with self._db.execute("SELECT 1;") as cur:
                await cur.fetchone()
            return True
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------ #
    # Plans
    # ------------------------------------------------------------------ #
    async def _seed_plans(self) -> None:
        """Insert built-in plans if the table is empty (idempotent)."""
        async with self._db.execute("SELECT COUNT(*) AS n FROM plans;") as cur:
            row = await cur.fetchone()
        if row and int(row["n"]) > 0:
            return
        now = time.time()
        for plan in DEFAULT_PLANS:
            await self._db.execute(
                "INSERT OR IGNORE INTO plans (name, limits_json, description, max_instances, active, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 1, ?, ?);",
                (plan.name, plan.to_json(), f"{plan.name.title()} plan",
                 plan.limit_for("max_instances"), now, now),
            )
        await self._db.commit()
        logger.info("Seeded %d default plans", len(DEFAULT_PLANS))

    @staticmethod
    def _plan_from_row(row: aiosqlite.Row) -> Plan:
        """Build a base :class:`Plan` from a plans row (max_instances from column)."""
        plan = Plan.from_row(row["name"], row["limits_json"], id=row["id"])
        keys = row.keys()
        if "max_instances" in keys and row["max_instances"] is not None:
            plan.limits["max_instances"] = int(row["max_instances"])
        return plan

    async def get_plan(self, name: str) -> Optional[Plan]:
        """Fetch a plan by name (base plan; modules from limits_json)."""
        async with self._db.execute(
            "SELECT * FROM plans WHERE name = ?;", (name,)
        ) as cur:
            row = await cur.fetchone()
        return self._plan_from_row(row) if row else None

    async def get_plan_by_id(self, plan_id: int) -> Optional[Plan]:
        """Fetch a plan by primary key (base plan)."""
        async with self._db.execute(
            "SELECT * FROM plans WHERE id = ?;", (plan_id,)
        ) as cur:
            row = await cur.fetchone()
        return self._plan_from_row(row) if row else None

    async def resolve_plan(self, plan_id: int) -> Optional[Plan]:
        """Resolve a plan with **DB-driven** modules from ``plan_features``.

        Modules come from the many-to-many ``plan_features`` mapping (the dynamic
        feature registry). If a plan has no feature rows yet (fresh install
        before seeding), the JSON ``modules`` are used as a backward-compatible
        fallback so existing behavior is preserved.
        """
        plan = await self.get_plan_by_id(plan_id)
        if plan is None:
            return None
        feature_keys = await self.get_plan_feature_keys(plan_id)
        if feature_keys:
            plan.modules = set(feature_keys)
        return plan

    async def upsert_plan(self, plan: Plan) -> None:
        """Create or update a plan by name (limits + max_instances column)."""
        now = time.time()
        await self._db.execute(
            "INSERT INTO plans (name, limits_json, max_instances, active, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET limits_json = excluded.limits_json, "
            "max_instances = excluded.max_instances, updated_at = excluded.updated_at;",
            (plan.name, plan.to_json(), plan.limit_for("max_instances"), now, now),
        )
        await self._db.commit()

    async def list_plans(self, *, active_only: bool = False) -> list[Plan]:
        """Return all plans (optionally only active ones)."""
        query = "SELECT * FROM plans"
        if active_only:
            query += " WHERE active = 1"
        query += " ORDER BY id;"
        async with self._db.execute(query) as cur:
            rows = await cur.fetchall()
        return [self._plan_from_row(r) for r in rows]

    async def get_plan_row(self, plan_id: int) -> Optional[aiosqlite.Row]:
        """Return the raw plans row (for admin display: description/active/…)."""
        async with self._db.execute(
            "SELECT * FROM plans WHERE id = ?;", (plan_id,)
        ) as cur:
            return await cur.fetchone()

    # ---- Plan editor (admin, no manual SQL) -------------------------- #
    async def create_plan(
        self, name: str, *, description: str = "", max_instances: int = 1
    ) -> int:
        """Create a new (empty-feature) plan; returns its id."""
        now = time.time()
        import json as _json

        limits = _json.dumps({"modules": [], "limits": {"max_instances": max_instances}})
        cur = await self._db.execute(
            "INSERT INTO plans (name, limits_json, description, max_instances, active, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 1, ?, ?);",
            (name, limits, description, max_instances, now, now),
        )
        await self._db.commit()
        return int(cur.lastrowid)

    async def rename_plan(self, plan_id: int, name: str) -> None:
        """Rename a plan."""
        await self._db.execute(
            "UPDATE plans SET name = ?, updated_at = ? WHERE id = ?;",
            (name, time.time(), plan_id),
        )
        await self._db.commit()

    async def set_plan_description(self, plan_id: int, description: str) -> None:
        """Set a plan's description."""
        await self._db.execute(
            "UPDATE plans SET description = ?, updated_at = ? WHERE id = ?;",
            (description, time.time(), plan_id),
        )
        await self._db.commit()

    async def set_plan_max_instances(self, plan_id: int, max_instances: int) -> None:
        """Set a plan's ``max_instances`` (column + mirrored into limits_json)."""
        import json as _json

        row = await self.get_plan_row(plan_id)
        if row is None:
            return
        try:
            data = _json.loads(row["limits_json"]) if row["limits_json"] else {}
        except (TypeError, ValueError):
            data = {}
        data.setdefault("limits", {})["max_instances"] = int(max_instances)
        await self._db.execute(
            "UPDATE plans SET max_instances = ?, limits_json = ?, updated_at = ? WHERE id = ?;",
            (int(max_instances), _json.dumps(data), time.time(), plan_id),
        )
        await self._db.commit()

    async def set_plan_limit(self, plan_id: int, key: str, value: int) -> None:
        """Set an arbitrary numeric limit (e.g. ``gemini_keys_max``) in limits_json."""
        import json as _json

        row = await self.get_plan_row(plan_id)
        if row is None:
            return
        try:
            data = _json.loads(row["limits_json"]) if row["limits_json"] else {}
        except (TypeError, ValueError):
            data = {}
        data.setdefault("limits", {})[key] = int(value)
        await self._db.execute(
            "UPDATE plans SET limits_json = ?, updated_at = ? WHERE id = ?;",
            (_json.dumps(data), time.time(), plan_id),
        )
        await self._db.commit()

    async def set_plan_active(self, plan_id: int, active: bool) -> None:
        """Activate/deactivate a plan (inactive plans are hidden from users)."""
        await self._db.execute(
            "UPDATE plans SET active = ?, updated_at = ? WHERE id = ?;",
            (1 if active else 0, time.time(), plan_id),
        )
        await self._db.commit()

    async def delete_plan(self, plan_id: int) -> bool:
        """Delete a plan and its feature links (subscriptions retain the id)."""
        await self._db.execute("DELETE FROM plan_features WHERE plan_id = ?;", (plan_id,))
        cur = await self._db.execute("DELETE FROM plans WHERE id = ?;", (plan_id,))
        await self._db.commit()
        return cur.rowcount > 0

    async def plan_in_use(self, plan_id: int) -> dict[str, int]:
        """Count rows referencing a plan (users, subscriptions, requests).

        Used to refuse deleting a plan that is still assigned — with
        ``PRAGMA foreign_keys=ON`` the bare DELETE would raise IntegrityError.
        """
        counts: dict[str, int] = {}
        for table, col in (
            ("users", "plan_id"),
            ("subscriptions", "plan_id"),
            ("subscription_requests", "requested_plan_id"),
        ):
            async with self._db.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE {col} = ?;", (plan_id,)  # noqa: S608
            ) as cur:
                row = await cur.fetchone()
            counts[table] = int(row["n"]) if row else 0
        return counts

    # ------------------------------------------------------------------ #
    # Features (dynamic registry) + plan_features (M:N)
    # ------------------------------------------------------------------ #
    async def register_feature(
        self, key: str, *, title: str = "", description: str = ""
    ) -> int:
        """Register a feature (auto-create if missing); returns its id.

        Idempotent: an existing feature keeps its id and refreshes title/desc.
        """
        now = time.time()
        await self._db.execute(
            "INSERT INTO features (key, title, description, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET title = excluded.title, description = excluded.description;",
            (key, title or key, description, now),
        )
        await self._db.commit()
        async with self._db.execute("SELECT id FROM features WHERE key = ?;", (key,)) as cur:
            row = await cur.fetchone()
        return int(row["id"])

    async def list_features(self) -> list[aiosqlite.Row]:
        """Return all registered features."""
        async with self._db.execute("SELECT * FROM features ORDER BY key;") as cur:
            return list(await cur.fetchall())

    async def get_feature(self, key: str) -> Optional[aiosqlite.Row]:
        """Return a feature row by key."""
        async with self._db.execute("SELECT * FROM features WHERE key = ?;", (key,)) as cur:
            return await cur.fetchone()

    async def attach_feature(self, plan_id: int, feature_key: str) -> bool:
        """Grant a feature to a plan (by feature key). ``False`` if unknown key."""
        feat = await self.get_feature(feature_key)
        if feat is None:
            return False
        await self._db.execute(
            "INSERT OR IGNORE INTO plan_features (plan_id, feature_id) VALUES (?, ?);",
            (plan_id, feat["id"]),
        )
        await self._db.commit()
        return True

    async def detach_feature(self, plan_id: int, feature_key: str) -> bool:
        """Revoke a feature from a plan (by feature key)."""
        feat = await self.get_feature(feature_key)
        if feat is None:
            return False
        await self._db.execute(
            "DELETE FROM plan_features WHERE plan_id = ? AND feature_id = ?;",
            (plan_id, feat["id"]),
        )
        await self._db.commit()
        return True

    async def get_plan_feature_keys(self, plan_id: int) -> set[str]:
        """Return the set of feature keys granted by a plan."""
        async with self._db.execute(
            "SELECT f.key FROM plan_features pf JOIN features f ON pf.feature_id = f.id "
            "WHERE pf.plan_id = ?;",
            (plan_id,),
        ) as cur:
            rows = await cur.fetchall()
        return {r["key"] for r in rows}

    # ------------------------------------------------------------------ #
    # Users (panel users)
    # ------------------------------------------------------------------ #
    async def create_user(
        self,
        telegram_user_id: int,
        *,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
        plan_name: Optional[str] = None,
        status: str = USER_ACTIVE,
    ) -> UserRecord:
        """Create (or refresh) a panel user, assigning a plan by name."""
        plan_id: Optional[int] = None
        if plan_name:
            plan = await self.get_plan(plan_name)
            plan_id = plan.id if plan else None
        now = time.time()
        await self._db.execute(
            """
            INSERT INTO users (telegram_user_id, username, first_name, status, plan_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                updated_at = excluded.updated_at;
            """,
            (telegram_user_id, username, first_name, status, plan_id, now, now),
        )
        # Only set plan on first creation (don't override an admin-assigned plan).
        if plan_id is not None:
            await self._db.execute(
                "UPDATE users SET plan_id = COALESCE(plan_id, ?) WHERE telegram_user_id = ?;",
                (plan_id, telegram_user_id),
            )
        await self._db.commit()
        record = await self.get_user(telegram_user_id)
        assert record is not None
        return record

    async def get_user(self, telegram_user_id: int) -> Optional[UserRecord]:
        """Fetch a panel user (with plan name) by Telegram id."""
        async with self._db.execute(
            """
            SELECT u.*, p.name AS plan_name
            FROM users u LEFT JOIN plans p ON u.plan_id = p.id
            WHERE u.telegram_user_id = ?;
            """,
            (telegram_user_id,),
        ) as cur:
            row = await cur.fetchone()
        return _user_from_row(row) if row else None

    async def get_user_by_pk(self, user_pk: int) -> Optional[UserRecord]:
        """Fetch a panel user by primary key."""
        async with self._db.execute(
            "SELECT u.*, p.name AS plan_name FROM users u "
            "LEFT JOIN plans p ON u.plan_id = p.id WHERE u.id = ?;",
            (user_pk,),
        ) as cur:
            row = await cur.fetchone()
        return _user_from_row(row) if row else None

    async def list_users(self, *, status: Optional[str] = None) -> list[UserRecord]:
        """Return panel users, optionally filtered by desired status."""
        query = (
            "SELECT u.*, p.name AS plan_name FROM users u "
            "LEFT JOIN plans p ON u.plan_id = p.id"
        )
        params: tuple = ()
        if status is not None:
            query += " WHERE u.status = ?"
            params = (status,)
        query += " ORDER BY u.created_at;"
        async with self._db.execute(query, params) as cur:
            rows = await cur.fetchall()
        return [_user_from_row(r) for r in rows]

    async def set_user_status(self, telegram_user_id: int, status: str) -> None:
        """Set a user's desired account status (active/suspended/pending)."""
        await self._db.execute(
            "UPDATE users SET status = ?, updated_at = ? WHERE telegram_user_id = ?;",
            (status, time.time(), telegram_user_id),
        )
        await self._db.commit()

    async def set_user_plan(self, telegram_user_id: int, plan_name: str) -> bool:
        """Assign a plan (by name) to a user; ``False`` if plan unknown."""
        plan = await self.get_plan(plan_name)
        if plan is None:
            return False
        await self._db.execute(
            "UPDATE users SET plan_id = ?, updated_at = ? WHERE telegram_user_id = ?;",
            (plan.id, time.time(), telegram_user_id),
        )
        await self._db.commit()
        return True

    async def set_user_prefix(self, telegram_user_id: int, prefix: str) -> None:
        """Persist a user's selfbot command prefix."""
        await self._db.execute(
            "UPDATE users SET prefix = ?, updated_at = ? WHERE telegram_user_id = ?;",
            (prefix, time.time(), telegram_user_id),
        )
        await self._db.commit()

    async def set_tos_accepted(self, telegram_user_id: int) -> None:
        """Stamp the user's Terms-of-Service acceptance time."""
        await self._db.execute(
            "UPDATE users SET tos_accepted_at = ?, updated_at = ? WHERE telegram_user_id = ?;",
            (time.time(), time.time(), telegram_user_id),
        )
        await self._db.commit()

    async def plan_for_user(self, telegram_user_id: int) -> Optional[Plan]:
        """Return the effective (DB-resolved) :class:`Plan` for a user."""
        user = await self.get_user(telegram_user_id)
        if user is None:
            return None
        return await self.plan_for_user_pk(user.id)

    async def plan_for_user_pk(self, user_pk: int) -> Optional[Plan]:
        """Return the effective :class:`Plan` for a user by primary key.

        The effective plan is the user's **active subscription** plan when one
        exists, otherwise the user's ``plan_id`` (backward-compatible for users
        created before subscriptions existed). Modules resolve from
        ``plan_features`` via :meth:`resolve_plan`.
        """
        plan_id = await self.effective_plan_id(user_pk)
        if plan_id is None:
            return None
        return await self.resolve_plan(plan_id)

    async def effective_plan_id(self, user_pk: int) -> Optional[int]:
        """Return the plan id governing a user right now (sub → user.plan_id)."""
        sub = await self.get_active_subscription(user_pk)
        if sub is not None:
            return int(sub["plan_id"])
        user = await self.get_user_by_pk(user_pk)
        return user.plan_id if user else None

    # ------------------------------------------------------------------ #
    # Instances (many per user)
    # ------------------------------------------------------------------ #
    async def create_instance(
        self, user_pk: int, name: str, session_path: str, db_path: str
    ) -> InstanceRecord:
        """Create a new (pending) instance owned by ``user_pk``."""
        now = time.time()
        cur = await self._db.execute(
            """
            INSERT INTO instances (user_id, name, session_path, db_path, auth_state, enabled, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', 1, 'stopped', ?);
            """,
            (user_pk, name, session_path, db_path, now),
        )
        await self._db.commit()
        inst = await self.get_instance(int(cur.lastrowid))
        assert inst is not None
        return inst

    async def get_instance(self, instance_id: int) -> Optional[InstanceRecord]:
        """Fetch an instance by id."""
        async with self._db.execute(
            "SELECT * FROM instances WHERE id = ?;", (instance_id,)
        ) as cur:
            row = await cur.fetchone()
        return _instance_from_row(row) if row else None

    async def list_instances(
        self, user_pk: Optional[int] = None, *, enabled_only: bool = False
    ) -> list[InstanceRecord]:
        """Return instances, optionally scoped to a user / enabled-only."""
        clauses, params = [], []
        if user_pk is not None:
            clauses.append("user_id = ?")
            params.append(user_pk)
        if enabled_only:
            clauses.append("enabled = 1")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        async with self._db.execute(
            f"SELECT * FROM instances{where} ORDER BY id;", tuple(params)
        ) as cur:
            rows = await cur.fetchall()
        return [_instance_from_row(r) for r in rows]

    async def list_runnable_instances(self) -> list[InstanceRecord]:
        """Return enabled instances whose owner account is active (supervisor)."""
        async with self._db.execute(
            """
            SELECT i.* FROM instances i JOIN users u ON i.user_id = u.id
            WHERE i.enabled = 1 AND u.status = ? AND i.auth_state = ?
            ORDER BY i.id;
            """,
            (USER_ACTIVE, AUTH_ACTIVE),
        ) as cur:
            rows = await cur.fetchall()
        return [_instance_from_row(r) for r in rows]

    async def count_instances(self, user_pk: int) -> int:
        """Return the number of instances a user owns."""
        async with self._db.execute(
            "SELECT COUNT(*) AS n FROM instances WHERE user_id = ?;", (user_pk,)
        ) as cur:
            row = await cur.fetchone()
        return int(row["n"]) if row else 0

    async def set_instance_paths(
        self, instance_id: int, session_path: str, db_path: str
    ) -> None:
        """Set an instance's computed per-instance storage paths."""
        await self._db.execute(
            "UPDATE instances SET session_path = ?, db_path = ? WHERE id = ?;",
            (session_path, db_path, instance_id),
        )
        await self._db.commit()

    async def set_instance_account(
        self, instance_id: int, account_id: int, *, auth_state: str = AUTH_ACTIVE
    ) -> None:
        """Record the authorized selfbot account id + auth state for an instance."""
        await self._db.execute(
            "UPDATE instances SET account_id = ?, auth_state = ? WHERE id = ?;",
            (account_id, auth_state, instance_id),
        )
        await self._db.commit()

    async def set_auth_state(self, instance_id: int, auth_state: str) -> None:
        """Update an instance's Telethon auth state."""
        await self._db.execute(
            "UPDATE instances SET auth_state = ? WHERE id = ?;", (auth_state, instance_id)
        )
        await self._db.commit()

    async def set_instance_enabled(self, instance_id: int, enabled: bool) -> None:
        """Set an instance's desired run-state (enabled/disabled)."""
        await self._db.execute(
            "UPDATE instances SET enabled = ? WHERE id = ?;",
            (1 if enabled else 0, instance_id),
        )
        await self._db.commit()

    async def set_instance_status(
        self,
        instance_id: int,
        status: str,
        *,
        pid: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None:
        """Update an instance's actual runtime status."""
        started = time.time() if status == INST_STARTING else None
        await self._db.execute(
            """
            UPDATE instances SET
                status = ?,
                pid = COALESCE(?, pid),
                started_at = COALESCE(?, started_at),
                last_error = ?
            WHERE id = ?;
            """,
            (status, pid, started, error, instance_id),
        )
        await self._db.commit()

    async def heartbeat(
        self, instance_id: int, *, active_tasks: int = 0, active_banners: int = 0
    ) -> None:
        """Record a liveness heartbeat + runtime metrics for a running instance."""
        await self._db.execute(
            """
            UPDATE instances SET
                last_heartbeat = ?, status = ?, active_tasks = ?, active_banners = ?
            WHERE id = ?;
            """,
            (time.time(), INST_RUNNING, active_tasks, active_banners, instance_id),
        )
        await self._db.commit()

    async def increment_restart(self, instance_id: int) -> None:
        """Increment an instance's restart counter."""
        await self._db.execute(
            "UPDATE instances SET restart_count = restart_count + 1 WHERE id = ?;",
            (instance_id,),
        )
        await self._db.commit()

    async def request_restart(self, instance_id: int) -> None:
        """Signal the supervisor to restart this instance on its next tick."""
        await self._db.execute(
            "UPDATE instances SET restart_requested = ? WHERE id = ?;",
            (time.time(), instance_id),
        )
        await self._db.commit()

    async def consume_restart(self, instance_id: int) -> bool:
        """Atomically clear + report whether a restart was requested.

        Single statement so a concurrent ``request_restart()`` can never slip
        in between a SELECT and the clearing UPDATE (lost restart).
        """
        cur = await self._db.execute(
            "UPDATE instances SET restart_requested = NULL "
            "WHERE id = ? AND restart_requested IS NOT NULL;",
            (instance_id,),
        )
        await self._db.commit()
        return cur.rowcount > 0

    async def instance_exists(self, instance_id: int) -> bool:
        """Return ``True`` if an instance row with ``instance_id`` exists."""
        async with self._db.execute(
            "SELECT 1 FROM instances WHERE id = ?;", (instance_id,)
        ) as cur:
            return await cur.fetchone() is not None

    async def get_instance_owner(self, instance_id: int) -> Optional[int]:
        """Return the owning user's primary key for ``instance_id`` (or ``None``).

        Ownership is always resolved from the database — callers must never
        trust an owner id supplied by a client/callback payload.
        """
        async with self._db.execute(
            "SELECT user_id FROM instances WHERE id = ?;", (instance_id,)
        ) as cur:
            row = await cur.fetchone()
        return int(row["user_id"]) if row else None

    async def delete_instance(self, instance_id: int) -> bool:
        """Permanently delete an instance and its related state, atomically.

        Removes the ``instances`` row (which carries all runtime metadata — pid,
        heartbeat, restart counters, status, etc.) *and* any ``bot_state`` rows
        that reference this instance (an in-flight onboarding wizard), in a
        single transaction. The two deletes share one implicit transaction under
        SQLite's deferred isolation; on any error the transaction is rolled back
        so a partial delete can never occur. Filesystem cleanup is the caller's
        responsibility (see ``InstancePaths.purge``).

        Args:
            instance_id: The instance to delete.

        Returns:
            ``True`` if an instance row was removed.
        """
        try:
            cur = await self._db.execute(
                "DELETE FROM instances WHERE id = ?;", (instance_id,)
            )
            await self._db.execute(
                "DELETE FROM bot_state WHERE target_instance_id = ?;", (instance_id,)
            )
        except Exception:  # noqa: BLE001 - keep the DB consistent on failure
            await self._db.rollback()
            raise
        await self._db.commit()
        removed = cur.rowcount > 0
        if removed:
            logger.info("Deleted instance %s (row + related bot_state)", instance_id)
        return removed

    # ------------------------------------------------------------------ #
    # Gemini keys (per user; enable/disable, never auto-delete)
    # ------------------------------------------------------------------ #
    async def add_gemini_key(
        self, user_pk: int, api_key: str, *, label: Optional[str] = None
    ) -> None:
        """Add a Gemini API key for a user (enabled by default)."""
        await self._db.execute(
            "INSERT INTO gemini_keys (user_id, api_key, label, enabled, created_at) "
            "VALUES (?, ?, ?, 1, ?);",
            (user_pk, api_key, label, time.time()),
        )
        await self._db.commit()

    async def get_active_gemini_keys(self, user_pk: int) -> list[str]:
        """Return a user's ENABLED keys, least-recently-failed first (worker)."""
        async with self._db.execute(
            "SELECT api_key FROM gemini_keys WHERE user_id = ? AND enabled = 1 "
            "ORDER BY failure_count ASC, COALESCE(last_used, 0) ASC;",
            (user_pk,),
        ) as cur:
            rows = await cur.fetchall()
        return [r["api_key"] for r in rows]

    async def list_gemini_keys(
        self, user_pk: int, *, enabled: Optional[bool] = None
    ) -> list[aiosqlite.Row]:
        """Return a user's key rows (optionally filtered by enabled flag)."""
        query = "SELECT * FROM gemini_keys WHERE user_id = ?"
        params: list = [user_pk]
        if enabled is not None:
            query += " AND enabled = ?"
            params.append(1 if enabled else 0)
        query += " ORDER BY id;"
        async with self._db.execute(query, tuple(params)) as cur:
            return list(await cur.fetchall())

    async def set_gemini_key_enabled(
        self, user_pk: int, key_id: int, enabled: bool
    ) -> bool:
        """Enable or disable one of a user's keys (never deletes)."""
        cur = await self._db.execute(
            "UPDATE gemini_keys SET enabled = ? WHERE id = ? AND user_id = ?;",
            (1 if enabled else 0, key_id, user_pk),
        )
        await self._db.commit()
        return cur.rowcount > 0

    async def delete_gemini_key(self, user_pk: int, key_id: int) -> bool:
        """Manually delete one of a user's keys (explicit user action only)."""
        cur = await self._db.execute(
            "DELETE FROM gemini_keys WHERE id = ? AND user_id = ?;", (key_id, user_pk)
        )
        await self._db.commit()
        return cur.rowcount > 0

    async def mark_gemini_failure(self, user_pk: int, api_key: str) -> None:
        """Increment a key's failure counter (rotation/health; no deletion)."""
        await self._db.execute(
            "UPDATE gemini_keys SET failure_count = failure_count + 1, last_used = ? "
            "WHERE user_id = ? AND api_key = ?;",
            (time.time(), user_pk, api_key),
        )
        await self._db.commit()

    async def count_gemini_keys(
        self, user_pk: int, *, enabled: Optional[bool] = None
    ) -> int:
        """Count a user's keys (optionally by enabled flag)."""
        query = "SELECT COUNT(*) AS n FROM gemini_keys WHERE user_id = ?"
        params: list = [user_pk]
        if enabled is not None:
            query += " AND enabled = ?"
            params.append(1 if enabled else 0)
        async with self._db.execute(query, tuple(params)) as cur:
            row = await cur.fetchone()
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------ #
    # Bot state (persisted wizard / panel-input; NO secrets)
    # ------------------------------------------------------------------ #
    async def upsert_bot_state(
        self,
        chat_id: int,
        *,
        flow: str,
        phase: Optional[str] = None,
        phone: Optional[str] = None,
        phone_code_hash: Optional[str] = None,
        session_path: Optional[str] = None,
        target_instance_id: Optional[int] = None,
        tos_accepted: bool = False,
        payload: Optional[str] = None,
    ) -> None:
        """Persist one control-chat's wizard/panel-input state.

        Never stores the OTP code or 2FA password — only routing metadata.
        ``payload`` carries non-secret admin-flow context (e.g. a target id).
        """
        await self._db.execute(
            """
            INSERT INTO bot_state
                (chat_id, flow, phase, phone, phone_code_hash, session_path, target_instance_id, tos_accepted, payload, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                flow = excluded.flow,
                phase = excluded.phase,
                phone = COALESCE(excluded.phone, bot_state.phone),
                phone_code_hash = COALESCE(excluded.phone_code_hash, bot_state.phone_code_hash),
                session_path = COALESCE(excluded.session_path, bot_state.session_path),
                target_instance_id = COALESCE(excluded.target_instance_id, bot_state.target_instance_id),
                tos_accepted = excluded.tos_accepted,
                payload = excluded.payload,
                updated_at = excluded.updated_at;
            """,
            (
                chat_id, flow, phase, phone, phone_code_hash, session_path,
                target_instance_id, 1 if tos_accepted else 0, payload, time.time(),
            ),
        )
        await self._db.commit()

    async def get_bot_state(self, chat_id: int) -> Optional[aiosqlite.Row]:
        """Return a control-chat's persisted state, or ``None``."""
        async with self._db.execute(
            "SELECT * FROM bot_state WHERE chat_id = ?;", (chat_id,)
        ) as cur:
            return await cur.fetchone()

    async def delete_bot_state(self, chat_id: int) -> None:
        """Clear a control-chat's persisted state."""
        await self._db.execute("DELETE FROM bot_state WHERE chat_id = ?;", (chat_id,))
        await self._db.commit()

    async def list_bot_state(self, *, flow: Optional[str] = None) -> list[aiosqlite.Row]:
        """Return persisted control-chat states (optionally by flow)."""
        if flow is None:
            query, params = "SELECT * FROM bot_state;", ()
        else:
            query, params = "SELECT * FROM bot_state WHERE flow = ?;", (flow,)
        async with self._db.execute(query, params) as cur:
            return list(await cur.fetchall())

    # ------------------------------------------------------------------ #
    # Admins
    # ------------------------------------------------------------------ #
    async def seed_admins(self, telegram_ids: "set[int] | list[int]") -> None:
        """Insert env-provided admin ids if absent (idempotent)."""
        now = time.time()
        for tg in telegram_ids:
            await self._db.execute(
                "INSERT OR IGNORE INTO admins (telegram_user_id, role, created_at) "
                "VALUES (?, 'admin', ?);",
                (int(tg), now),
            )
        await self._db.commit()

    async def add_admin(
        self, telegram_user_id: int, *, role: str = "admin", added_by: Optional[int] = None
    ) -> None:
        """Add or update an admin."""
        await self._db.execute(
            "INSERT INTO admins (telegram_user_id, role, added_by, created_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(telegram_user_id) DO UPDATE SET role = excluded.role;",
            (telegram_user_id, role, added_by, time.time()),
        )
        await self._db.commit()

    async def is_admin(self, telegram_user_id: int) -> bool:
        """Return ``True`` if the given Telegram id is a platform admin."""
        async with self._db.execute(
            "SELECT 1 FROM admins WHERE telegram_user_id = ?;", (telegram_user_id,)
        ) as cur:
            return await cur.fetchone() is not None

    async def list_admins(self) -> list[aiosqlite.Row]:
        """Return all admin rows."""
        async with self._db.execute("SELECT * FROM admins ORDER BY id;") as cur:
            return list(await cur.fetchall())

    async def remove_admin(self, telegram_user_id: int) -> bool:
        """Remove an admin (owner is protected by the caller)."""
        cur = await self._db.execute(
            "DELETE FROM admins WHERE telegram_user_id = ?;", (telegram_user_id,)
        )
        await self._db.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------ #
    # Admin roles & permissions (RBAC)
    # ------------------------------------------------------------------ #
    async def seed_roles(self) -> None:
        """Seed default roles + their permissions if absent (idempotent)."""
        for role_name, perms in DEFAULT_ROLE_PERMISSIONS.items():
            await self._db.execute(
                "INSERT OR IGNORE INTO admin_roles (name) VALUES (?);", (role_name,)
            )
            rid = await self.get_role_id(role_name)
            if rid is None:
                continue
            for perm in perms:
                await self._db.execute(
                    "INSERT OR IGNORE INTO admin_permissions (role_id, permission_key) VALUES (?, ?);",
                    (rid, perm),
                )
        await self._db.commit()

    async def get_role_id(self, name: str) -> Optional[int]:
        """Return a role id by name."""
        async with self._db.execute(
            "SELECT id FROM admin_roles WHERE name = ?;", (name,)
        ) as cur:
            row = await cur.fetchone()
        return int(row["id"]) if row else None

    async def list_roles(self) -> list[aiosqlite.Row]:
        """Return all admin roles."""
        async with self._db.execute("SELECT * FROM admin_roles ORDER BY id;") as cur:
            return list(await cur.fetchall())

    async def set_admin_role(self, telegram_user_id: int, role_name: str) -> bool:
        """Assign a role (by name) to an admin; ``False`` if role unknown."""
        rid = await self.get_role_id(role_name)
        if rid is None:
            return False
        await self._db.execute(
            "UPDATE admins SET role = ?, role_id = ? WHERE telegram_user_id = ?;",
            (role_name, rid, telegram_user_id),
        )
        await self._db.commit()
        return True

    async def role_permissions(self, role_name: str) -> set[str]:
        """Return the permission keys granted to a role."""
        rid = await self.get_role_id(role_name)
        if rid is None:
            return set()
        async with self._db.execute(
            "SELECT permission_key FROM admin_permissions WHERE role_id = ?;", (rid,)
        ) as cur:
            rows = await cur.fetchall()
        return {r["permission_key"] for r in rows}

    async def has_permission(self, telegram_user_id: int, permission_key: str) -> bool:
        """Return ``True`` if an admin's role grants ``permission_key``.

        The owner (``role='owner'``) implicitly has every permission. This is
        the only RBAC check code paths use — roles/permissions live in the DB.
        """
        async with self._db.execute(
            "SELECT role FROM admins WHERE telegram_user_id = ?;", (telegram_user_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return False
        if row["role"] == "owner":
            return True
        return permission_key in await self.role_permissions(row["role"])

    async def ensure_owner(self, telegram_user_id: int) -> None:
        """Ensure a Telegram id is the platform owner (full access)."""
        if telegram_user_id <= 0:
            return
        await self.seed_roles()
        rid = await self.get_role_id("owner")
        await self._db.execute(
            "INSERT INTO admins (telegram_user_id, role, role_id, created_at) "
            "VALUES (?, 'owner', ?, ?) "
            "ON CONFLICT(telegram_user_id) DO UPDATE SET role = 'owner', role_id = excluded.role_id;",
            (telegram_user_id, rid, time.time()),
        )
        await self._db.commit()

    # ------------------------------------------------------------------ #
    # Subscriptions
    # ------------------------------------------------------------------ #
    async def create_subscription(
        self,
        user_pk: int,
        plan_id: int,
        *,
        duration_days: Optional[int],
        approved_by: Optional[int] = None,
    ) -> int:
        """Create an active subscription; supersedes any prior active one.

        ``duration_days=None`` means a perpetual subscription (no expiry).
        Also mirrors the plan onto ``users.plan_id`` for convenience.
        """
        now = time.time()
        expires = (now + duration_days * 86_400) if duration_days else None
        # Cancel any existing active subscription for this user.
        await self._db.execute(
            "UPDATE subscriptions SET status = ? WHERE user_id = ? AND status = ?;",
            (SUB_CANCELLED, user_pk, SUB_ACTIVE),
        )
        cur = await self._db.execute(
            "INSERT INTO subscriptions (user_id, plan_id, starts_at, expires_at, status, approved_by, approved_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?);",
            (user_pk, plan_id, now, expires, SUB_ACTIVE, approved_by, now, now),
        )
        await self._db.execute(
            "UPDATE users SET plan_id = ?, updated_at = ? WHERE id = ?;",
            (plan_id, now, user_pk),
        )
        await self._db.commit()
        return int(cur.lastrowid)

    async def get_active_subscription(self, user_pk: int) -> Optional[aiosqlite.Row]:
        """Return the user's active, non-expired subscription (latest), or None."""
        now = time.time()
        async with self._db.execute(
            "SELECT * FROM subscriptions WHERE user_id = ? AND status = ? "
            "AND (expires_at IS NULL OR expires_at > ?) ORDER BY id DESC LIMIT 1;",
            (user_pk, SUB_ACTIVE, now),
        ) as cur:
            return await cur.fetchone()

    async def get_latest_subscription(self, user_pk: int) -> Optional[aiosqlite.Row]:
        """Return the user's most recent subscription regardless of status."""
        async with self._db.execute(
            "SELECT * FROM subscriptions WHERE user_id = ? ORDER BY id DESC LIMIT 1;",
            (user_pk,),
        ) as cur:
            return await cur.fetchone()

    async def extend_subscription(self, user_pk: int, extra_days: int) -> bool:
        """Extend (or revive) a user's subscription by ``extra_days``."""
        now = time.time()
        sub = await self.get_active_subscription(user_pk) or await self.get_latest_subscription(user_pk)
        if sub is None:
            return False
        if sub["expires_at"] is None:
            # Perpetual subscription: nothing to extend, keep it lifetime.
            return True
        base = max(now, sub["expires_at"])
        new_expiry = base + extra_days * 86_400
        await self._db.execute(
            "UPDATE subscriptions SET expires_at = ?, status = ? WHERE id = ?;",
            (new_expiry, SUB_ACTIVE, sub["id"]),
        )
        await self._db.commit()
        return True

    async def set_subscription_status(self, sub_id: int, status: str) -> None:
        """Set a subscription's status (active/expired/suspended/cancelled)."""
        await self._db.execute(
            "UPDATE subscriptions SET status = ? WHERE id = ?;", (status, sub_id)
        )
        await self._db.commit()

    async def suspend_user_subscription(self, user_pk: int) -> None:
        """Suspend a user's active subscription (premium blocked until resumed)."""
        await self._db.execute(
            "UPDATE subscriptions SET status = ? WHERE user_id = ? AND status = ?;",
            (SUB_SUSPENDED, user_pk, SUB_ACTIVE),
        )
        await self._db.commit()

    async def resume_user_subscription(self, user_pk: int) -> None:
        """Resume a suspended, non-expired subscription."""
        now = time.time()
        await self._db.execute(
            "UPDATE subscriptions SET status = ? WHERE user_id = ? AND status = ? "
            "AND (expires_at IS NULL OR expires_at > ?);",
            (SUB_ACTIVE, user_pk, SUB_SUSPENDED, now),
        )
        await self._db.commit()

    async def expire_due_subscriptions(self) -> list[int]:
        """Mark all past-due active subscriptions expired; return owner user_pks."""
        now = time.time()
        async with self._db.execute(
            "SELECT DISTINCT user_id FROM subscriptions WHERE status = ? "
            "AND expires_at IS NOT NULL AND expires_at <= ?;",
            (SUB_ACTIVE, now),
        ) as cur:
            user_pks = [int(r["user_id"]) for r in await cur.fetchall()]
        if user_pks:
            await self._db.execute(
                "UPDATE subscriptions SET status = ? WHERE status = ? "
                "AND expires_at IS NOT NULL AND expires_at <= ?;",
                (SUB_EXPIRED, SUB_ACTIVE, now),
            )
            await self._db.commit()
        return user_pks

    # ------------------------------------------------------------------ #
    # Subscription requests
    # ------------------------------------------------------------------ #
    async def create_request(
        self, user_pk: int, plan_id: int, *, duration_days: Optional[int] = None
    ) -> int:
        """Create a pending subscription request; returns its id."""
        now = time.time()
        cur = await self._db.execute(
            "INSERT INTO subscription_requests (user_id, requested_plan_id, duration_days, status, created_at) "
            "VALUES (?, ?, ?, ?, ?);",
            (user_pk, plan_id, duration_days, REQ_PENDING, now),
        )
        await self._db.commit()
        return int(cur.lastrowid)

    async def get_request(self, request_id: int) -> Optional[aiosqlite.Row]:
        """Return a request row by id."""
        async with self._db.execute(
            "SELECT * FROM subscription_requests WHERE id = ?;", (request_id,)
        ) as cur:
            return await cur.fetchone()

    async def list_requests(self, *, status: Optional[str] = None) -> list[aiosqlite.Row]:
        """Return requests (optionally filtered by status), newest first."""
        if status is None:
            query, params = "SELECT * FROM subscription_requests ORDER BY id DESC;", ()
        else:
            query = "SELECT * FROM subscription_requests WHERE status = ? ORDER BY id DESC;"
            params = (status,)
        async with self._db.execute(query, params) as cur:
            return list(await cur.fetchall())

    async def set_request_status(
        self, request_id: int, status: str, *, processed_by: Optional[int] = None
    ) -> None:
        """Mark a request approved/rejected."""
        await self._db.execute(
            "UPDATE subscription_requests SET status = ?, processed_at = ?, processed_by = ? WHERE id = ?;",
            (status, time.time(), processed_by, request_id),
        )
        await self._db.commit()

    async def count_pending_requests(self) -> int:
        """Return the number of pending requests."""
        async with self._db.execute(
            "SELECT COUNT(*) AS n FROM subscription_requests WHERE status = ?;", (REQ_PENDING,)
        ) as cur:
            row = await cur.fetchone()
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------ #
    # Monitoring aggregates (Phase 1 dashboard)
    # ------------------------------------------------------------------ #
    async def _scalar(self, query: str, params: tuple = ()) -> int:
        """Run a COUNT/SUM scalar query, returning 0 on empty."""
        async with self._db.execute(query, params) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    async def count_all_users(self) -> int:
        """Total registered panel users."""
        return await self._scalar("SELECT COUNT(*) FROM users;")

    async def count_subscriptions_by_status(self, status: str) -> int:
        """Count subscriptions in a given status."""
        return await self._scalar(
            "SELECT COUNT(*) FROM subscriptions WHERE status = ?;", (status,)
        )

    async def count_all_instances(self) -> int:
        """Total instances across all users."""
        return await self._scalar("SELECT COUNT(*) FROM instances;")

    async def count_instances_by_status(self, status: str) -> int:
        """Count instances whose ACTUAL status matches."""
        return await self._scalar(
            "SELECT COUNT(*) FROM instances WHERE status = ?;", (status,)
        )

    async def count_all_gemini_keys(self, *, enabled: Optional[bool] = None) -> int:
        """Total Gemini keys across all users (optionally enabled-only)."""
        if enabled is None:
            return await self._scalar("SELECT COUNT(*) FROM gemini_keys;")
        return await self._scalar(
            "SELECT COUNT(*) FROM gemini_keys WHERE enabled = ?;", (1 if enabled else 0,)
        )

    async def gemini_failure_totals(self) -> tuple[int, int]:
        """Return ``(total_failures, key_count)`` across all Gemini keys."""
        async with self._db.execute(
            "SELECT COALESCE(SUM(failure_count),0) AS f, COUNT(*) AS n FROM gemini_keys;"
        ) as cur:
            row = await cur.fetchone()
        return (int(row["f"]) if row else 0, int(row["n"]) if row else 0)

    # ------------------------------------------------------------------ #
    # Tickets (Phase 4 support system)
    # ------------------------------------------------------------------ #
    async def create_ticket(
        self, user_pk: int, subject: str, body: str, *,
        category: str = "general", priority: str = "normal",
    ) -> int:
        """Open a ticket + store its first message; returns the ticket id."""
        now = time.time()
        cur = await self._db.execute(
            "INSERT INTO tickets (user_id, subject, category, priority, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'open', ?, ?);",
            (user_pk, subject, category, priority, now, now),
        )
        tid = int(cur.lastrowid)
        await self._db.execute(
            "INSERT INTO ticket_messages (ticket_id, sender_id, is_admin, body, created_at) "
            "VALUES (?, ?, 0, ?, ?);",
            (tid, user_pk, body, now),
        )
        await self._db.commit()
        return tid

    async def get_ticket(self, ticket_id: int) -> Optional[aiosqlite.Row]:
        """Return a ticket row by id."""
        async with self._db.execute(
            "SELECT * FROM tickets WHERE id = ?;", (ticket_id,)
        ) as cur:
            return await cur.fetchone()

    async def list_tickets(
        self, *, status: Optional[str] = None, user_pk: Optional[int] = None
    ) -> list[aiosqlite.Row]:
        """Return tickets filtered by status and/or owner, newest first."""
        clauses, params = [], []
        if status is not None:
            clauses.append("status = ?"); params.append(status)
        if user_pk is not None:
            clauses.append("user_id = ?"); params.append(user_pk)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        async with self._db.execute(
            f"SELECT * FROM tickets{where} ORDER BY updated_at DESC;", tuple(params)
        ) as cur:
            return list(await cur.fetchall())

    async def add_ticket_message(
        self, ticket_id: int, sender_id: int, body: str, *, is_admin: bool = False
    ) -> None:
        """Append a message to a ticket and bump its ``updated_at``."""
        now = time.time()
        await self._db.execute(
            "INSERT INTO ticket_messages (ticket_id, sender_id, is_admin, body, created_at) "
            "VALUES (?, ?, ?, ?, ?);",
            (ticket_id, sender_id, 1 if is_admin else 0, body, now),
        )
        await self._db.execute(
            "UPDATE tickets SET updated_at = ? WHERE id = ?;", (now, ticket_id)
        )
        await self._db.commit()

    async def get_ticket_messages(self, ticket_id: int) -> list[aiosqlite.Row]:
        """Return a ticket's messages oldest-first."""
        async with self._db.execute(
            "SELECT * FROM ticket_messages WHERE ticket_id = ? ORDER BY id;", (ticket_id,)
        ) as cur:
            return list(await cur.fetchall())

    async def set_ticket_status(self, ticket_id: int, status: str) -> None:
        """Close/reopen a ticket."""
        await self._db.execute(
            "UPDATE tickets SET status = ?, updated_at = ? WHERE id = ?;",
            (status, time.time(), ticket_id),
        )
        await self._db.commit()

    async def assign_ticket(self, ticket_id: int, admin_id: int) -> None:
        """Assign a ticket to an admin."""
        await self._db.execute(
            "UPDATE tickets SET assigned_admin = ?, updated_at = ? WHERE id = ?;",
            (admin_id, time.time(), ticket_id),
        )
        await self._db.commit()

    async def count_open_tickets(self) -> int:
        """Number of open tickets."""
        return await self._scalar(
            "SELECT COUNT(*) FROM tickets WHERE status = ?;", (TICKET_OPEN,)
        )

    # ------------------------------------------------------------------ #
    # Audit log (Phase 6)
    # ------------------------------------------------------------------ #
    async def add_audit_log(
        self, admin_id: Optional[int], action: str, *,
        target_user: Optional[int] = None, target_instance: Optional[int] = None,
        details: Optional[str] = None,
    ) -> None:
        """Append an immutable audit entry (best-effort; never raises upward)."""
        try:
            await self._db.execute(
                "INSERT INTO audit_logs (admin_id, action, target_user, target_instance, details, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?);",
                (admin_id, action, target_user, target_instance, details, time.time()),
            )
            await self._db.commit()
        except Exception:  # noqa: BLE001 - auditing must not break an action
            logger.debug("audit write failed for %s", action)

    async def list_audit_logs(
        self, *, admin_id: Optional[int] = None, target_user: Optional[int] = None,
        action: Optional[str] = None, since: Optional[float] = None,
        until: Optional[float] = None, limit: int = 30,
    ) -> list[aiosqlite.Row]:
        """Return audit entries filtered by admin/user/action/date, newest first."""
        clauses, params = [], []
        if admin_id is not None:
            clauses.append("admin_id = ?"); params.append(admin_id)
        if target_user is not None:
            clauses.append("target_user = ?"); params.append(target_user)
        if action is not None:
            clauses.append("action = ?"); params.append(action)
        if since is not None:
            clauses.append("created_at >= ?"); params.append(since)
        if until is not None:
            clauses.append("created_at <= ?"); params.append(until)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(int(limit))
        async with self._db.execute(
            f"SELECT * FROM audit_logs{where} ORDER BY id DESC LIMIT ?;", tuple(params)
        ) as cur:
            return list(await cur.fetchall())

    # ------------------------------------------------------------------ #
    # Alert rate-limit state (Phase 5)
    # ------------------------------------------------------------------ #
    async def alert_should_send(self, kind: str, min_interval: float) -> bool:
        """Return ``True`` if an alert of ``kind`` may fire now (rate limit)."""
        now = time.time()
        async with self._db.execute(
            "SELECT last_sent FROM alert_state WHERE kind = ?;", (kind,)
        ) as cur:
            row = await cur.fetchone()
        if row and (now - float(row["last_sent"])) < min_interval:
            return False
        return True

    async def record_alert_sent(self, kind: str) -> None:
        """Mark an alert kind as just-sent (bumps its counter)."""
        await self._db.execute(
            "INSERT INTO alert_state (kind, last_sent, count) VALUES (?, ?, 1) "
            "ON CONFLICT(kind) DO UPDATE SET last_sent = excluded.last_sent, count = count + 1;",
            (kind, time.time()),
        )
        await self._db.commit()


def _user_from_row(row: aiosqlite.Row) -> UserRecord:
    """Build a :class:`UserRecord` from a joined users/plans row."""
    keys = row.keys()
    return UserRecord(
        id=row["id"],
        telegram_user_id=row["telegram_user_id"],
        username=row["username"],
        first_name=row["first_name"],
        status=row["status"],
        plan_id=row["plan_id"],
        plan_name=row["plan_name"] if "plan_name" in keys else None,
        prefix=row["prefix"] if "prefix" in keys else ".",
        tos_accepted_at=row["tos_accepted_at"] if "tos_accepted_at" in keys else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _instance_from_row(row: aiosqlite.Row) -> InstanceRecord:
    """Build an :class:`InstanceRecord` from a database row."""
    return InstanceRecord(
        id=row["id"],
        user_id=row["user_id"],
        account_id=row["account_id"],
        name=row["name"],
        session_path=row["session_path"],
        db_path=row["db_path"],
        auth_state=row["auth_state"],
        enabled=bool(row["enabled"]),
        status=row["status"],
        pid=row["pid"],
        started_at=row["started_at"],
        last_heartbeat=row["last_heartbeat"],
        active_tasks=int(row["active_tasks"] or 0),
        active_banners=int(row["active_banners"] or 0),
        restart_count=int(row["restart_count"] or 0),
        restart_requested=row["restart_requested"],
        last_error=row["last_error"],
        created_at=row["created_at"],
    )
