"""Permission-aware provider credentials stored in a instance TOML file."""

from __future__ import annotations

import logging
import os
import stat
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

LOGGER = logging.getLogger(__name__)


class CredentialError(RuntimeError):
    """Raised when provider credentials cannot be loaded or persisted safely."""


@dataclass(frozen=True, slots=True)
class ProviderCredential:
    """One named LLM provider connection."""

    name: str
    model: str
    api_base: str | None = None
    api_key: str | None = None


class CredentialStore:
    """Load and atomically persist named providers in ``credentials.toml``."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()

    def read(self) -> dict[str, ProviderCredential]:
        if not self.path.is_file():
            LOGGER.debug("Credentials file is not present path=%s", self.path)
            return {}
        try:
            with self.path.open("rb") as handle:
                value: Any = tomllib.load(handle)
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise CredentialError(f"Cannot read credentials file {self.path}") from exc
        raw_providers = value.get("providers") if isinstance(value, dict) else None
        if not isinstance(raw_providers, dict):
            raise CredentialError("Credentials file must contain a [providers] table")
        providers: dict[str, ProviderCredential] = {}
        for name, raw in raw_providers.items():
            if not isinstance(name, str) or not name or not isinstance(raw, dict):
                raise CredentialError("Each provider must be a named TOML table")
            model = raw.get("model")
            if not isinstance(model, str) or not model.strip():
                raise CredentialError(f"Provider {name!r} must define a non-empty model")
            api_base = _optional_string(raw, "api_base", provider=name)
            api_key = _optional_string(raw, "api_key", provider=name)
            providers[name] = ProviderCredential(name=name, model=model, api_base=api_base, api_key=api_key)
        LOGGER.debug("Provider credentials loaded path=%s providers=%s", self.path, sorted(providers))
        return providers

    def save(self, providers: Mapping[str, ProviderCredential]) -> None:
        """Persist provider records with mode ``0600`` using an atomic replace."""

        try:
            import tomli_w
        except ImportError as exc:  # pragma: no cover - declared runtime dependency
            raise CredentialError("tomli-w is required to save provider credentials") from exc
        document = {
            "providers": {
                name: {
                    key: value
                    for key, value in {
                        "model": provider.model,
                        "api_base": provider.api_base,
                        "api_key": provider.api_key,
                    }.items()
                    if value is not None
                }
                for name, provider in providers.items()
            }
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                tomli_w.dump(document, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
            os.replace(temporary, self.path)
            LOGGER.info("Provider credentials saved path=%s providers=%s", self.path, sorted(providers))
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise CredentialError(f"Cannot save credentials file {self.path}") from exc


def _optional_string(source: Mapping[str, Any], key: str, *, provider: str) -> str | None:
    value = source.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise CredentialError(f"Provider {provider!r} field {key!r} must be a non-empty string")
    return value


__all__ = ["CredentialError", "CredentialStore", "ProviderCredential"]
