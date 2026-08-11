"""Simple durable text memory with atomic writes."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


class TextMemory:
    """Store one UTF-8 transcript per session under a workspace memory directory."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, session_id: str) -> Path:
        safe = "".join(char if char.isalnum() or char in "-_." else "_" for char in session_id).strip(".")
        if not safe:
            raise ValueError("session_id must contain at least one safe character")
        return self.root / f"{safe}.txt"

    def read(self, session_id: str) -> str:
        path = self.path_for(session_id)
        try:
            return path.read_text(encoding="utf-8") if path.exists() else ""
        except OSError as exc:
            raise OSError(f"Unable to read memory for session {session_id!r}") from exc

    def write(self, session_id: str, content: str) -> None:
        path = self.path_for(session_id)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise OSError(f"Unable to write memory for session {session_id!r}") from exc

    def append(self, session_id: str, content: str) -> None:
        existing = self.read(session_id)
        separator = "\n" if existing and not existing.endswith("\n") else ""
        self.write(session_id, existing + separator + content)
