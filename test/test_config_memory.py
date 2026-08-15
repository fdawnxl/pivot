from pathlib import Path

import pytest

from pivot.config import ConfigurationError, PivotConfig
from pivot.credentials import CredentialStore, ProviderCredential
from pivot.memory import MemoryStore


def test_instance_bootstrap_and_environment_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = tmp_path / "instance"
    (instance / "config.toml").parent.mkdir()
    (instance / "config.toml").write_text('provider = "file-provider"\nmax_rounds = 3\n', encoding="utf-8")
    CredentialStore(instance / "credentials.toml").save(
        {
            "file-provider": ProviderCredential("file-provider", "file-model"),
            "env-provider": ProviderCredential("env-provider", "env-model", "https://example.test/v1", "secret"),
        }
    )
    monkeypatch.setenv("PIVOT_PROVIDER", "env-provider")
    config = PivotConfig.load(instance_path=instance)
    assert config.provider.name == "env-provider"
    assert config.provider.model == "env-model"
    assert config.provider.api_key == "secret"
    assert config.max_rounds == 3
    assert (instance / "capabilities/measure").is_dir()
    assert (instance / "environment/measure/pyproject.toml").is_file()
    assert (instance / "environment/think/pyproject.toml").is_file()
    assert (instance / "environment/work/pyproject.toml").is_file()
    assert (instance / "environment/event/pyproject.toml").is_file()
    assert (instance / "dependencies").is_dir()
    assert (instance / "logs/pivot.log").is_file()


def test_instance_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PIVOT_INSTANCE_PATH", raising=False)
    with pytest.raises(ConfigurationError):
        PivotConfig.load()


def test_codex_files_are_not_runtime_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    codex_home = tmp_path / "home" / ".codex"
    codex_home.mkdir(parents=True)
    (codex_home / "config.toml").write_text('model = "codex-only-model"\n', encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    instance = tmp_path / "instance"
    instance.mkdir()
    (instance / "config.toml").write_text('provider = "local"\n', encoding="utf-8")
    CredentialStore(instance / "credentials.toml").save({"local": ProviderCredential("local", "local-model")})
    config = PivotConfig.load(instance_path=instance)
    assert config.provider.model == "local-model"


def test_memory_uses_one_sqlite_database_and_stable_main_identity(tmp_path: Path) -> None:
    first = MemoryStore(tmp_path)
    agent_id = first.main_agent_id()
    first.close()

    second = MemoryStore(tmp_path)
    assert second.main_agent_id() == agent_id
    assert (tmp_path / "pivot.db").is_file()
    second.close()


def test_instance_credentials_are_restricted(tmp_path: Path) -> None:
    instance = tmp_path / "instance"
    instance.mkdir()
    (instance / "config.toml").write_text('provider = "local"\n', encoding="utf-8")
    CredentialStore(instance / "credentials.toml").save(
        {
            "local": ProviderCredential("local", "local-model", "https://example.invalid/v1", "test-secret"),
            "backup": ProviderCredential("backup", "backup-model"),
        }
    )
    config = PivotConfig.load(instance_path=instance)
    assert config.provider.model == "local-model"
    assert config.provider.api_base == "https://example.invalid/v1"
    assert config.provider.api_key == "test-secret"
    assert (instance / "credentials.toml").stat().st_mode & 0o777 == 0o600
    assert "test-secret" not in (instance / "config.toml").read_text(encoding="utf-8")


def test_selected_provider_must_exist(tmp_path: Path) -> None:
    instance = tmp_path / "instance"
    instance.mkdir()
    (instance / "config.toml").write_text('provider = "missing"\n', encoding="utf-8")
    CredentialStore(instance / "credentials.toml").save({"local": ProviderCredential("local", "model")})
    with pytest.raises(ConfigurationError, match="not defined"):
        PivotConfig.load(instance_path=instance)


def test_missing_provider_still_bootstraps_empty_instance(tmp_path: Path) -> None:
    instance = tmp_path / "new-instance"
    with pytest.raises(ConfigurationError, match="provider is required"):
        PivotConfig.load(instance_path=instance)
    assert (instance / "config.toml").is_file()
    assert (instance / "environment/work/pyproject.toml").is_file()


def test_dbus_control_configuration_uses_environment_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = tmp_path / "instance"
    instance.mkdir()
    (instance / "config.toml").write_text(
        'provider = "local"\n'
        'dbus_control_enabled = false\n'
        'dbus_control_bus = "system"\n'
        'dbus_control_service = "org.pivot.FromFile"\n',
        encoding="utf-8",
    )
    CredentialStore(instance / "credentials.toml").save({"local": ProviderCredential("local", "model")})
    monkeypatch.setenv("PIVOT_DBUS_CONTROL_ENABLED", "true")
    monkeypatch.setenv("PIVOT_DBUS_CONTROL_BUS", "session")
    monkeypatch.setenv("PIVOT_DBUS_CONTROL_SERVICE", "org.pivot.FromEnvironment")

    config = PivotConfig.load(instance_path=instance)

    assert config.dbus_control_enabled
    assert config.dbus_control_bus == "session"
    assert config.dbus_control_service == "org.pivot.FromEnvironment"
