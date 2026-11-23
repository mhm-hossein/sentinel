"""Media and document extraction helpers for the AI module.

Determines the type of a replied Telegram message's attachment and turns it
into something Gemini can consume: raw image bytes for vision, or extracted
plain text for documents. All extraction is defensive — unsupported or
corrupt files raise a clean :class:`UnsupportedFileError` rather than crashing.
"""

from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass
from typing import Optional

from .errors import UnsupportedFileError

logger = logging.getLogger("banner.ai.extractors")

#: Image MIME types accepted for vision requests.
SUPPORTED_IMAGE_MIMES = frozenset(
    {"image/jpeg", "image/jpg", "image/png", "image/webp"}
)

#: Document extensions handled by :func:`extract_document_text`.
SUPPORTED_DOC_EXTENSIONS = frozenset({".pdf", ".txt", ".md", ".docx"})


@dataclass(slots=True)
class ExtractedContent:
    """Result of inspecting a replied message's attachment.

    Exactly one of ``image_bytes`` / ``text`` is populated for media; ``kind``
    classifies the content for prompt building.

    Attributes:
        kind: One of ``"image"``, ``"document"`` or ``"none"``.
        text: Extracted document text (for documents).
        image_bytes: Raw image bytes (for images).
        mime_type: MIME type of the image (for images).
        filename: Original filename when known.
    """

    kind: str
    text: Optional[str] = None
    image_bytes: Optional[bytes] = None
    mime_type: Optional[str] = None
    filename: Optional[str] = None


def truncate(text: str, limit: int) -> str:
    """Truncate ``text`` to ``limit`` characters with a visible marker.

    Args:
        text: Input text.
        limit: Maximum number of characters to keep.

    Returns:
        The text unchanged if within the limit, otherwise truncated with a
        ``"\\n…[truncated]"`` suffix.
    """
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n…[truncated]"


def detect_image_mime(mime_type: Optional[str], filename: Optional[str]) -> Optional[str]:
    """Return a supported image MIME type for the attachment, or ``None``.

    Args:
        mime_type: The Telegram-reported MIME type, if any.
        filename: The filename, used as a fallback by extension.

    Returns:
        A canonical supported image MIME type, or ``None`` if not an image we
        support.
    """
    if mime_type and mime_type.lower() in SUPPORTED_IMAGE_MIMES:
        m = mime_type.lower()
        return "image/jpeg" if m == "image/jpg" else m
    ext = _extension(filename)
    by_ext = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }
    return by_ext.get(ext)


def extract_document_text(data: bytes, filename: Optional[str], mime_type: Optional[str]) -> str:
    """Extract plain text from a supported document's bytes.

    Supports PDF, TXT, MD and DOCX. PDF/DOCX rely on optional third-party
    libraries; if those are missing the function raises
    :class:`UnsupportedFileError` instead of crashing.

    Args:
        data: Raw document bytes.
        filename: Original filename (used to pick the extractor).
        mime_type: Reported MIME type (used as a secondary hint).

    Returns:
        The extracted text (may be empty if the document has no text layer).

    Raises:
        UnsupportedFileError: If the type is unsupported or extraction fails.
    """
    ext = _extension(filename)
    if not ext and mime_type:
        ext = _ext_from_mime(mime_type)

    if ext in (".txt", ".md"):
        return data.decode("utf-8", errors="replace")
    if ext == ".pdf":
        return _extract_pdf(data)
    if ext == ".docx":
        return _extract_docx(data)

    raise UnsupportedFileError()


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #
def _extension(filename: Optional[str]) -> str:
    """Return the lowercased file extension (including the dot), or ``""``."""
    if not filename:
        return ""
    return os.path.splitext(filename)[1].lower()


def _ext_from_mime(mime_type: str) -> str:
    """Map a document MIME type to a file extension."""
    mapping = {
        "application/pdf": ".pdf",
        "text/plain": ".txt",
        "text/markdown": ".md",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    }
    return mapping.get(mime_type.lower(), "")


def _extract_pdf(data: bytes) -> str:
    """Extract text from PDF bytes using :mod:`pypdf`."""
    try:
        from pypdf import PdfReader
    except Exception as exc:  # noqa: BLE001 - dependency may be absent
        logger.error("pypdf not installed: %s", exc)
        raise UnsupportedFileError("⚠ PDF support unavailable") from exc
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:  # noqa: BLE001 - corrupt/edge-case PDFs
        logger.error("PDF extraction failed: %s", exc)
        raise UnsupportedFileError("⚠ Could not read PDF") from exc


def _extract_docx(data: bytes) -> str:
    """Extract text from DOCX bytes using :mod:`python-docx`."""
    try:
        import docx  # python-docx
    except Exception as exc:  # noqa: BLE001 - dependency may be absent
        logger.error("python-docx not installed: %s", exc)
        raise UnsupportedFileError("⚠ DOCX support unavailable") from exc
    try:
        document = docx.Document(io.BytesIO(data))
        return "\n".join(p.text for p in document.paragraphs)
    except Exception as exc:  # noqa: BLE001 - corrupt/edge-case DOCX
        logger.error("DOCX extraction failed: %s", exc)
        raise UnsupportedFileError("⚠ Could not read DOCX") from exc
