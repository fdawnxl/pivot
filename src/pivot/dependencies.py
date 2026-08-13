"""Discovery and lifecycle management for isolated external dependencies."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import tomllib
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol

LOGGER = logging.getLogger(__name__)

DEPENDENCY_MANIFEST = "dependency.toml"
DEPENDENCY_READY_MARKER = ".pivot-installed"
DEPENDENCY_DBUS_PATH = "/org/pivot/Dependency"
DEPENDENCY_DBUS_INTERFACE = "org.pivot.Dependency1"
_DEPENDENCY_ID = re.compile(r"^[a-z][a-z0-9-]*$")
_DBUS_SERVICE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)+$")
_ENVIRONMENT_KEYS = {
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


class DependencyError(RuntimeError):
    """Raised when an external dependency cannot be loaded or managed."""


class DependencyState(StrEnum):
    """Lifecycle state maintained by the dependency manager."""

    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class DependencyDBus:
    """D-Bus transport address for one dependency service."""

    bus: Literal["session", "system"]
    service: str


@dataclass(frozen=True, slots=True)
class DependencyDescriptor:
    """Validated metadata for one instance dependency project."""

    dependency_id: str
    root: Path
    command: tuple[str, ...]
    dbus: DependencyDBus
    description: str = ""


@dataclass(frozen=True, slots=True)
class DependencyStatus:
    """Status reported by the dependency's common D-Bus interface."""

    dependency_id: str
    state: DependencyState
    message: str = ""
    details: Mapping[str, Any] | None = None

    @classmethod
    def from_payload(cls, dependency_id: str, payload: Mapping[str, Any]) -> "DependencyStatus":
        reported_id = payload.get("id")
        state = payload.get("state")
        if reported_id != dependency_id:
            raise DependencyError(f"Dependency {dependency_id!r} reported a different id")
        if state not in {
            DependencyState.STARTING,
            DependencyState.READY,
            DependencyState.DEGRADED,
            DependencyState.STOPPING,
            DependencyState.ERROR,
        }:
            raise DependencyError(f"Dependency {dependency_id!r} reported an invalid state")
        message = payload.get("message", "")
        details = payload.get("details")
        if not isinstance(message, str) or (details is not None and not isinstance(details, Mapping)):
            raise DependencyError(f"Dependency {dependency_id!r} reported an invalid status payload")
        return cls(dependency_id, DependencyState(state), message, dict(details) if details is not None else None)


class DependencyStatusClient(Protocol):
    """Transport boundary used by the manager to inspect dependencies."""

    def query(self, descriptor: DependencyDescriptor, *, timeout: float) -> DependencyStatus:
        """Read and validate one dependency status."""

    def ping(self, descriptor: DependencyDescriptor, *, timeout: float) -> bool:
        """Return whether one dependency responds to the common heartbeat."""


