"""Core orchestration for the AI Assistant.

Ties together the Gemini client, persistence and extraction layers. Given a
parsed request and the (optional) replied-to Telegram message, it gathers the
relevant context (text, document text, or image bytes), builds an appropriate
prompt, calls Gemini asynchronously, records statistics and returns the answer.

Rate limiting uses an in-memory rolling window per user; model selection and
trusted users are delegated to :class:`~ai.database.AIDatabase`.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Optional

from telethon import TelegramClient
from telethon.tl.types import DocumentAttributeFilename

from .config import AIConfig
from .database import AIDatabase
from .errors import (
    AIError,
    EmptyContextError,
    FileTooLargeError,
    GeminiUnavailableError,
    ModelNotFoundError,
    RateLimitError,
)
from .extractors import (
    ExtractedContent,
    detect_image_mime,
    extract_document_text,
    truncate,
)
from .gemini import GeminiClient
from .prompts import (
    IMAGE_INTENTS,
    SMART_IMAGE_INSTRUCTION,
    SMART_TEXT_INSTRUCTION,
    TEXT_INTENTS,
)

logger = logging.getLogger("banner.ai.assistant")

#: Intent keywords recognised as the first ``.ai`` argument.
INTENT_KEYWORDS: frozenset[str] = frozenset(set(TEXT_INTENTS) | set(IMAGE_INTENTS))


class AIAssistant:
    """High-level AI operations backing the ``.ai`` command."""

    def __init__(
        self,
        client: TelegramClient,
        database: AIDatabase,
        config: AIConfig,
        *,
        store: object | None = None,
    ) -> None:
        """Initialise the assistant.

        Args:
            client: Connected Telethon client (used to download media).
            database: AI persistence layer.
            config: AI configuration.
            store: Optional shared store; when provided, fallback activations
                are recorded to the ``ai_fallback_activations`` counter for the
                global statistics.
        """
        self._client = client
        self._db = database
        self._config = config
        self._store = store
        self._gemini = GeminiClient(config.api_key, timeout=config.request_timeout)
        #: user_id -> recent request timestamps (rolling-window rate limiting).
        self._request_times: dict[int, list[float]] = defaultdict(list)

    def fallback_chain(self) -> list[str]:
        """Return the configured fallback model chain (excluding the primary)."""
        return list(self._config.fallback_models)

    @property
    def config(self) -> AIConfig:
        """The active configuration."""
        return self._config

    # ------------------------------------------------------------------ #
    # Model management
    # ------------------------------------------------------------------ #
    async def get_model(self) -> str:
        """Return the currently selected model (persisted, else default)."""
        return await self._db.get_model(self._config.default_model)

    def list_models(self) -> list[str]:
        """Return the list of selectable model names."""
        return list(self._config.models)

    async def set_model(self, name: str) -> str:
        """Validate and persist a new model selection.

        Args:
            name: Desired model name.

        Returns:
            The persisted model name.

        Raises:
            ModelNotFoundError: If ``name`` is not a known model.
        """
        if name not in self._config.models:
            raise ModelNotFoundError()
        await self._db.set_model(name)
        logger.info("AI model changed to %s", name)
        return name

    # ------------------------------------------------------------------ #
    # Rate limiting
    # ------------------------------------------------------------------ #
    def enforce_rate_limit(self, user_id: int) -> None:
        """Apply the per-user rolling-window rate limit.

        Args:
            user_id: The requesting user.

        Raises:
            RateLimitError: If the user exceeded the configured per-minute quota.
        """
        limit = self._config.rate_limit_per_minute
        if limit <= 0:
            return
        now = time.time()
        window = [t for t in self._request_times[user_id] if now - t < 60.0]
        if len(window) >= limit:
            self._request_times[user_id] = window
            raise RateLimitError()
        window.append(now)
        self._request_times[user_id] = window

    # ------------------------------------------------------------------ #
    # Main entry point
    # ------------------------------------------------------------------ #
    async def process(
        self,
        *,
        user_id: int,
        intent: Optional[str],
        text_arg: str,
        replied_message: object | None,
    ) -> str:
        """Run one AI request and return the response text.

        Args:
            user_id: The requesting user (for statistics).
            intent: Recognised intent keyword (e.g. ``"summarize"``) or ``None``
                for smart/direct mode.
            text_arg: Free-form text typed after ``.ai`` (a question or an extra
                instruction). May be empty.
            replied_message: The Telegram message being replied to, or ``None``.

        Returns:
            The model's response text.

        Raises:
            AIError: Any user-facing failure (empty context, unsupported file,
                Gemini failure, …); statistics record the failure.
        """
        logger.info("AI request user=%s intent=%s", user_id, intent or "smart")
        try:
            content = await self._gather(replied_message)
            parts = self._build_parts(intent, text_arg, content)
            current = await self.get_model()
            answer, model = await self._generate_with_fallback(current, parts)
        except AIError:
            await self._db.record_request(user_id, success=False)
            logger.warning("AI request failed user=%s intent=%s", user_id, intent)
            raise
        except Exception as exc:  # noqa: BLE001 - convert unknowns to AIError
            await self._db.record_request(user_id, success=False)
            logger.exception("Unexpected AI error: %s", exc)
            raise AIError() from exc

        await self._db.record_request(user_id, success=True)
        logger.info("AI success user=%s model=%s chars=%d", user_id, model, len(answer))
        return answer

    async def _generate_with_fallback(
        self, current: str, parts: list[dict]
    ) -> tuple[str, str]:
        """Generate content, walking the fallback chain on transient failures.

        Tries the active model first, then each configured fallback model in
        order. A *retryable* Gemini failure (5xx/429/timeout/quota/overload)
        advances to the next model and records a fallback activation; a
        *non-retryable* failure (blocked content, malformed request) is raised
        immediately since another model would not help.

        Args:
            current: The active/primary model.
            parts: The Gemini content parts.

        Returns:
            ``(answer, model)`` — the response text and the model that produced
            it.

        Raises:
            GeminiUnavailableError: If every model in the chain fails.
        """
        chain = self._config.fallback_chain(current)
        last_error: Optional[GeminiUnavailableError] = None

        for index, model in enumerate(chain):
            try:
                answer = await self._gemini.generate(model, parts)
                if index > 0:
                    logger.info(
                        "AI fallback recovered on model %s (after %d failure(s))",
                        model,
                        index,
                    )
                return answer, model
            except GeminiUnavailableError as exc:
                last_error = exc
                if not exc.retryable or index == len(chain) - 1:
                    # Permanent failure, or no models left to try.
                    raise
                next_model = chain[index + 1]
                logger.warning(
                    "AI model %s failed (retryable); falling back to %s",
                    model,
                    next_model,
                )
                await self._record_fallback()

        # Defensive: the loop always returns or raises, but satisfy the type.
        raise last_error or GeminiUnavailableError()

    async def _record_fallback(self) -> None:
        """Increment the global fallback-activation counter (best-effort)."""
        if self._store is None:
            return
        try:
            from core.store import COUNTER_AI_FALLBACKS

            await self._store.incr(COUNTER_AI_FALLBACKS)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - stats must never break a request
            logger.debug("Could not record AI fallback activation")

    # ------------------------------------------------------------------ #
    # Prompt assembly
    # ------------------------------------------------------------------ #
    def _build_parts(
        self, intent: Optional[str], text_arg: str, content: ExtractedContent
    ) -> list[dict]:
        """Build Gemini content parts from the request and gathered context.

        Args:
            intent: Recognised intent keyword or ``None``.
            text_arg: Free-form text typed after ``.ai``.
            content: The extracted reply content.

        Returns:
            The ordered list of Gemini parts.

        Raises:
            EmptyContextError: If there is nothing to send to the model.
        """
        text_arg = text_arg.strip()

        if content.kind == "image":
            instruction = self._image_instruction(intent, text_arg)
            assert content.image_bytes is not None
            return [
                self._gemini.text_part(instruction),
                self._gemini.image_part(content.image_bytes, content.mime_type or "image/jpeg"),
            ]

        context = (content.text or "").strip()
        prompt = self._text_prompt(intent, text_arg, context)
        if not prompt.strip():
            raise EmptyContextError()
        return [self._gemini.text_part(truncate(prompt, self._config.max_input_chars))]

    def _text_prompt(self, intent: Optional[str], text_arg: str, context: str) -> str:
        """Compose the text prompt for text/document requests."""
        context = truncate(context, self._config.max_input_chars)

        if context:
            # There is replied content to act on.
            if intent:
                instruction = TEXT_INTENTS.get(
                    intent, f"{intent.capitalize()} the following:"
                )
                if text_arg:
                    instruction = f"{instruction}\n(Extra instruction: {text_arg})"
                return f"{instruction}\n\n{context}"
            if text_arg:
                return f"{text_arg}\n\nContent:\n{context}"
            return f"{SMART_TEXT_INSTRUCTION}\n{context}"

        # No replied content: the typed text is the content/question.
        if intent:
            if not text_arg:
                raise EmptyContextError()
            instruction = TEXT_INTENTS.get(
                intent, f"{intent.capitalize()} the following:"
            )
            return f"{instruction}\n\n{text_arg}"
        if text_arg:
            return text_arg
        raise EmptyContextError()

    @staticmethod
    def _image_instruction(intent: Optional[str], text_arg: str) -> str:
        """Compose the instruction text for an image request."""
        if intent and text_arg:
            base = IMAGE_INTENTS.get(intent, "")
            return f"{base} {text_arg}".strip()
        if text_arg:
            return text_arg
        if intent and intent in IMAGE_INTENTS:
            return IMAGE_INTENTS[intent]
        return SMART_IMAGE_INSTRUCTION

    # ------------------------------------------------------------------ #
    # Context gathering from the replied message
    # ------------------------------------------------------------------ #
    async def _gather(self, message: object | None) -> ExtractedContent:
        """Inspect a replied message and extract usable content.

        Args:
            message: The Telethon message replied to, or ``None``.

        Returns:
            An :class:`ExtractedContent` describing the content (possibly
            ``kind="none"``).

        Raises:
            FileTooLargeError: If an attached document exceeds the size limit.
            UnsupportedFileError: If a document type cannot be processed.
        """
        if message is None:
            return ExtractedContent(kind="none")

        mime, filename = self._media_meta(message)
        image_mime = detect_image_mime(mime, filename)

        # Image: either a photo or an image-typed document/sticker.
        if getattr(message, "photo", None) or image_mime:
            data = await self._download(message)
            return ExtractedContent(
                kind="image",
                image_bytes=data,
                mime_type=image_mime or "image/jpeg",
                filename=filename,
            )

        # Document: enforce size limit, then extract text.
        document = getattr(message, "document", None)
        if document is not None:
            size = int(getattr(document, "size", 0) or 0)
            if size > self._config.max_file_size_bytes:
                raise FileTooLargeError()
            data = await self._download(message)
            text = extract_document_text(data, filename, mime)
            return ExtractedContent(kind="document", text=text, filename=filename)

        # Plain text message.
        text = getattr(message, "message", None) or getattr(message, "raw_text", None) or ""
        return ExtractedContent(kind="text", text=text)

    async def _download(self, message: object) -> bytes:
        """Download a message's media into memory, enforcing the size cap.

        Args:
            message: The message whose media to download.

        Returns:
            The downloaded bytes.

        Raises:
            FileTooLargeError: If the downloaded payload exceeds the limit.
        """
        data = await self._client.download_media(message, file=bytes)
        if data is None:
            raise EmptyContextError()
        if len(data) > self._config.max_file_size_bytes:
            raise FileTooLargeError()
        return data

    @staticmethod
    def _media_meta(message: object) -> tuple[Optional[str], Optional[str]]:
        """Return ``(mime_type, filename)`` for a message's document, if any."""
        document = getattr(message, "document", None)
        if document is None:
            return None, None
        mime = getattr(document, "mime_type", None)
        filename: Optional[str] = None
        for attr in getattr(document, "attributes", []) or []:
            if isinstance(attr, DocumentAttributeFilename):
                filename = attr.file_name
                break
        return mime, filename
