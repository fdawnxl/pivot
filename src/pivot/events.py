"""In-process event definitions and deterministic waiter pool."""

from __future__ import annotations

import importlib.util
import json
import logging
import operator
import subprocess
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .models import EventDescriptor

LOGGER = logging.getLogger(__name__)


class EventError(RuntimeError):
    """Raised for invalid definitions or event operations."""


class EventScriptRunner:
    """Execute event scripts through their dedicated uv project."""

    def __init__(self, environment: str, *, timeout: float = 15.0, uv_binary: str = "uv") -> None:
        self.environment = environment
        self.timeout = timeout
        self.uv_binary = uv_binary

    def _run(self, script: str, arguments: list[str]) -> object:
        command = [self.uv_binary, "run", "--project", self.environment, "python", script, *arguments]
        LOGGER.info("Event process started script=%s operation=%s", script, arguments[0])
        LOGGER.debug("Event process command=%s timeout=%g", command, self.timeout)
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=self.timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            LOGGER.error("Event process execution failed script=%s error_type=%s", script, type(exc).__name__)
            raise EventError(f"Event script execution failed: {type(exc).__name__}") from exc
        if result.returncode != 0:
            detail = result.stderr.strip()[-500:] or "no error detail"
            LOGGER.error("Event process failed script=%s return_code=%d stderr=%s", script, result.returncode, detail)
            raise EventError(f"Event script failed with code {result.returncode}: {detail}")
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            LOGGER.error("Event process returned invalid JSON script=%s", script)
            raise EventError(f"Event script returned invalid JSON: {exc.msg}") from exc
        LOGGER.info("Event process completed script=%s operation=%s", script, arguments[0])
        return value

    def list_events(self, script: str) -> tuple[EventDescriptor, ...]:
        value = self._run(script, ["-l"])
        if not isinstance(value, list):
            raise EventError("Event -l response must be a JSON list")
        result = []
        for item in value:
            if not isinstance(item, dict):
                raise EventError("Event descriptor must be a JSON object")
            try:
                result.append(EventDescriptor(str(item["name"]), str(item["description"]), str(item["field"]), str(item["operator"]), item.get("expected"), script))
            except (KeyError, TypeError, ValueError) as exc:
                raise EventError("Event descriptor is missing required fields") from exc
        return tuple(result)

    def poll(self, script: str) -> dict[str, object]:
        value = self._run(script, ["-p"])
        if not isinstance(value, dict):
            raise EventError("Event -p response must be a JSON object")
        return {str(key): item for key, item in value.items()}


OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    ">": operator.gt,
    ">=": operator.ge,
    "==": operator.eq,
    "!=": operator.ne,
    "<": operator.lt,
    "<=": operator.le,
    "is": operator.eq,
}


@dataclass(frozen=True, slots=True)
class EventNotification:
    event_name: str
    payload: Mapping[str, Any]