class DBusDependencyStatusClient:
    """Query the common lifecycle interface at a dependency's D-Bus address."""

    def __init__(self, *, bus_addresses: Mapping[str, str] | None = None) -> None:
        self.bus_addresses = dict(bus_addresses or {})

    def query(self, descriptor: DependencyDescriptor, *, timeout: float) -> DependencyStatus:
        dependency_id = descriptor.dependency_id
        reply = self._call(descriptor, "GetStatus", timeout=timeout)
        if len(reply) != 1 or not isinstance(reply[0], str):
            raise DependencyError(f"Dependency {dependency_id!r} returned an invalid GetStatus response")
        try:
            payload = json.loads(reply[0])
        except json.JSONDecodeError as exc:
            raise DependencyError(f"Dependency {dependency_id!r} returned invalid status JSON") from exc
        if not isinstance(payload, Mapping):
            raise DependencyError(f"Dependency {dependency_id!r} status must be a JSON object")
        return DependencyStatus.from_payload(dependency_id, payload)

    def ping(self, descriptor: DependencyDescriptor, *, timeout: float) -> bool:
        try:
            reply = self._call(descriptor, "Ping", timeout=timeout)
        except DependencyError:
            return False
        return len(reply) == 1 and reply[0] in {descriptor.dependency_id, "pong"}

    def _call(self, descriptor: DependencyDescriptor, member: str, *, timeout: float) -> list[Any]:
        dependency_id = descriptor.dependency_id
        _validate_dependency_id(dependency_id)

        async def invoke() -> list[Any]:
            try:
                from dbus_next import BusType, Message, MessageType
                from dbus_next.aio import MessageBus
            except ImportError as exc:  # pragma: no cover - declared runtime dependency
                raise DependencyError("dbus-next is required for dependency status queries") from exc
            bus_type = BusType.SESSION if descriptor.dbus.bus == "session" else BusType.SYSTEM
            try:
                bus_address = self.bus_addresses.get(descriptor.dbus.bus)
                message_bus = MessageBus(bus_address=bus_address) if bus_address else MessageBus(bus_type=bus_type)
                connection = await message_bus.connect()
                try:
                    reply = await connection.call(
                        Message(
                            destination=descriptor.dbus.service,
                            path=DEPENDENCY_DBUS_PATH,
                            interface=DEPENDENCY_DBUS_INTERFACE,
                            member=member,
                        )
                    )
                finally:
                    connection.disconnect()
            except Exception as exc:
                raise DependencyError(
                    f"D-Bus {member} failed for dependency {dependency_id!r}: {type(exc).__name__}"
                ) from exc
            if reply.message_type == MessageType.ERROR:
                detail = str(reply.body[0]) if reply.body else reply.error_name or "D-Bus error"
                raise DependencyError(f"Dependency {dependency_id!r} rejected {member}: {detail}")
            return list(reply.body)

        try:
            return _run_async(asyncio.wait_for(invoke(), timeout=timeout))
        except DependencyError:
            raise
        except (TimeoutError, OSError) as exc:
            raise DependencyError(
                f"D-Bus {member} failed for dependency {dependency_id!r}: {type(exc).__name__}"
            ) from exc


