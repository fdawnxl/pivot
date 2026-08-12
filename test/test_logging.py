from __future__ import annotations

from io import StringIO
import json
import logging
from pathlib import Path

import pytest

from pivot.config import ConfigurationError, PivotConfig
from pivot.credentials import CredentialStore, ProviderCredential
from pivot.logging import configure_logging, log_context


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
    persisted = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert [item["message"] for item in persisted[-3:]] == ["debug detail", "normal operation", "failure detail"]
    assert persisted[-1]["level"] == "ERROR"


def test_structured_logging_includes_correlation_context(tmp_path: Path) -> None:
    log_path = tmp_path / "pivot.log"
    configure_logging("error", file_level="info", log_path=log_path, stream=StringIO())
    with log_context(correlation_id="request-1", session_id="session-1"):
        logging.getLogger("pivot.test").info("correlated operation", extra={"capability": "cpu_read"})
    for handler in logging.getLogger().handlers:
        handler.flush()
    record = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
    assert record["correlation_id"] == "request-1"
    assert record["session_id"] == "session-1"
    assert record["capability"] == "cpu_read"


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
