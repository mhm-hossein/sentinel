"""Exception hierarchy for the AI Assistant module.

All user-facing failures derive from :class:`AIError` and carry a short,
safe message suitable for display in Telegram. Internal details (API keys,
stack traces, raw responses) are never embedded in these messages.
"""

from __future__ import annotations


class AIError(Exception):
    """Base class for all AI module errors.

    The string form is a concise, user-safe message (e.g. ``"⚠ AI Error"``).
    """

    user_message = "⚠ AI Error"

    def __str__(self) -> str:  # noqa: D105 - trivial
        return self.args[0] if self.args else self.user_message


class AccessDeniedError(AIError):
    """Raised when a non-trusted user attempts to use the AI module."""

    user_message = "⛔ Access Denied"


class RateLimitError(AIError):
    """Raised when a user exceeds their per-minute request quota."""

    user_message = "⚠ Rate limit reached. Try again shortly."


class ModelNotFoundError(AIError):
    """Raised when an unknown model name is selected."""

    user_message = "⚠ Model Not Found"


class UnsupportedFileError(AIError):
    """Raised when a replied file type cannot be processed."""

    user_message = "⚠ Unsupported File"


class FileTooLargeError(AIError):
    """Raised when a file exceeds the configured size limit."""

    user_message = "⚠ File too large"


class MissingAPIKeyError(AIError):
    """Raised when the Gemini API key is not configured."""

    user_message = "⚠ AI unavailable (no API key)"


class GeminiUnavailableError(AIError):
    """Raised when the Gemini API call fails, times out, or returns an error.

    Carries a ``retryable`` flag indicating whether trying a different model in
    the fallback chain might succeed (transient/availability errors) versus a
    permanent failure (blocked content, malformed request) where falling back
    is pointless.
    """

    user_message = "⚠ Gemini Unavailable"

    def __init__(self, *args: object, retryable: bool = False) -> None:
        super().__init__(*args)
        self.retryable = retryable


class EmptyContextError(AIError):
    """Raised when a command needs content but none was provided."""

    user_message = "⚠ Nothing to process. Add text or reply to a message."
