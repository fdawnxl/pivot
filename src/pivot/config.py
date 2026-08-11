"""Configuration loading and workspace initialization."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .logging import configure_logging
from .credentials import CredentialStore


class ConfigurationError(ValueError):
    """Raised when configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class PivotConfig:
    workspace_path: Path
    model: str = "gpt-4o-mini"
    api_base: str | None = None
    api_key: str | None = None
    provider: str | None = None
    max_rounds: int = 8
    llm_timeout: float = 120.0
    log_level: str = "INFO"

    @classmethod
    def load(cls, *, workspace_path: str | Path | None = None, config_path: str | Path | None = None) -> "PivotConfig":
        """Load config with env > TOML > defaults, then initialize workspace."""

        explicit_workspace = workspace_path or os.getenv("PIVOT_WORKSPACE_PATH")
        if not explicit_workspace:
            raise ConfigurationError("PIVOT_WORKSPACE_PATH or --workspace is required")
        workspace = Path(explicit_workspace).expanduser().resolve()
        toml_file = Path(config_path).expanduser() if config_path else workspace / "config.toml"
        values: dict[str, Any] = {}
        if toml_file.is_file():
            try:
                with toml_file.open("rb") as handle:
                    loaded = tomllib.load(handle)
                values.update(loaded.get("pivot", loaded))
            except (OSError, tomllib.TOMLDecodeError) as exc:
                raise ConfigurationError(f"Cannot load configuration {toml_file}: {exc}") from exc

        llm_values = values.get("llm", {}) if isinstance(values.get("llm", {}), dict) else {}
        # Support both the initial flat schema and the clearer [llm] schema.
        merged: dict[str, Any] = {**values, **llm_values}
        credentials = CredentialStore(workspace / "credentials.json")
        stored_credentials = credentials.read()

        def get(name: str, default: Any, cast: Any = str) -> Any:
            env_name = f"PIVOT_{name.upper()}"
            value = os.getenv(env_name, merged.get(name.lower(), default))
            if value is None or cast is str:
                return value
            try:
                return cast(value)
            except (TypeError, ValueError) as exc:
                raise ConfigurationError(f"Invalid {env_name} value: {value!r}") from exc

        config = cls(
            workspace_path=workspace,
            model=get("model", "gpt-4o-mini"),
            api_base=get("api_base", None),
            api_key=os.getenv("PIVOT_API_KEY") or stored_credentials.get("api_key"),
            provider=get("provider", None),
            max_rounds=get("max_rounds", 8, int),
            llm_timeout=get("llm_timeout", 120.0, float),
            log_level=get("log_level", "INFO"),
        )
        if config.max_rounds < 1 or config.llm_timeout <= 0:
            raise ConfigurationError("max_rounds must be positive and llm_timeout must be greater than zero")
        config.ensure_workspace()
        configure_logging(config.log_level)
        return config

    def ensure_workspace(self) -> None:
        """Create the documented workspace directories without overwriting user files."""

        for relative in ("capabilities/think", "capabilities/measure", "capabilities/work", "events", "memory", "logs", "measure-env", "event-env"):
            (self.workspace_path / relative).mkdir(parents=True, exist_ok=True)
        measure_project = self.workspace_path / "measure-env" / "pyproject.toml"
        if not measure_project.exists():
            measure_project.write_text(
                '[project]\nname = "pivot-measure-environment"\nversion = "0.1.0"\nrequires-python = ">=3.11"\ndependencies = []\n',
                encoding="utf-8",
            )
        event_project = self.workspace_path / "event-env" / "pyproject.toml"
        if not event_project.exists():
            event_project.write_text(
                '[project]\nname = "pivot-event-environment"\nversion = "0.1.0"\nrequires-python = ">=3.11"\ndependencies = []\n',
                encoding="utf-8",
            )
        config_file = self.workspace_path / "config.toml"
        if not config_file.exists():
            config_file.write_text("# pivot workspace configuration\n", encoding="utf-8")
