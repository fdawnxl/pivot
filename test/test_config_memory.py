from pathlib import Path
from uuid import uuid4

import pytest

from pivot.config import ConfigurationError, PivotConfig
from pivot.credentials import CredentialStore, ProviderCredential
from pivot.memory import TextMemory


def test_workspace_bootstrap_and_environment_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "config.toml").parent.mkdir()
    (workspace / "config.toml").write_text('provider = "file-provider"\nmax_rounds = 3\n', encoding="utf-8")
    CredentialStore(workspace / "credentials.toml").save(
        {
            "file-provider": ProviderCredential("file-provider", "file-model"),
            "env-provider": ProviderCredential("env-provider", "env-model", "https://example.test/v1", "secret"),
        }
    )
    monkeypatch.setenv("PIVOT_PROVIDER", "env-provider")
    config = PivotConfig.load(workspace_path=workspace)
    assert config.provider.name == "env-provider"
    assert config.provider.model == "env-model"
    assert config.provider.api_key == "secret"
    assert config.max_rounds == 3
    assert (workspace / "capabilities/measure").is_dir()
    assert (workspace / "environment/measure/pyproject.toml").is_file()
    assert (workspace / "environment/think/pyproject.toml").is_file()
    assert (workspace / "environment/work/pyproject.toml").is_file()
    assert (workspace / "environment/event/pyproject.toml").is_file()
    assert (workspace / "logs/pivot.log").is_file()


def test_workspace_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PIVOT_WORKSPACE_PATH", raising=False)
    with pytest.raises(ConfigurationError):
        PivotConfig.load()


def test_codex_files_are_not_runtime_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    codex_home = tmp_path / "home" / ".codex"
    codex_home.mkdir(parents=True)
    (codex_home / "config.toml").write_text('model = "codex-only-model"\n', encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "config.toml").write_text('provider = "local"\n', encoding="utf-8")
    CredentialStore(workspace / "credentials.toml").save({"local": ProviderCredential("local", "local-model")})
    config = PivotConfig.load(workspace_path=workspace)
    assert config.provider.model == "local-model"


def test_memory_uses_uuid_session_directories(tmp_path: Path) -> None:
    memory = TextMemory(tmp_path)
    session_id = str(uuid4())
    memory.append(session_id, "first")
    memory.append(session_id, "second")
    assert memory.read(session_id) == "first\nsecond"
    assert (tmp_path / session_id / "history.jsonl").is_file()
    with pytest.raises(ValueError):
        memory.read("robot/one")


def test_workspace_credentials_are_restricted(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "config.toml").write_text('provider = "local"\n', encoding="utf-8")
    CredentialStore(workspace / "credentials.toml").save(
        {
            "local": ProviderCredential("local", "local-model", "https://example.invalid/v1", "test-secret"),
            "backup": ProviderCredential("backup", "backup-model"),
        }
    )
    config = PivotConfig.load(workspace_path=workspace)
    assert config.provider.model == "local-model"
    assert config.provider.api_base == "https://example.invalid/v1"
    assert config.provider.api_key == "test-secret"
    assert (workspace / "credentials.toml").stat().st_mode & 0o777 == 0o600
    assert "test-secret" not in (workspace / "config.toml").read_text(encoding="utf-8")


def test_selected_provider_must_exist(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "config.toml").write_text('provider = "missing"\n', encoding="utf-8")
    CredentialStore(workspace / "credentials.toml").save({"local": ProviderCredential("local", "model")})
    with pytest.raises(ConfigurationError, match="not defined"):
        PivotConfig.load(workspace_path=workspace)