def _run_async(awaitable: Any) -> Any:
    """Run a short D-Bus coroutine even when the caller owns an event loop."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    result: list[Any] = []
    error: list[BaseException] = []

    def run() -> None:
        try:
            result.append(asyncio.run(awaitable))
        except BaseException as exc:
            error.append(exc)

    thread = threading.Thread(target=run, name="pivot-dependency-dbus", daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result[0]


def _validate_dependency_id(dependency_id: str) -> None:
    if not isinstance(dependency_id, str) or not _DEPENDENCY_ID.fullmatch(dependency_id):
        raise DependencyError(f"Invalid dependency id: {dependency_id!r}")


def _load_dbus(value: object, *, dependency_id: str) -> DependencyDBus:
    if not isinstance(value, Mapping):
        raise DependencyError(f"Dependency {dependency_id!r} must define a dbus table")
    bus = value.get("bus")
    service = value.get("service")
    if bus not in {"session", "system"}:
        raise DependencyError(f"Dependency {dependency_id!r} dbus.bus must be 'session' or 'system'")
    if not isinstance(service, str) or not _DBUS_SERVICE.fullmatch(service):
        raise DependencyError(f"Dependency {dependency_id!r} has an invalid D-Bus service name")
    return DependencyDBus(bus, service)


def load_dependency_manifest(root: str | Path) -> DependencyDescriptor:
    """Load one dependency project manifest without executing project code."""

    project_root = Path(root).expanduser().resolve()
    manifest = project_root / DEPENDENCY_MANIFEST
    if not manifest.is_file():
        raise DependencyError(f"Dependency manifest is missing: {manifest}")
    if not (project_root / "pyproject.toml").is_file():
        raise DependencyError(f"Dependency project has no pyproject.toml: {project_root}")
    try:
        with manifest.open("rb") as handle:
            value: Any = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise DependencyError(f"Cannot load dependency manifest: {manifest}") from exc
    if not isinstance(value, Mapping):
        raise DependencyError(f"Dependency manifest must be a TOML table: {manifest}")
    dependency_id = value.get("id")
    command = value.get("command")
    description = value.get("description", "")
    _validate_dependency_id(dependency_id)
    dbus = _load_dbus(value.get("dbus"), dependency_id=dependency_id)
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(item, str) and item and "\x00" not in item for item in command)
    ):
        raise DependencyError(f"Dependency {dependency_id!r} command must be a non-empty string array")
    if not isinstance(description, str):
        raise DependencyError(f"Dependency {dependency_id!r} description must be a string")
    return DependencyDescriptor(dependency_id, project_root, tuple(command), dbus, description)


def discover_dependencies(root: str | Path) -> tuple[DependencyDescriptor, ...]:
    """Discover valid dependency projects from first-level instance directories."""

    dependencies_root = Path(root).expanduser().resolve()
    if not dependencies_root.is_dir():
        return ()
    discovered: list[DependencyDescriptor] = []
    try:
        candidates = sorted((item for item in dependencies_root.iterdir() if item.is_dir()), key=lambda item: item.name)
    except OSError as exc:
        LOGGER.warning("Unable to scan dependency root path=%s error_type=%s", dependencies_root, type(exc).__name__)
        return ()
    for project in candidates:
        try:
            project.resolve().relative_to(dependencies_root)
            discovered.append(load_dependency_manifest(project))
        except (DependencyError, ValueError) as exc:
            LOGGER.warning("Skipping instance dependency directory=%s error=%s", project.name, exc)
    counts = Counter(item.dependency_id for item in discovered)
    duplicates = sorted(dependency_id for dependency_id, count in counts.items() if count > 1)
    for dependency_id in duplicates:
        LOGGER.warning("Skipping duplicate instance dependency id=%s", dependency_id)
    return tuple(item for item in discovered if counts[item.dependency_id] == 1)


@dataclass(slots=True)
class _RunningDependency:
    descriptor: DependencyDescriptor
    process: subprocess.Popen[bytes]
    log_handle: Any


class DependencyManager:
    """Install, launch, inspect, and stop isolated instance dependencies."""

    def __init__(
        self,
        instance: str | Path,
        *,
        status_client: DependencyStatusClient | None = None,
        uv_binary: str = "uv",
        install_timeout: float = 300.0,
        start_timeout: float = 15.0,
        dbus_timeout: float = 1.0,
        stop_timeout: float = 5.0,
        poll_interval: float = 0.1,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        process_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if min(install_timeout, start_timeout, dbus_timeout, stop_timeout, poll_interval) <= 0:
            raise ValueError("Dependency timeouts and poll interval must be greater than zero")
        self.instance = Path(instance).expanduser().resolve()
        self.root = self.instance / "dependencies"
        self.status_client = status_client or DBusDependencyStatusClient()
        self.uv_binary = uv_binary
        self.install_timeout = install_timeout
        self.start_timeout = start_timeout
        self.dbus_timeout = dbus_timeout
        self.stop_timeout = stop_timeout
        self.poll_interval = poll_interval
        self.runner = runner
        self.process_factory = process_factory
        self.sleeper = sleeper
        self.clock = clock
        self._descriptors: dict[str, DependencyDescriptor] = {}
        self._running: dict[str, _RunningDependency] = {}
        self._statuses: dict[str, DependencyStatus] = {}
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()

    def scan(self) -> tuple[DependencyDescriptor, ...]:
        """Refresh the valid manifest set without starting project code."""

        descriptors = discover_dependencies(self.root)
        with self._lock:
            self._descriptors = {item.dependency_id: item for item in descriptors}
            for descriptor in descriptors:
                self._statuses.setdefault(
                    descriptor.dependency_id,
                    DependencyStatus(descriptor.dependency_id, DependencyState.STOPPED, "Not started"),
                )
            self._statuses = {
                dependency_id: status
                for dependency_id, status in self._statuses.items()
                if dependency_id in self._descriptors
            }
        LOGGER.info("Instance dependency discovery completed loaded=%d root=%s", len(descriptors), self.root)
        return descriptors

    def start_all(self) -> tuple[DependencyStatus, ...]:
        """Start every valid dependency while isolating individual failures."""

        statuses: list[DependencyStatus] = []
        for descriptor in self.scan():
            try:
                statuses.append(self.start(descriptor.dependency_id))
            except DependencyError as exc:
                self._set_status(descriptor.dependency_id, DependencyState.ERROR, str(exc))
                LOGGER.warning("Unable to start dependency id=%s error=%s", descriptor.dependency_id, exc)
        return tuple(statuses)

    def start(self, dependency_id: str) -> DependencyStatus:
        """Install a dependency once, launch it, and wait for D-Bus readiness."""

        with self._lifecycle_lock:
            try:
                return self._start_locked(dependency_id)
            except DependencyError as exc:
                with self._lock:
                    known = dependency_id in self._descriptors
                if known:
                    self._set_status(dependency_id, DependencyState.ERROR, str(exc))
                raise

    def _start_locked(self, dependency_id: str) -> DependencyStatus:
        _validate_dependency_id(dependency_id)
        with self._lock:
            running = self._running.get(dependency_id)
            if running is not None and running.process.poll() is None:
                return self.query_status(dependency_id)
            if running is not None:
                self._running.pop(dependency_id)
                running.log_handle.close()
            descriptor = self._descriptors.get(dependency_id)
        if descriptor is None:
            self.scan()
            descriptor = self._descriptors.get(dependency_id)
        if descriptor is None:
            raise DependencyError(f"Unknown instance dependency: {dependency_id}")
        self._set_status(dependency_id, DependencyState.STARTING, "Starting dependency")
        self._install_once(descriptor)
        log_path = self.instance / "logs" / "dependencies" / f"{dependency_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            log_handle = log_path.open("ab", buffering=0)
        except OSError as exc:
            raise DependencyError(f"Cannot open dependency log for {dependency_id!r}") from exc
        command = [
            self.uv_binary,
            "run",
            "--project",
            str(descriptor.root),
            "--no-sync",
            *descriptor.command,
        ]
        try:
            process = self.process_factory(
                command,
                cwd=descriptor.root,
                env=self._environment(dependency_id),
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            log_handle.close()
            raise DependencyError(f"Unable to launch dependency {dependency_id!r}: {type(exc).__name__}") from exc
        with self._lock:
            self._running[dependency_id] = _RunningDependency(descriptor, process, log_handle)
        LOGGER.info("Dependency process started id=%s pid=%s", dependency_id, process.pid)
        try:
            return self._wait_until_ready(dependency_id, process)
        except BaseException:
            self.stop(dependency_id)
            raise

    def _install_once(self, descriptor: DependencyDescriptor) -> None:
        marker = descriptor.root / DEPENDENCY_READY_MARKER
        if marker.is_file():
            return
        command = [self.uv_binary, "sync", "--project", str(descriptor.root)]
        LOGGER.info("Dependency installation started id=%s", descriptor.dependency_id)
        try:
            result = self.runner(
                command,
                capture_output=True,
                text=True,
                timeout=self.install_timeout,
                check=False,
                cwd=descriptor.root,
                env=self._environment(descriptor.dependency_id),
            )
        except subprocess.TimeoutExpired as exc:
            raise DependencyError(
                f"Dependency {descriptor.dependency_id!r} installation timed out after {self.install_timeout:g} seconds"
            ) from exc
        except OSError as exc:
            raise DependencyError(
                f"Unable to install dependency {descriptor.dependency_id!r}: {type(exc).__name__}"
            ) from exc
        if result.returncode != 0:
            detail = result.stderr.strip()[-500:] or "no error detail"
            raise DependencyError(
                f"Dependency {descriptor.dependency_id!r} installation failed with code {result.returncode}: {detail}"
            )
        _write_marker(marker, descriptor.dependency_id)
        LOGGER.info("Dependency installation completed id=%s", descriptor.dependency_id)

    def _environment(self, dependency_id: str) -> dict[str, str]:
        environment = {key: value for key, value in os.environ.items() if key in _ENVIRONMENT_KEYS}
        environment["PIVOT_INSTANCE_PATH"] = str(self.instance)
        environment["PIVOT_DEPENDENCY_ID"] = dependency_id
        descriptor = self._descriptors[dependency_id]
        environment["PIVOT_DEPENDENCY_DBUS_BUS"] = descriptor.dbus.bus
        environment["PIVOT_DEPENDENCY_DBUS_SERVICE"] = descriptor.dbus.service
        return environment

    def _wait_until_ready(
        self, dependency_id: str, process: subprocess.Popen[bytes]
    ) -> DependencyStatus:
        deadline = self.clock() + self.start_timeout
        last_error = "D-Bus status is unavailable"
        while self.clock() < deadline:
            return_code = process.poll()
            if return_code is not None:
                raise DependencyError(f"Dependency {dependency_id!r} exited during startup with code {return_code}")
            try:
                if self.heartbeat(dependency_id):
                    status = self.query_status(dependency_id)
                    if status.state in {DependencyState.READY, DependencyState.DEGRADED}:
                        self._set_status(status.dependency_id, status.state, status.message, status.details)
                        LOGGER.info("Dependency became available id=%s state=%s", dependency_id, status.state)
                        return status
                    if status.state == DependencyState.ERROR:
                        raise DependencyError(f"Dependency {dependency_id!r} reported an error: {status.message}")
            except DependencyError as exc:
                last_error = str(exc)
            self.sleeper(self.poll_interval)
        raise DependencyError(f"Dependency {dependency_id!r} did not become ready: {last_error}")

    def query_status(self, dependency_id: str) -> DependencyStatus:
        """Query any dependency by its meaningful id through D-Bus."""

        descriptor = self._descriptor(dependency_id)
        status = self.status_client.query(descriptor, timeout=self.dbus_timeout)
        self._set_status(status.dependency_id, status.state, status.message, status.details)
        return status

    def heartbeat(self, dependency_id: str) -> bool:
        """Check whether a dependency responds to the common D-Bus heartbeat."""

        return self.status_client.ping(self._descriptor(dependency_id), timeout=self.dbus_timeout)

    def stop(self, dependency_id: str) -> bool:
        """Stop one process launched by this manager."""

        with self._lifecycle_lock:
            with self._lock:
                running = self._running.pop(dependency_id, None)
            if running is None:
                return False
            self._set_status(dependency_id, DependencyState.STOPPING, "Stopping dependency")
            process = running.process
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=self.stop_timeout)
                    except subprocess.TimeoutExpired:
                        LOGGER.warning("Dependency did not stop gracefully id=%s", dependency_id)
                        process.kill()
                        process.wait(timeout=self.stop_timeout)
            finally:
                running.log_handle.close()
            LOGGER.info("Dependency process stopped id=%s return_code=%s", dependency_id, process.returncode)
            self._set_status(dependency_id, DependencyState.STOPPED, "Stopped")
            return True

    def close(self) -> None:
        """Stop all processes in deterministic dependency-id order."""

        with self._lock:
            dependency_ids = sorted(self._running)
        for dependency_id in dependency_ids:
            try:
                self.stop(dependency_id)
            except (DependencyError, OSError, subprocess.SubprocessError) as exc:
                LOGGER.warning("Unable to stop dependency id=%s error_type=%s", dependency_id, type(exc).__name__)

    def descriptors(self) -> tuple[DependencyDescriptor, ...]:
        with self._lock:
            return tuple(self._descriptors[key] for key in sorted(self._descriptors))

    def statuses(self, *, refresh: bool = False) -> tuple[DependencyStatus, ...]:
        """Return manager-owned lifecycle snapshots in stable dependency order."""

        if refresh:
            for dependency_id in tuple(item.dependency_id for item in self.descriptors()):
                with self._lock:
                    running = self._running.get(dependency_id)
                if running is None or running.process.poll() is not None:
                    continue
                try:
                    self.query_status(dependency_id)
                except DependencyError as exc:
                    self._set_status(dependency_id, DependencyState.ERROR, str(exc))
        with self._lock:
            return tuple(self._statuses[key] for key in sorted(self._statuses))

    def _descriptor(self, dependency_id: str) -> DependencyDescriptor:
        _validate_dependency_id(dependency_id)
        with self._lock:
            descriptor = self._descriptors.get(dependency_id)
        if descriptor is None:
            raise DependencyError(f"Unknown instance dependency: {dependency_id}")
        return descriptor

    def _set_status(
        self,
        dependency_id: str,
        state: DependencyState,
        message: str = "",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._statuses[dependency_id] = DependencyStatus(
                dependency_id,
                state,
                message,
                dict(details) if details is not None else None,
            )


def _write_marker(path: Path, dependency_id: str) -> None:
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{dependency_id}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise DependencyError(f"Cannot write dependency installation marker: {path}") from exc


__all__ = [
    "DEPENDENCY_DBUS_INTERFACE",
    "DEPENDENCY_DBUS_PATH",
    "DEPENDENCY_MANIFEST",
    "DEPENDENCY_READY_MARKER",
    "DBusDependencyStatusClient",
    "DependencyDBus",
    "DependencyDescriptor",
    "DependencyError",
    "DependencyManager",
    "DependencyStatus",
    "DependencyStatusClient",
    "DependencyState",
    "discover_dependencies",
    "load_dependency_manifest",
]
