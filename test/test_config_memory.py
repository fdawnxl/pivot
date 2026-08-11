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


def test_memory_uses_safe_session_names(tmp_path: Path) -> None:
    memory = TextMemory(tmp_path)
    memory.append("robot/one", "first")
    memory.append("robot/one", "second")
    assert memory.read("robot/one") == "first\nsecond"
    assert len(list(tmp_path.glob("*.txt"))) == 1
