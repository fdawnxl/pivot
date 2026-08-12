from __future__ import annotations

from io import StringIO
import logging
from pathlib import Path

import pytest

from pivot.config import ConfigurationError, PivotConfig
from pivot.credentials import CredentialStore, ProviderCredential
from pivot.logging import configure_logging


def test_logging_uses_independent_console_and_file_levels(tmp_path: Path) -> None:
    console = StringIO()
    log_path = tmp_path / "pivot.log"
    configure_logging("error", file_level="debug", log_path=log_path, stream=console)
    logger = logging.getLogger("pivot.test")
    logger.debug("debug detail")
    logger.info("normal operation")
    logger.error("failure detail")
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert "failure detail" in console.getvalue()
    assert "normal operation" not in console.getvalue()
    persisted = log_path.read_text(encoding="utf-8")
    assert "debug detail" in persisted
    assert "normal operation" in persisted
    assert "failure detail" in persisted


def test_logging_level_environment_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "config.toml").write_text(
        'provider = "test"\n[logging]\nlog_console_level = "error"\nlog_file_level = "info"\n', encoding="utf-8"
    )
    CredentialStore(workspace / "credentials.toml").save({"test": ProviderCredential("test", "model")})
    monkeypatch.setenv("PIVOT_LOG_STORAGE_LEVEL", "debug")
    config = PivotConfig.load(workspace_path=workspace)
    assert config.log_console_level == "ERROR"
    assert config.log_file_level == "DEBUG"


def test_invalid_logging_level_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "config.toml").write_text('provider = "test"\n[logging]\ndisplay_level = "verbose"\n', encoding="utf-8")
    CredentialStore(workspace / "credentials.toml").save({"test": ProviderCredential("test", "model")})
    with pytest.raises(ConfigurationError):
        PivotConfig.load(workspace_path=workspace)
