"""Isolated discovery of instance capability scripts."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..models import CapabilityKind
from .registry import CapabilityError, CapabilityRegistry, CapabilityScriptRunner

LOGGER = logging.getLogger(__name__)


def register_instance_capabilities(
    instance: str | Path,
    registry: CapabilityRegistry,
    environments: str | Path,
    *,
    timeout: float = 15.0,
) -> None:
    """Discover and register scripts without importing instance code."""

    root = Path(instance).expanduser().resolve()
    environment_root = Path(environments).expanduser().resolve()
    loaded = 0
    for kind in ("think", "measure", "work"):
        capability_kind: CapabilityKind = kind
        runner = CapabilityScriptRunner(
            environment_root / kind, instance=root, timeout=timeout
        )
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
                LOGGER.warning(
                    "Unable to register %s capability %s: %s", kind, script, exc
                )
    LOGGER.info(
        "Instance capability discovery completed loaded=%d root=%s",
        loaded,
        root / "capabilities",
    )


def _think_reader(runner: CapabilityScriptRunner, script: Path) -> Callable[[], str]:
    def read() -> str:
        return runner.read_think(script)

    return read


def _measure_handler(
    runner: CapabilityScriptRunner, script: Path
) -> Callable[[str], Any]:
    def read(feature: str) -> Any:
        return runner.read_measure(script, feature)

    return read


def _work_handler(runner: CapabilityScriptRunner, script: Path) -> Callable[..., Any]:
    def execute(**arguments: Any) -> Any:
        return runner.execute_work(script, arguments)

    return execute


__all__ = ["register_instance_capabilities"]
