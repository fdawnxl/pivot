"""Configuration loading and workspace initialization."""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .credentials import CredentialStore, ProviderCredential
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
    provider: ProviderCredential
    max_rounds: int = 8
    llm_timeout: float = 120.0
    capability_timeout: float = 15.0
    event_poll_interval: float = 1.0
    event_max_wait: float = 3600.0
    dependency_install_timeout: float = 300.0
    dependency_start_timeout: float = 15.0
    dependency_dbus_timeout: float = 1.0
    dependency_stop_timeout: float = 5.0
    log_console_level: str = "INFO"
    log_file_level: str = "DEBUG"

    @classmethod
    def load(cls, *, workspace_path: str | Path | None = None, config_path: str | Path | None = None) -> "PivotConfig":
        """Load config with env > TOML > defaults, then initialize workspace."""

        explicit_workspace = workspace_path or os.getenv("PIVOT_WORKSPACE_PATH")
        if not explicit_workspace:
            raise ConfigurationError("PIVOT_WORKSPACE_PATH or --workspace is required")
        workspace = Path(explicit_workspace).expanduser().resolve()
        _ensure_workspace(workspace)
        toml_file = Path(config_path).expanduser() if config_path else workspace / "config.toml"
        values: dict[str, Any] = {}
        if toml_file.is_file():
            try:
                with toml_file.open("rb") as handle:
                    loaded = tomllib.load(handle)
                values.update(loaded.get("pivot", loaded))
            except (OSError, tomllib.TOMLDecodeError) as exc:
                raise ConfigurationError(f"Cannot load configuration {toml_file}: {exc}") from exc

        logging_values = values.get("logging", {}) if isinstance(values.get("logging", {}), dict) else {}
        merged: dict[str, Any] = {**values, **logging_values}
        providers = CredentialStore(workspace / "credentials.toml").read()

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
        provider_name = get("provider", None)
        if not provider_name:
            raise ConfigurationError("PIVOT_PROVIDER or config.toml provider is required")
        provider = providers.get(provider_name)
        if provider is None:
            raise ConfigurationError(f"Provider {provider_name!r} is not defined in credentials.toml")
        config = cls(
            workspace_path=workspace,
            provider=provider,
            max_rounds=get("max_rounds", 8, int),
            llm_timeout=get("llm_timeout", 120.0, float),
            capability_timeout=get("capability_timeout", 15.0, float),
            event_poll_interval=get("event_poll_interval", 1.0, float),
            event_max_wait=get("event_max_wait", 3600.0, float),
            dependency_install_timeout=get("dependency_install_timeout", 300.0, float),
            dependency_start_timeout=get("dependency_start_timeout", 15.0, float),
            dependency_dbus_timeout=get("dependency_dbus_timeout", 1.0, float),
            dependency_stop_timeout=get("dependency_stop_timeout", 5.0, float),
            log_console_level=_validate_log_level(console_level, setting="display log level"),
            log_file_level=_validate_log_level(file_level, setting="storage log level"),
        )
        if (
            config.max_rounds < 1
            or config.llm_timeout <= 0
            or config.capability_timeout <= 0
            or config.event_poll_interval <= 0
            or config.event_max_wait <= 0
            or config.dependency_install_timeout <= 0
            or config.dependency_start_timeout <= 0
            or config.dependency_dbus_timeout <= 0
            or config.dependency_stop_timeout <= 0
        ):
            raise ConfigurationError("max_rounds and timeouts must be greater than zero")
        config.ensure_workspace()
        configure_logging(
            config.log_console_level,
            file_level=config.log_file_level,
            log_path=config.workspace_path / "logs" / "pivot.log",
        )
        LOGGER.info("Workspace initialized path=%s", config.workspace_path)
        LOGGER.debug(
            "Configuration loaded model=%s provider=%s api_base_configured=%s max_rounds=%d llm_timeout=%g",
            config.provider.model,
            config.provider.name,
            config.provider.api_base is not None,
            config.max_rounds,
            config.llm_timeout,
        )
        return config

    def ensure_workspace(self) -> None:
        """Create the documented workspace directories without overwriting user files."""

        _ensure_workspace(self.workspace_path)


def _ensure_workspace(workspace_path: Path) -> None:
    """Bootstrap workspace files before provider validation can fail."""

    for relative in (
            "capabilities/think",
            "capabilities/measure",
            "capabilities/work",
            "events",
            "dependencies",
            "memory",
            "logs",
            "environment/measure",
            "environment/think",
            "environment/work",
            "environment/event",
        ):
        (workspace_path / relative).mkdir(parents=True, exist_ok=True)
    for kind in ("think", "measure", "work", "event"):
        project = workspace_path / "environment" / kind / "pyproject.toml"
        if not project.exists():
            project.write_text(
                f'[project]\nname = "pivot-{kind}-environment"\nversion = "0.1.0"\nrequires-python = ">=3.11"\ndependencies = []\n',
                encoding="utf-8",
            )
    config_file = workspace_path / "config.toml"
    if not config_file.exists():
        config_file.write_text("# pivot workspace configuration\n# provider = \"provider-name\"\n", encoding="utf-8")
