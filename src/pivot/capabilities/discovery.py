"""Isolated discovery of workspace capability scripts."""

from __future__ import annotations

import logging
from pathlib import Path

from ..models import CapabilityKind
from .registry import CapabilityError, CapabilityRegistry, CapabilityScriptRunner

LOGGER = logging.getLogger(__name__)


def register_workspace_capabilities(
    workspace: str | Path,
    registry: CapabilityRegistry,
    environments: str | Path,
    *,
    timeout: float = 15.0,
) -> None:
    """Discover and register scripts without importing workspace code."""

    root = Path(workspace).expanduser().resolve()
    environment_root = Path(environments).expanduser().resolve()
    loaded = 0
    for kind in ("think", "measure", "work"):
        capability_kind: CapabilityKind = kind
        runner = CapabilityScriptRunner(environment_root / kind, workspace=root, timeout=timeout)
        directory = root / "capabilities" / kind
        for script in sorted(directory.glob("*.py")):
            try:
                descriptor = runner.describe(script, capability_kind)
                if kind == "think":
                    registry.register_think(descriptor, _think_reader(runner, script))
                elif kind == "measure":
                    registry.register(descriptor, _measure_handler(runner, script))
                else:
                    registry.register(descriptor, _work_handler(runner, script))
                loaded += 1
            except CapabilityError as exc:
                LOGGER.warning("Unable to register %s capability %s: %s", kind, script, exc)
    LOGGER.info("Workspace capability discovery completed loaded=%d root=%s", loaded, root / "capabilities")


def _think_reader(runner: CapabilityScriptRunner, script: Path):
    def read() -> str:
        return runner.read_think(script)

    return read


def _measure_handler(runner: CapabilityScriptRunner, script: Path):
    def read(feature: str):
        return runner.read_measure(script, feature)

    return read


def _work_handler(runner: CapabilityScriptRunner, script: Path):
    def execute(**arguments):
        return runner.execute_work(script, arguments)

    return execute


__all__ = ["register_workspace_capabilities"]
