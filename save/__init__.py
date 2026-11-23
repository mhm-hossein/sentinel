"""Save Message module — copy protected messages into Saved Messages.

Downloads a message's content and re-uploads it to the owner's Saved Messages,
which works even where Telegram blocks forwarding (Protected Content / forward
or copy restricted chats). Text, photos, video, voice, audio, documents,
stickers, GIFs and whole media groups are supported, each annotated with source
metadata and a best-effort message link.
"""

from __future__ import annotations

from .commands import SaveCommands, help_section
from .saver import MessageSaver

__all__ = ["MessageSaver", "SaveCommands", "help_section"]
