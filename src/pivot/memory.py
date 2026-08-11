"""Simple durable text memory with atomic writes."""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from uuid import UUID

LOGGER = logging.getLogger(__name__)


class TextMemory:
    """Store one UTF-8 transcript in a UUID-named session directory."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def directory_for(self, session_id: str) -> Path:
        """Return the canonical UUID directory for a session."""

        try:
            canonical = str(UUID(session_id))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError("session_id must be a valid UUID") from exc
        return self.root / canonical

    def path_for(self, session_id: str) -> Path:
        return self.directory_for(session_id) / "history.jsonl"

    def read(self, session_id: str) -> str:
        path = self.path_for(session_id)
        try:
            content = path.read_text(encoding="utf-8") if path.exists() else ""
        except OSError as exc:
            LOGGER.error("Unable to read session memory session_id=%s error_type=%s", session_id, type(exc).__name__)
            raise OSError(f"Unable to read memory for session {session_id!r}") from exc
        LOGGER.debug("Session memory read session_id=%s bytes=%d", session_id, len(content.encode("utf-8")))
        return content

    def write(self, session_id: str, content: str) -> None:
        path = self.path_for(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            LOGGER.debug("Session memory written session_id=%s bytes=%d path=%s", session_id, len(content.encode("utf-8")), path)
        except OSError as exc:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            LOGGER.error("Unable to write session memory session_id=%s error_type=%s", session_id, type(exc).__name__)
            raise OSError(f"Unable to write memory for session {session_id!r}") from exc

    def append(self, session_id: str, content: str) -> None:
        existing = self.read(session_id)
        separator = "\n" if existing and not existing.endswith("\n") else ""
        self.write(session_id, existing + separator + content)
