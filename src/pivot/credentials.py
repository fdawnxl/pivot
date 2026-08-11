"""Small, permission-aware credential store for a local pivot workspace."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any


class CredentialError(RuntimeError):
    """Raised when credentials cannot be read or persisted safely."""


class CredentialStore:
    """Persist provider credentials in a JSON file readable only by its owner."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()

    def read(self) -> dict[str, str]:
        if not self.path.is_file():
            return {}
        try:
            value: Any = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CredentialError(f"Cannot read credentials file {self.path}") from exc
        if not isinstance(value, dict):
            raise CredentialError("Credentials file must contain a JSON object")
        return {str(key): str(item) for key, item in value.items() if item is not None}

    def save(self, credentials: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        try:
            temporary.write_text(json.dumps(credentials, indent=2) + "\n", encoding="utf-8")
            os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
            os.replace(temporary, self.path)
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise CredentialError(f"Cannot save credentials file {self.path}") from exc
