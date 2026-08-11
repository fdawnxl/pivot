"""Configuration loading and workspace initialization."""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .credentials import CredentialStore
from .logging import configure_logging

LOGGER = logging.getLogger(__name__)


class ConfigurationError(ValueError):
    """Raised when configuration is missing or invalid."""


def _validate_log_level(value: str, *, setting: str) -> str:
    normalized = value.upper()
    if normalized not in {"DEBUG", "INFO", "WARN", "WARNING", "ERROR"}:
        raise ConfigurationError(f"Invalid {setting} value: {value!r}")
    return normalized


@dataclass(frozen=True, slots=True)
class PivotConfig:
    workspace_path: Path
    model: str = "gpt-4o-mini"
    api_base: str | None = None
    api_key: str | None = None
    provider: str | None = None
    max_rounds: int = 8
    llm_timeout: float = 120.0
    log_console_level: str = "INFO"
    log_file_level: str = "DEBUG"

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
        logging_values = values.get("logging", {}) if isinstance(values.get("logging", {}), dict) else {}
        # Support both the initial flat schema and the clearer [llm] schema.
        merged: dict[str, Any] = {**values, **llm_values, **logging_values}
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

        legacy_log_level = get("log_level", None)
        console_level = os.getenv("PIVOT_LOG_DISPLAY_LEVEL") or get(
            "log_console_level", merged.get("display_level", legacy_log_level or "INFO")
        )
        file_level = os.getenv("PIVOT_LOG_STORAGE_LEVEL") or get(
            "log_file_level", merged.get("storage_level", legacy_log_level or "DEBUG")
        )
        config = cls(
            workspace_path=workspace,
            model=get("model", "gpt-4o-mini"),
            api_base=get("api_base", None),
            api_key=os.getenv("PIVOT_API_KEY") or stored_credentials.get("api_key"),
            provider=get("provider", None),
            max_rounds=get("max_rounds", 8, int),
            llm_timeout=get("llm_timeout", 120.0, float),
            log_console_level=_validate_log_level(console_level, setting="display log level"),
            log_file_level=_validate_log_level(file_level, setting="storage log level"),
        )
        if config.max_rounds < 1 or config.llm_timeout <= 0:
            raise ConfigurationError("max_rounds must be positive and llm_timeout must be greater than zero")
        config.ensure_workspace()
        configure_logging(
            config.log_console_level,
            file_level=config.log_file_level,
            log_path=config.workspace_path / "logs" / "pivot.log",
        )
        LOGGER.info("Workspace initialized path=%s", config.workspace_path)
        LOGGER.debug(
            "Configuration loaded model=%s provider=%s api_base_configured=%s max_rounds=%d llm_timeout=%g",
            config.model,
            config.provider or "auto",
            config.api_base is not None,
            config.max_rounds,
            config.llm_timeout,
        )
        return config

    def ensure_workspace(self) -> None:
        """Create the documented workspace directories without overwriting user files."""

        for relative in (
            "capabilities/think",
            "capabilities/measure",
            "capabilities/work",
            "events",
            "memory",
            "logs",
            "environment/measure",
            "environment/event",
        ):
            (self.workspace_path / relative).mkdir(parents=True, exist_ok=True)
        measure_project = self.workspace_path / "environment" / "measure" / "pyproject.toml"
        if not measure_project.exists():
            measure_project.write_text(
                '[project]\nname = "pivot-measure-environment"\nversion = "0.1.0"\nrequires-python = ">=3.11"\ndependencies = []\n',
                encoding="utf-8",
            )
        event_project = self.workspace_path / "environment" / "event" / "pyproject.toml"
        if not event_project.exists():
            event_project.write_text(
                '[project]\nname = "pivot-event-environment"\nversion = "0.1.0"\nrequires-python = ">=3.11"\ndependencies = []\n',
                encoding="utf-8",
            )
        config_file = self.workspace_path / "config.toml"
        if not config_file.exists():
            config_file.write_text("# pivot workspace configuration\n", encoding="utf-8")
