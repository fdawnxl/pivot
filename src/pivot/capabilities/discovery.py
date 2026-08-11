"""Best-effort loading of workspace capability metadata."""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any

from ..models import CapabilityDescriptor
from .registry import CapabilityError, CapabilityRegistry, MeasureRunner

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


def _load_module(script: Path, prefix: str) -> Any:
    spec = importlib.util.spec_from_file_location(f"{prefix}_{script.stem}", script)
    if spec is None or spec.loader is None:
        raise ImportError("module spec unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _descriptor(value: Any, kind: str, script: Path) -> CapabilityDescriptor:
    if isinstance(value, CapabilityDescriptor):
        descriptor = value
    elif isinstance(value, dict):
        descriptor = CapabilityDescriptor(
            name=str(value["name"]),
            kind=kind,  # type: ignore[arg-type]
            description=str(value.get("description", "")),
            parameters=dict(value.get("parameters", {})),
        )
    else:
        raise CapabilityError("DESCRIPTOR has the wrong type")
    if descriptor.kind != kind:
        raise CapabilityError("DESCRIPTOR has the wrong capability kind")
    return CapabilityDescriptor(descriptor.name, descriptor.kind, descriptor.description, descriptor.parameters, str(script))


def register_workspace_capabilities(workspace: str | Path, registry: CapabilityRegistry, measure_environment: str | Path) -> None:
    """Load valid think/work metadata and measure script adapters independently."""

    root = Path(workspace).expanduser()
    for kind in ("think", "work", "measure"):
        directory = root / "capabilities" / kind
        for script in sorted(directory.glob("*.py")):
            try:
                module = _load_module(script, f"pivot_workspace_{kind}")
                descriptor = _descriptor(getattr(module, "DESCRIPTOR", None), kind, script)
                if kind == "think":
                    registry.register(descriptor)
                elif kind == "work":
                    handler = getattr(module, "handle", None)
                    if not callable(handler):
                        raise CapabilityError("work script must expose callable handle")
                    registry.register(descriptor, handler)
                else:
                    runner = MeasureRunner(measure_environment)
                    registry.register(descriptor, lambda feature, _script=script, _runner=runner: _runner.read(_script, feature))
            except Exception as exc:
                LOGGER.warning("Unable to register %s capability %s: %s", kind, script, exc)
