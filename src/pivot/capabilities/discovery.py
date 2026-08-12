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
                    registry.register_think(descriptor, lambda _script=script, _runner=runner: _runner.read_think(_script))
                elif kind == "measure":
                    registry.register(
                        descriptor,
                        lambda feature, _script=script, _runner=runner: _runner.read_measure(_script, feature),
                    )
                else:
                    registry.register(
                        descriptor,
                        lambda _script=script, _runner=runner, **arguments: _runner.execute_work(_script, arguments),
                    )
                loaded += 1
            except CapabilityError as exc:
                LOGGER.warning("Unable to register %s capability %s: %s", kind, script, exc)
    LOGGER.info("Workspace capability discovery completed loaded=%d root=%s", loaded, root / "capabilities")


__all__ = ["register_workspace_capabilities"]