class EventPool:
    """Register event conditions and notify waiters in registration order."""

    def __init__(self) -> None:
        self._events: dict[str, EventDescriptor] = {}
        self._waiters: dict[str, OrderedDict[str, Callable[[EventNotification], None]]] = {}
        self._lock = threading.RLock()

    def register(self, event: EventDescriptor) -> None:
        if event.operator not in OPERATORS:
            raise EventError(f"Unsupported event operator: {event.operator}")
        with self._lock:
            if event.name in self._events:
                raise EventError(f"Event already registered: {event.name}")
            self._events[event.name] = event
            self._waiters[event.name] = OrderedDict()
        LOGGER.info("Event registered name=%s source=%s", event.name, event.source or "built-in")

    def descriptors(self) -> tuple[EventDescriptor, ...]:
        with self._lock:
            return tuple(sorted(self._events.values(), key=lambda item: item.name))

    def wait(self, event_name: str, session_id: str, callback: Callable[[EventNotification], None]) -> None:
        with self._lock:
            if event_name not in self._events:
                raise EventError(f"Unknown event: {event_name}")
            self._waiters[event_name][session_id] = callback
        LOGGER.info("Event waiter registered event=%s session_id=%s", event_name, session_id)

    def cancel(self, event_name: str, session_id: str) -> bool:
        with self._lock:
            cancelled = self._waiters.get(event_name, {}).pop(session_id, None) is not None
        LOGGER.info("Event waiter cancellation event=%s session_id=%s cancelled=%s", event_name, session_id, cancelled)
        return cancelled

    def report(self, event_name: str, payload: Mapping[str, Any]) -> tuple[str, ...]:
        """Evaluate one report and synchronously notify matching waiters in FIFO order."""

        with self._lock:
            event = self._events.get(event_name)
            if event is None:
                raise EventError(f"Unknown event: {event_name}")
            if event.field not in payload:
                raise EventError(f"Event payload is missing field: {event.field}")
            try:
                matched = OPERATORS[event.operator](payload[event.field], event.expected)
            except (TypeError, ValueError) as exc:
                raise EventError(f"Cannot evaluate event {event_name}: {exc}") from exc
            if not matched:
                LOGGER.debug("Event condition did not match name=%s field=%s", event_name, event.field)
                return ()
            waiters = tuple(self._waiters[event_name].items())
            self._waiters[event_name].clear()
        notification = EventNotification(event_name=event_name, payload=dict(payload))
        notified: list[str] = []
        for session_id, callback in waiters:
            try:
                callback(notification)
                notified.append(session_id)
            except Exception:
                # A failed waiter is isolated; callers can infer it from the result.
                LOGGER.exception("Event waiter callback failed event=%s session_id=%s", event_name, session_id)
                continue
        LOGGER.info("Event matched name=%s notified=%d", event_name, len(notified))
        return tuple(notified)


def load_event_scripts(root: str) -> tuple[EventDescriptor, ...]:
    """Load event descriptors from Python files in a workspace directory.

    A script may expose ``EVENTS`` (an iterable of descriptors) or classes with a
    ``descriptor`` attribute. Import failures are isolated and logged.
    """

    descriptors: list[EventDescriptor] = []
    from pathlib import Path

    directory = Path(root).expanduser()
    if not directory.is_dir():
        return ()
    for script in sorted(directory.glob("*.py")):
        module_name = f"pivot_workspace_event_{script.stem}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, script)
            if spec is None or spec.loader is None:
                raise ImportError("module spec unavailable")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            candidates = list(getattr(module, "EVENTS", ()))
            candidates.extend(getattr(value, "descriptor") for value in vars(module).values() if hasattr(value, "descriptor"))
            descriptors.extend(item for item in candidates if isinstance(item, EventDescriptor))
        except Exception as exc:
            LOGGER.warning("Unable to load event script %s: %s", script, exc)
    return tuple(descriptors)


def load_event_scripts_isolated(root: str, runner: EventScriptRunner) -> tuple[EventDescriptor, ...]:
    """Load event metadata without importing untrusted scripts in the framework process."""

    from pathlib import Path

    result: list[EventDescriptor] = []
    for script in sorted(Path(root).expanduser().glob("*.py")):
        try:
            result.extend(runner.list_events(str(script)))
        except EventError as exc:
            LOGGER.warning("Unable to load isolated event script %s: %s", script, exc)
    LOGGER.info("Workspace event discovery completed loaded=%d root=%s", len(result), root)
    return tuple(result)


class EventSupervisor:
    """Poll isolated event scripts and report matching payloads to an EventPool."""

    def __init__(self, pool: EventPool, root: str, runner: EventScriptRunner) -> None:
        self.pool = pool
        self.root = root
        self.runner = runner

    def poll_once(self) -> dict[str, tuple[str, ...]]:
        LOGGER.info("Event supervisor poll started root=%s", self.root)
        results: dict[str, tuple[str, ...]] = {}
        from pathlib import Path

        for script in sorted(Path(self.root).expanduser().glob("*.py")):
            try:
                payload = self.runner.poll(str(script))
            except EventError as exc:
                LOGGER.warning("Unable to poll isolated event script %s: %s", script, exc)
                continue
            for event in self.pool.descriptors():
                if event.source == str(script):
                    try:
                        notified = self.pool.report(event.name, payload)
                    except EventError as exc:
                        LOGGER.warning("Unable to report event %s: %s", event.name, exc)
                        continue
                    results[event.name] = notified
        LOGGER.info("Event supervisor poll completed evaluated=%d", len(results))
        return results
