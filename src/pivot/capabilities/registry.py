"""Capability metadata registry and isolated dispatch adapters."""

from __future__ import annotations

import inspect
import json
import logging
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..models import CapabilityDescriptor, CapabilityKind, ToolCall

LOGGER = logging.getLogger(__name__)


class CapabilityError(RuntimeError):
    """Raised when a capability cannot be registered or executed safely."""


class MeasureRunner:
    """Run measure scripts in their uv-managed project environment."""

    def __init__(self, environment: str | Path, *, timeout: float = 15.0, uv_binary: str = "uv") -> None:
        self.environment = Path(environment).expanduser().resolve()
        self.timeout = timeout
        self.uv_binary = uv_binary
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")

    def _run(self, script: str | Path, arguments: list[str]) -> Any:
        script_path = Path(script).expanduser().resolve()
        if not script_path.is_file():
            raise CapabilityError(f"Measure script does not exist: {script_path}")
        command = [self.uv_binary, "run", "--project", str(self.environment), "python", str(script_path), *arguments]
        LOGGER.info("Measure process started script=%s operation=%s", script_path.name, arguments[0])
        LOGGER.debug("Measure process command=%s timeout=%g", command, self.timeout)
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=self.timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            LOGGER.error("Measure process timed out script=%s timeout=%g", script_path.name, self.timeout)
            raise CapabilityError(f"Measure capability timed out after {self.timeout:g} seconds") from exc
        except OSError as exc:
            LOGGER.error("Measure process could not start script=%s error_type=%s", script_path.name, type(exc).__name__)
            raise CapabilityError(f"Unable to start measure capability: {exc}") from exc
        if result.returncode != 0:
            detail = result.stderr.strip()[-500:] or "no error detail"
            LOGGER.error("Measure process failed script=%s return_code=%d stderr=%s", script_path.name, result.returncode, detail)
            raise CapabilityError(f"Measure capability failed with code {result.returncode}: {detail}")
        output = result.stdout.strip()
        LOGGER.info("Measure process completed script=%s", script_path.name)
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            return output

    def list_features(self, script: str | Path) -> Any:
        return self._run(script, ["-l"])

    def read(self, script: str | Path, feature: str) -> Any:
        if not feature or feature.startswith("-"):
            raise CapabilityError("Measure feature must be a non-option name")
        return self._run(script, ["-r", feature])


class CapabilityRegistry:
    """Own capability metadata and dispatch work/measure calls by stable name."""

    def __init__(self) -> None:
        self._descriptors: dict[str, CapabilityDescriptor] = {}
        self._handlers: dict[str, Callable[..., Any]] = {}

    def register(self, descriptor: CapabilityDescriptor, handler: Callable[..., Any] | None = None) -> None:
        if descriptor.name in self._descriptors:
            raise CapabilityError(f"Capability already registered: {descriptor.name}")
        if descriptor.kind in ("work", "measure") and handler is None:
            raise CapabilityError(f"Executable capability requires a handler: {descriptor.name}")
        if descriptor.kind == "think" and handler is not None:
            raise CapabilityError("Think capabilities provide context and cannot have handlers")
        self._descriptors[descriptor.name] = descriptor
        if handler is not None:
            self._handlers[descriptor.name] = handler
        LOGGER.debug("Capability registered name=%s kind=%s source=%s", descriptor.name, descriptor.kind, descriptor.source or "built-in")

    def descriptors(self, kind: CapabilityKind | None = None) -> tuple[CapabilityDescriptor, ...]:
        items = self._descriptors.values()
        return tuple(sorted((item for item in items if kind is None or item.kind == kind), key=lambda item: item.name))

    def llm_tools(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "type": "function",
                "function": {"name": item.name, "description": item.description, "parameters": item.parameters or {"type": "object", "properties": {}}},
            }
            for item in self.descriptors()
            if item.kind in ("measure", "work")
        )

    def prompt_context(self) -> list[dict[str, Any]]:
        return [item.as_prompt_dict() for item in self.descriptors()]

    def execute(self, call: ToolCall) -> Any:
        descriptor = self._descriptors.get(call.name)
        handler = self._handlers.get(call.name)
        if descriptor is None or handler is None:
            raise CapabilityError(f"Unknown or non-executable capability: {call.name}")
        try:
            signature = inspect.signature(handler)
            signature.bind(**call.arguments)
        except TypeError as exc:
            raise CapabilityError(f"Invalid arguments for capability {call.name}: {exc}") from exc
        LOGGER.info("Executing %s capability '%s'", descriptor.kind, descriptor.name)
        try:
            result = handler(**call.arguments)
        except CapabilityError:
            LOGGER.warning("Capability failed name=%s kind=%s", descriptor.name, descriptor.kind)
            raise
        except Exception as exc:
            LOGGER.error("Capability raised name=%s kind=%s error_type=%s", descriptor.name, descriptor.kind, type(exc).__name__)
            raise CapabilityError(f"Capability {call.name} failed: {type(exc).__name__}: {exc}") from exc
        LOGGER.info("Capability completed name=%s kind=%s", descriptor.name, descriptor.kind)
        return result
