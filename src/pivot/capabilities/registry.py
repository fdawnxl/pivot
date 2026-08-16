"""Capability metadata registry and isolated script adapters."""

from __future__ import annotations

import inspect
import json
import logging
import os
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..models import CapabilityDescriptor, CapabilityKind, ToolCall

LOGGER = logging.getLogger(__name__)
THINK_READER_NAME = "pivot_read_think"
_MAX_OUTPUT_BYTES = 1024 * 1024


class CapabilityError(RuntimeError):
    """Raised when a capability cannot be registered or executed safely."""


class CapabilityScriptRunner:
    """Run an untrusted capability script in a dedicated uv project process."""

    def __init__(
        self,
        environment: str | Path,
        *,
        instance: str | Path | None = None,
        timeout: float = 15.0,
        uv_binary: str = "uv",
        max_output_bytes: int = _MAX_OUTPUT_BYTES,
    ) -> None:
        self.environment = Path(environment).expanduser().resolve()
        self.instance = (
            Path(instance).expanduser().resolve()
            if instance
            else self.environment.parent.parent
        )
        self.timeout = timeout
        self.uv_binary = uv_binary
        self.max_output_bytes = max_output_bytes
        if timeout <= 0 or max_output_bytes < 1:
            raise ValueError("timeout and max_output_bytes must be positive")

    def _run(
        self,
        script: str | Path,
        arguments: list[str],
        *,
        input_value: object | None = None,
    ) -> Any:
        script_path = Path(script).expanduser().resolve()
        if not script_path.is_file():
            raise CapabilityError(f"Capability script does not exist: {script_path}")
        command = [
            self.uv_binary,
            "run",
            "--project",
            str(self.environment),
            "python",
            str(script_path),
            *arguments,
        ]
        operation = arguments[0]
        LOGGER.info(
            "Capability process started script=%s operation=%s",
            script_path.name,
            operation,
        )
        environment = {
            key: value
            for key, value in os.environ.items()
            if key
            in {
                "PATH",
                "HOME",
                "LANG",
                "LC_ALL",
                "TMPDIR",
                "UV_CACHE_DIR",
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
                "DBUS_SESSION_BUS_ADDRESS",
                "DBUS_SYSTEM_BUS_ADDRESS",
            }
        }
        environment["PIVOT_INSTANCE_PATH"] = str(self.instance)
        try:
            result = subprocess.run(
                command,
                input=json.dumps(input_value, ensure_ascii=False)
                if input_value is not None
                else None,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
                cwd=self.instance,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            LOGGER.error(
                "Capability process timed out script=%s timeout=%g",
                script_path.name,
                self.timeout,
            )
            raise CapabilityError(
                f"Capability timed out after {self.timeout:g} seconds"
            ) from exc
        except OSError as exc:
            LOGGER.error(
                "Capability process could not start script=%s error_type=%s",
                script_path.name,
                type(exc).__name__,
            )
            raise CapabilityError(
                f"Unable to start capability: {type(exc).__name__}"
            ) from exc
        stderr = result.stderr.strip()[-500:]
        if result.returncode != 0:
            LOGGER.error(
                "Capability process failed script=%s return_code=%d stderr=%s",
                script_path.name,
                result.returncode,
                stderr or "no error detail",
            )
            raise CapabilityError(
                f"Capability failed with code {result.returncode}: {stderr or 'no error detail'}"
            )
        encoded = result.stdout.encode("utf-8")
        if len(encoded) > self.max_output_bytes:
            raise CapabilityError(
                f"Capability output exceeds {self.max_output_bytes} bytes"
            )
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise CapabilityError(
                f"Capability returned invalid JSON: {exc.msg}"
            ) from exc
        LOGGER.info(
            "Capability process completed script=%s operation=%s",
            script_path.name,
            operation,
        )
        return value

    def describe(
        self, script: str | Path, kind: CapabilityKind
    ) -> CapabilityDescriptor:
        value = self._run(script, ["-l"])
        if not isinstance(value, Mapping):
            raise CapabilityError("Capability -l response must be a JSON object")
        try:
            parameters = value.get("parameters", {})
            if not isinstance(parameters, Mapping):
                raise TypeError("parameters must be an object")
            return CapabilityDescriptor(
                name=str(value["name"]),
                kind=kind,
                description=str(value["description"]),
                parameters=dict(parameters),
                source=str(Path(script).expanduser().resolve()),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CapabilityError(
                "Capability descriptor is missing required fields"
            ) from exc

    def read_think(self, script: str | Path) -> str:
        value = self._run(script, ["-r"])
        if not isinstance(value, str) or not value.strip():
            raise CapabilityError(
                "Think capability -r response must be a non-empty JSON string"
            )
        return value

    def read_measure(self, script: str | Path, feature: str) -> Any:
        if not feature or feature.startswith("-"):
            raise CapabilityError("Measure feature must be a non-option name")
        return self._run(script, ["-r", feature])

    def execute_work(self, script: str | Path, arguments: Mapping[str, Any]) -> Any:
        return self._run(script, ["-x"], input_value=dict(arguments))


class MeasureRunner(CapabilityScriptRunner):
    """Backward-compatible measure runner using the isolated script protocol."""

    def list_features(self, script: str | Path) -> Any:
        return self._run(script, ["-l"])

    def read(self, script: str | Path, feature: str) -> Any:
        return self.read_measure(script, feature)


class CapabilityRegistry:
    """Own capability metadata and dispatch executable calls by stable name."""

    def __init__(self) -> None:
        self._descriptors: dict[str, CapabilityDescriptor] = {}
        self._handlers: dict[str, Callable[..., Any]] = {}
        self._think_readers: dict[str, Callable[[], str]] = {}

    def register(
        self,
        descriptor: CapabilityDescriptor,
        handler: Callable[..., Any] | None = None,
    ) -> None:
        if descriptor.name in self._descriptors or descriptor.name == THINK_READER_NAME:
            raise CapabilityError(
                f"Capability already registered or reserved: {descriptor.name}"
            )
        if descriptor.kind in ("work", "measure") and handler is None:
            raise CapabilityError(
                f"Executable capability requires a handler: {descriptor.name}"
            )
        if descriptor.kind == "think" and handler is not None:
            raise CapabilityError(
                "Think capabilities provide context and cannot have handlers"
            )
        self._descriptors[descriptor.name] = descriptor
        if handler is not None:
            self._handlers[descriptor.name] = handler
        LOGGER.debug(
            "Capability registered name=%s kind=%s source=%s",
            descriptor.name,
            descriptor.kind,
            descriptor.source or "built-in",
        )

    def register_think(
        self, descriptor: CapabilityDescriptor, reader: Callable[[], str]
    ) -> None:
        if descriptor.kind != "think":
            raise CapabilityError("Lazy think reader requires a think descriptor")
        self.register(descriptor)
        self._think_readers[descriptor.name] = reader

    def descriptors(
        self, kind: CapabilityKind | None = None
    ) -> tuple[CapabilityDescriptor, ...]:
        items = self._descriptors.values()
        return tuple(
            sorted(
                (item for item in items if kind is None or item.kind == kind),
                key=lambda item: item.name,
            )
        )

    def llm_tools(self) -> tuple[dict[str, Any], ...]:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": item.name,
                    "description": item.description,
                    "parameters": item.parameters
                    or {"type": "object", "properties": {}},
                },
            }
            for item in self.descriptors()
            if item.kind in ("measure", "work")
        ]
        if self._think_readers:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": THINK_READER_NAME,
                        "description": "Read the full text of one optional think capability before applying it.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "name": {
                                    "type": "string",
                                    "enum": sorted(self._think_readers),
                                }
                            },
                            "required": ["name"],
                            "additionalProperties": False,
                        },
                    },
                }
            )
        return tuple(tools)

    def prompt_context(self) -> list[dict[str, Any]]:
        return [
            {
                **item.as_prompt_dict(),
                **({"lazy": True} if item.kind == "think" else {}),
            }
            for item in self.descriptors()
        ]

    def scoped(self, names: list[str] | tuple[str, ...]) -> "CapabilityRegistry":
        """Return an independent registry exposing only explicitly assigned capabilities."""

        requested = tuple(dict.fromkeys(names))
        unknown = sorted(set(requested) - self._descriptors.keys())
        if unknown:
            raise CapabilityError(
                f"Unknown assigned capabilities: {', '.join(unknown)}"
            )
        scoped = CapabilityRegistry()
        for name in requested:
            descriptor = self._descriptors[name]
            if descriptor.kind == "think":
                reader = self._think_readers.get(name)
                if reader is None:
                    raise CapabilityError(f"Think capability has no reader: {name}")
                scoped.register_think(descriptor, reader)
            else:
                scoped.register(descriptor, self._handlers[name])
        return scoped

    def execute(self, call: ToolCall) -> Any:
        if call.name == THINK_READER_NAME:
            return self._read_think(call.arguments)
        descriptor = self._descriptors.get(call.name)
        handler = self._handlers.get(call.name)
        if descriptor is None or handler is None:
            raise CapabilityError(f"Unknown or non-executable capability: {call.name}")
        try:
            inspect.signature(handler).bind(**call.arguments)
        except TypeError as exc:
            raise CapabilityError(
                f"Invalid arguments for capability {call.name}: {exc}"
            ) from exc
        LOGGER.info(
            "Executing %s capability '%s'",
            descriptor.kind,
            descriptor.name,
            extra={"capability": descriptor.name},
        )
        try:
            result = handler(**call.arguments)
            json.dumps(result, ensure_ascii=False)
        except CapabilityError:
            LOGGER.warning(
                "Capability failed name=%s kind=%s", descriptor.name, descriptor.kind
            )
            raise
        except (TypeError, ValueError) as exc:
            raise CapabilityError(
                f"Capability {call.name} returned a non-JSON result"
            ) from exc
        except Exception as exc:
            LOGGER.error(
                "Capability raised name=%s kind=%s error_type=%s",
                descriptor.name,
                descriptor.kind,
                type(exc).__name__,
            )
            raise CapabilityError(
                f"Capability {call.name} failed: {type(exc).__name__}: {exc}"
            ) from exc
        LOGGER.info(
            "Capability completed name=%s kind=%s",
            descriptor.name,
            descriptor.kind,
            extra={"capability": descriptor.name},
        )
        return result

    def _read_think(self, arguments: Mapping[str, Any]) -> dict[str, str]:
        name = arguments.get("name")
        if not isinstance(name, str) or name not in self._think_readers:
            raise CapabilityError(f"Unknown think capability: {name!r}")
        try:
            content = self._think_readers[name]()
        except CapabilityError:
            raise
        except Exception as exc:
            raise CapabilityError(
                f"Unable to read think capability {name}: {type(exc).__name__}"
            ) from exc
        return {"name": name, "content": content}


__all__ = [
    "CapabilityError",
    "CapabilityRegistry",
    "CapabilityScriptRunner",
    "MeasureRunner",
    "THINK_READER_NAME",
]
