from pathlib import Path

import pytest

from pivot.config import ConfigurationError, PivotConfig
from pivot.memory import TextMemory


def test_workspace_bootstrap_and_environment_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "config.toml").parent.mkdir()
    (workspace / "config.toml").write_text('[pivot]\nmodel = "file-model"\nmax_rounds = 3\n', encoding="utf-8")
    monkeypatch.setenv("PIVOT_MODEL", "env-model")
    config = PivotConfig.load(workspace_path=workspace)
    assert config.model == "env-model"
    assert config.max_rounds == 3
    assert (workspace / "capabilities/measure").is_dir()


def test_workspace_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PIVOT_WORKSPACE_PATH", raising=False)
    with pytest.raises(ConfigurationError):
        PivotConfig.load()


def test_codex_files_are_not_runtime_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    codex_home = tmp_path / "home" / ".codex"
    codex_home.mkdir(parents=True)
    (codex_home / "config.toml").write_text('model = "codex-only-model"\n', encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = PivotConfig.load(workspace_path=tmp_path / "workspace")
    assert config.model == "gpt-4o-mini"


def test_memory_uses_safe_session_names(tmp_path: Path) -> None:
    memory = TextMemory(tmp_path)
    memory.append("robot/one", "first")
    memory.append("robot/one", "second")
    assert memory.read("robot/one") == "first\nsecond"
    assert len(list(tmp_path.glob("*.txt"))) == 1


def test_workspace_credentials_are_restricted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pivot.credentials import CredentialStore

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "config.toml").write_text(
        '[llm]\nmodel = "local-model"\napi_base = "https://example.invalid/v1"\n', encoding="utf-8"
    )
    CredentialStore(workspace / "credentials.json").save({"api_key": "test-secret"})
    config = PivotConfig.load(workspace_path=workspace)
    assert config.model == "local-model"
    assert config.api_base == "https://example.invalid/v1"
    assert config.api_key == "test-secret"
    assert (workspace / "credentials.json").stat().st_mode & 0o777 == 0o600
    assert "test-secret" not in (workspace / "config.toml").read_text(encoding="utf-8")
