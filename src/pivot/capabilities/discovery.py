"""Best-effort loading of workspace capability metadata."""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any

from ..models import CapabilityDescriptor

LOGGER = logging.getLogger(__name__)


def discover_think_capabilities(root: str | Path) -> tuple[CapabilityDescriptor, ...]:
    """Read ``DESCRIPTOR`` values from think capability Python files."""

    directory = Path(root).expanduser()
    if not directory.is_dir():
        return ()
    result: list[CapabilityDescriptor] = []
    for script in sorted(directory.glob("*.py")):
        try:
            spec = importlib.util.spec_from_file_location(f"pivot_workspace_think_{script.stem}", script)
            if spec is None or spec.loader is None:
                raise ImportError("module spec unavailable")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            descriptor: Any = getattr(module, "DESCRIPTOR", None)
            if isinstance(descriptor, CapabilityDescriptor) and descriptor.kind == "think":
                result.append(descriptor)
        except Exception as exc:
            LOGGER.warning("Unable to load think capability %s: %s", script, exc)
    return tuple(result)
