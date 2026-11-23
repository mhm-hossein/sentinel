"""Business logic for the Quick Variables module.

A thin layer over :class:`~variables.database.VariablesDatabase` that enforces
keyword normalisation/validation and the add-vs-edit distinction. Values are
stored exactly as entered (multiline preserved).
"""

from __future__ import annotations

import logging
from typing import Optional

from .database import VariablesDatabase
from .models import Variable
from .utils import normalize_keyword, validate_keyword

logger = logging.getLogger("sentinel.variables.manager")


class VariableError(Exception):
    """Raised for user-facing variable errors (shown verbatim to the owner)."""


class VariablesManager:
    """Validate and apply variable operations against the database."""

    def __init__(self, database: VariablesDatabase) -> None:
        """Initialise the manager.

        Args:
            database: A connected :class:`VariablesDatabase`.
        """
        self._db = database

    async def add(self, keyword: str, value: str) -> Variable:
        """Create a new alias.

        Args:
            keyword: Raw keyword (normalised + validated here).
            value: The text value, stored verbatim.

        Returns:
            The newly created :class:`Variable`.

        Raises:
            VariableError: On invalid/reserved keyword, empty value, or an
                already-existing keyword.
        """
        kw = self._require_keyword(keyword)
        if not value:
            raise VariableError("Value is required")
        if not await self._db.add(kw, value):
            raise VariableError(f"'{kw}' already exists — use .var edit")
        logger.info("Variable added: %s", kw)
        variable = await self._db.get(kw)
        assert variable is not None
        return variable

    async def edit(self, keyword: str, value: str) -> Variable:
        """Update an existing alias's value.

        Raises:
            VariableError: On invalid keyword, empty value, or unknown keyword.
        """
        kw = self._require_keyword(keyword)
        if not value:
            raise VariableError("Value is required")
        if not await self._db.edit(kw, value):
            raise VariableError(f"'{kw}' does not exist — use .var add")
        logger.info("Variable edited: %s", kw)
        variable = await self._db.get(kw)
        assert variable is not None
        return variable

    async def remove(self, keyword: str) -> str:
        """Delete an alias.

        Returns:
            The normalised keyword that was removed.

        Raises:
            VariableError: On invalid or unknown keyword.
        """
        kw = self._require_keyword(keyword)
        if not await self._db.remove(kw):
            raise VariableError(f"'{kw}' does not exist")
        logger.info("Variable removed: %s", kw)
        return kw

    async def get(self, keyword: str) -> Optional[Variable]:
        """Fetch an alias by (normalised) keyword, or ``None``."""
        return await self._db.get(normalize_keyword(keyword))

    async def lookup_value(self, keyword: str) -> Optional[str]:
        """Return the stored value for an alias, or ``None`` if absent.

        Used by the alias executor on the hot path; performs no validation
        (an unknown keyword simply yields ``None``).
        """
        variable = await self._db.get(normalize_keyword(keyword))
        return variable.value if variable else None

    async def list_keywords(self) -> list[str]:
        """Return every keyword, alphabetically."""
        return await self._db.list_keywords()

    async def count(self) -> int:
        """Return the number of stored aliases."""
        return await self._db.count()

    @staticmethod
    def _require_keyword(raw: str) -> str:
        """Normalise + validate a keyword or raise :class:`VariableError`."""
        kw = normalize_keyword(raw)
        error = validate_keyword(kw)
        if error:
            raise VariableError(error)
        return kw
