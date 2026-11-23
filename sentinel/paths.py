"""Per-user filesystem layout for the multi-user platform.

Every tenant gets an isolated directory tree::

    <data_root>/users/<telegram_user_id>/
        sentinel.db        # all existing module tables, per user
        session.session    # this user's authorized Telethon session
        exports/           # export / search / archive / ghost / hunter dumps
        backups/           # backup zips
        cache/             # tracking autosave + transient media

The data root defaults to ``data`` (``SENTINEL_DATA_ROOT`` overrides it). The
central control-plane database lives at ``<data_root>/central.db``
(``SENTINEL_CENTRAL_DB`` overrides the full path).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def data_root() -> str:
    """Return the platform data root (``SENTINEL_DATA_ROOT`` or ``data``)."""
    return os.environ.get("SENTINEL_DATA_ROOT", "data")


def central_db_path() -> str:
    """Return the central control-plane database path.

    Resolution: ``SENTINEL_CENTRAL_DB`` -> ``<data_root>/central.db``.
    """
    explicit = os.environ.get("SENTINEL_CENTRAL_DB")
    if explicit:
        return explicit
    return os.path.join(data_root(), "central.db")


@dataclass(slots=True)
class UserPaths:
    """Resolved, isolated storage paths for a single tenant.

    Attributes:
        user_id: The tenant's Telegram user id.
        root: The user's base directory (``<data_root>/users/<id>``).
        db_path: Per-user SQLite database file.
        session: Per-user Telethon session file (without the ``.session`` suffix
            when handed to Telethon, but stored here with the full name).
        exports: Directory for export/search/archive dumps.
        backups: Directory for backup zips.
        cache: Directory for transient media / autosave.
    """

    user_id: int
    root: str
    db_path: str
    session: str
    exports: str
    backups: str
    cache: str

    @classmethod
    def for_user(cls, user_id: int, *, root: str | None = None) -> "UserPaths":
        """Build (without creating) the path set for ``user_id``.

        Args:
            user_id: The tenant's Telegram user id.
            root: Optional explicit data root (defaults to :func:`data_root`).

        Returns:
            A populated :class:`UserPaths` (directories are not yet created;
            call :meth:`ensure`).
        """
        base = os.path.join(root or data_root(), "users", str(user_id))
        return cls(
            user_id=user_id,
            root=base,
            db_path=os.path.join(base, "sentinel.db"),
            session=os.path.join(base, "session.session"),
            exports=os.path.join(base, "exports"),
            backups=os.path.join(base, "backups"),
            cache=os.path.join(base, "cache"),
        )

    @property
    def session_name(self) -> str:
        """Session path without the trailing ``.session`` (Telethon convention).

        Telethon appends ``.session`` itself, so it must be given the base path.
        """
        if self.session.endswith(".session"):
            return self.session[: -len(".session")]
        return self.session

    def ensure(self) -> "UserPaths":
        """Create the user's base + sub-directories idempotently.

        Returns:
            ``self`` for chaining.
        """
        for path in (self.root, self.exports, self.backups, self.cache):
            os.makedirs(path, exist_ok=True)
        return self


@dataclass(slots=True)
class InstancePaths:
    """Isolated storage for a single selfbot *instance* of a user.

    Layout (multi-instance): ``<data_root>/users/<owner_id>/instances/<instance_id>/``
    containing ``sentinel.db``, ``session.session``, ``exports/``, ``backups/``,
    ``cache/``. Structurally identical to :class:`UserPaths` (same attribute
    names) so the feature-wiring layer consumes either without change.

    Attributes:
        owner_id: The panel user's Telegram id (the account owner).
        instance_id: The instance's central-DB primary key.
        root: The instance's base directory.
        db_path / session / exports / backups / cache: Per-instance paths.
    """

    owner_id: int
    instance_id: int
    root: str
    db_path: str
    session: str
    exports: str
    backups: str
    cache: str

    @classmethod
    def for_instance(
        cls, owner_id: int, instance_id: int, *, root: str | None = None
    ) -> "InstancePaths":
        """Build (without creating) the path set for one instance."""
        base = os.path.join(
            root or data_root(), "users", str(owner_id), "instances", str(instance_id)
        )
        return cls(
            owner_id=owner_id,
            instance_id=instance_id,
            root=base,
            db_path=os.path.join(base, "sentinel.db"),
            session=os.path.join(base, "session.session"),
            exports=os.path.join(base, "exports"),
            backups=os.path.join(base, "backups"),
            cache=os.path.join(base, "cache"),
        )

    @property
    def session_name(self) -> str:
        """Session path without the trailing ``.session`` (Telethon convention)."""
        if self.session.endswith(".session"):
            return self.session[: -len(".session")]
        return self.session

    def ensure(self) -> "InstancePaths":
        """Create the instance's base + sub-directories idempotently."""
        for path in (self.root, self.exports, self.backups, self.cache):
            os.makedirs(path, exist_ok=True)
        return self

    def purge(self) -> bool:
        """Recursively delete the entire instance directory (missing-safe).

        Removes ``<data_root>/users/<owner_id>/instances/<instance_id>/`` and
        everything under it — the session file, ``sentinel.db`` (+ WAL/SHM),
        exports, backups, cache and ``worker.log``. Uses ``ignore_errors`` so a
        missing directory or an already-deleted file never raises.

        Returns:
            ``True`` if the directory no longer exists after the call.
        """
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)
        return not os.path.exists(self.root)
