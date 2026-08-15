"""Execution backends used by agents after deciding how work should be done."""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

LOGGER = logging.getLogger(__name__)
SHELL_EXECUTOR_TOOL = "pivot_execute_shell"
_ENVIRONMENT_KEYS = {
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "DBUS_SYSTEM_BUS_ADDRESS",
}


class ExecutorError(RuntimeError):
    """Raised when an executor request is invalid or cannot be completed."""


@dataclass(frozen=True, slots=True)
class ExecutorDescriptor:
    """Model-facing metadata for one execution backend."""

    name: str
    description: str
    parameters: dict[str, Any]
    tool_name: str | None = None

    def as_prompt_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


class Executor(Protocol):
    descriptor: ExecutorDescriptor

    def execute(self, arguments: Mapping[str, Any]) -> Any:
        """Execute one validated request and return JSON-serializable output."""


class ShellExecutor:
    """Run a shell command with a fixed cwd, timeout, environment, and output cap."""

    descriptor = ExecutorDescriptor(
        "shell",
        "Execute a shell command inside the pivot instance directory.",
        {
            "type": "object",
            "properties": {
                "command": {"type": "string", "minLength": 1},
                "timeout": {"type": "number", "exclusiveMinimum": 0},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        tool_name=SHELL_EXECUTOR_TOOL,
    )

    def __init__(
        self,
        instance: str | Path,
        *,
        timeout: float = 30.0,
        max_output_bytes: int = 1024 * 1024,
        shell: str = "/bin/sh",
    ) -> None:
        if timeout <= 0 or max_output_bytes < 1:
            raise ValueError("executor timeout and max_output_bytes must be positive")
        self.instance = Path(instance).expanduser().resolve()
        self.timeout = timeout
        self.max_output_bytes = max_output_bytes
        self.shell = shell

    def execute(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if set(arguments) - {"command", "timeout"}:
            raise ExecutorError("Shell executor accepts only command and timeout")
        command = arguments.get("command")
        requested_timeout = arguments.get("timeout", self.timeout)
        if not isinstance(command, str) or not command.strip() or "\x00" in command:
            raise ExecutorError("Shell command must be a non-empty string")
        if not isinstance(requested_timeout, (int, float)) or isinstance(requested_timeout, bool):
            raise ExecutorError("Shell timeout must be a number")
        if requested_timeout <= 0 or requested_timeout > self.timeout:
            raise ExecutorError(f"Shell timeout must be between 0 and {self.timeout:g} seconds")
        environment = {key: value for key, value in os.environ.items() if key in _ENVIRONMENT_KEYS}
        environment["PIVOT_INSTANCE_PATH"] = str(self.instance)
        LOGGER.info("Shell executor started timeout=%g cwd=%s", requested_timeout, self.instance)
        try:
            result = subprocess.run(
                [self.shell, "-c", command],
                capture_output=True,
                cwd=self.instance,
                env=environment,
                timeout=float(requested_timeout),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            LOGGER.warning("Shell executor timed out timeout=%g", requested_timeout)
            raise ExecutorError(f"Shell command timed out after {requested_timeout:g} seconds") from exc
        except OSError as exc:
            LOGGER.error("Shell executor could not start error_type=%s", type(exc).__name__)
            raise ExecutorError(f"Unable to start shell executor: {type(exc).__name__}") from exc
        stdout = result.stdout[-self.max_output_bytes :].decode("utf-8", errors="replace")
        stderr = result.stderr[-self.max_output_bytes :].decode("utf-8", errors="replace")
        truncated = len(result.stdout) > self.max_output_bytes or len(result.stderr) > self.max_output_bytes
        LOGGER.info("Shell executor completed exit_code=%d truncated=%s", result.returncode, truncated)
        return {
            "exit_code": result.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "truncated": truncated,
        }


class ExecutorRegistry:
    """Register execution backends and route normalized executor actions."""

    def __init__(self) -> None:
        self._executors: dict[str, Executor] = {}

    def register(self, executor: Executor) -> None:
        descriptor = executor.descriptor
        if not descriptor.name or descriptor.name in self._executors:
            raise ExecutorError(f"Executor already registered or invalid: {descriptor.name!r}")
        self._executors[descriptor.name] = executor

    def descriptors(self) -> tuple[ExecutorDescriptor, ...]:
        return tuple(self._executors[name].descriptor for name in sorted(self._executors))

    def prompt_context(self) -> list[dict[str, Any]]:
        return [item.as_prompt_dict() for item in self.descriptors()]

    def tool_routes(self) -> dict[str, str]:
        return {
            descriptor.tool_name: descriptor.name
            for descriptor in self.descriptors()
            if descriptor.tool_name is not None
        }

    def llm_tools(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "type": "function",
                "function": {
                    "name": descriptor.tool_name,
                    "description": descriptor.description,
                    "parameters": descriptor.parameters,
                },
            }
            for descriptor in self.descriptors()
            if descriptor.tool_name is not None
        )

    def execute(self, name: str, arguments: Mapping[str, Any]) -> Any:
        executor = self._executors.get(name)
        if executor is None:
            raise ExecutorError(f"Unknown executor: {name}")
        return executor.execute(arguments)


__all__ = [
    "Executor",
    "ExecutorDescriptor",
    "ExecutorError",
    "ExecutorRegistry",
    "SHELL_EXECUTOR_TOOL",
    "ShellExecutor",
]
