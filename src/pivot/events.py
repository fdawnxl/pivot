"""Generic isolated event sources, dynamic waits, and LLM wake-up messages."""

from __future__ import annotations

import json
import logging
import operator
import subprocess
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from .models import EventDescriptor

LOGGER = logging.getLogger(__name__)
EVENT_WAIT_TOOL = "pivot_wait_event"
OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    ">": operator.gt,
    ">=": operator.ge,
    "==": operator.eq,
    "!=": operator.ne,
    "<": operator.lt,
    "<=": operator.le,
}


class EventError(RuntimeError):
    """Raised for invalid event definitions or operations."""


class EventScriptRunner:
    """Execute event source scripts through their dedicated uv project."""

    def __init__(self, environment: str | Path, *, timeout: float = 15.0, uv_binary: str = "uv") -> None:
        self.environment = Path(environment).expanduser().resolve()
        self.timeout = timeout
        self.uv_binary = uv_binary
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")

    def _run(self, script: str | Path, arguments: list[str]) -> object:
        script_path = Path(script).expanduser().resolve()
        command = [self.uv_binary, "run", "--project", str(self.environment), "python", str(script_path), *arguments]
        LOGGER.info("Event process started script=%s operation=%s", script_path.name, arguments[0])
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=self.timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            LOGGER.error("Event process timed out script=%s timeout=%g", script_path.name, self.timeout)
            raise EventError(f"Event source timed out after {self.timeout:g} seconds") from exc
        except OSError as exc:
            LOGGER.error("Event process could not start script=%s error_type=%s", script_path.name, type(exc).__name__)
            raise EventError(f"Event source could not start: {type(exc).__name__}") from exc
        if result.returncode != 0:
            detail = result.stderr.strip()[-500:] or "no error detail"
            LOGGER.error("Event process failed script=%s return_code=%d stderr=%s", script_path.name, result.returncode, detail)
            raise EventError(f"Event source failed with code {result.returncode}: {detail}")
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise EventError(f"Event source returned invalid JSON: {exc.msg}") from exc
        LOGGER.info("Event process completed script=%s operation=%s", script_path.name, arguments[0])
        return value

    def list_events(self, script: str | Path) -> tuple[EventDescriptor, ...]:
        value = self._run(script, ["-l"])
        if not isinstance(value, list):
            raise EventError("Event -l response must be a JSON list")
        result: list[EventDescriptor] = []
        source = str(Path(script).expanduser().resolve())
        for item in value:
            if not isinstance(item, dict):
                raise EventError("Event descriptor must be a JSON object")
            try:
                raw_operators = item["operators"]
                if not isinstance(raw_operators, list) or not raw_operators:
                    raise TypeError("operators must be a non-empty list")
                operators = tuple(str(value) for value in raw_operators)
                if any(value not in OPERATORS for value in operators):
                    raise ValueError("unsupported operator")
                raw_templates = item.get("templates", {})
                if not isinstance(raw_templates, dict):
                    raise TypeError("templates must be an object")
                result.append(
                    EventDescriptor(
                        name=str(item["name"]),
                        description=str(item["description"]),
                        field=str(item["field"]),
                        operators=operators,
                        templates={str(key): str(template) for key, template in raw_templates.items()},
                        timeout_template=str(item.get("timeout_template", EventDescriptor.__dataclass_fields__["timeout_template"].default)),
                        error_template=str(item.get("error_template", EventDescriptor.__dataclass_fields__["error_template"].default)),
                        source=source,
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise EventError("Event descriptor is invalid or missing required fields") from exc
        return tuple(result)

    def poll(self, script: str | Path) -> dict[str, object]:
        value = self._run(script, ["-p"])
        if not isinstance(value, dict):
            raise EventError("Event -p response must be a JSON object")
        return {str(key): item for key, item in value.items()}


@dataclass(frozen=True, slots=True)
class EventWait:
    wait_id: str
    event_name: str
    field: str
    session_id: str
    operator: str
    expected: Any
    timeout: float
    deadline: float

    @property
    def condition(self) -> str:
        return f"{self.field} {self.operator} {self.expected}"


@dataclass(frozen=True, slots=True)
class EventNotification:
    wait_id: str
    event_name: str
    session_id: str
    status: Literal["matched", "timeout", "error"]
    message: str
    payload: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "wait_id": self.wait_id,
            "event": self.event_name,
            "status": self.status,
            "message": self.message,
            **({"payload": dict(self.payload)} if self.payload is not None else {}),
        }


class EventPool:
    """Own generic event sources, dynamic waits, and durable-in-process completions."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._events: dict[str, EventDescriptor] = {}
        self._waiters: OrderedDict[str, EventWait] = OrderedDict()
        self._completed: dict[str, EventNotification] = {}
        self._clock = clock
        self._lock = threading.RLock()

    def register(self, event: EventDescriptor) -> None:
        if not event.operators or any(item not in OPERATORS for item in event.operators):
            raise EventError("Event must provide supported operators")
        if any(item not in event.operators for item in event.templates):
            raise EventError("Event template references an unsupported operator")
        for template in (*event.templates.values(), event.timeout_template, event.error_template):
            _format_template(template, condition="field > value", value="value", timeout=1.0, error="error")
        with self._lock:
            if event.name in self._events:
                raise EventError(f"Event already registered: {event.name}")
            self._events[event.name] = event
        LOGGER.info("Event registered name=%s source=%s", event.name, event.source or "built-in")

    def descriptors(self) -> tuple[EventDescriptor, ...]:
        with self._lock:
            return tuple(sorted(self._events.values(), key=lambda item: item.name))

    def create_wait(self, event_name: str, session_id: str, operator_name: str, expected: Any, timeout: float) -> EventWait:
        if timeout <= 0:
            raise EventError("Event timeout must be greater than zero")
        with self._lock:
            event = self._events.get(event_name)
            if event is None:
                raise EventError(f"Unknown event: {event_name}")
            if operator_name not in event.operators:
                raise EventError(f"Event {event_name} does not support operator {operator_name}")
            wait = EventWait(
                str(uuid4()),
                event_name,
                event.field,
                session_id,
                operator_name,
                expected,
                timeout,
                self._clock() + timeout,
            )
            self._waiters[wait.wait_id] = wait
        LOGGER.info("Event wait created event=%s wait_id=%s timeout=%g", event_name, wait.wait_id, timeout, extra={"event": event_name})
        return wait

    def cancel(self, wait_id: str) -> bool:
        with self._lock:
            cancelled = self._waiters.pop(wait_id, None) is not None
        LOGGER.info("Event wait cancellation wait_id=%s cancelled=%s", wait_id, cancelled)
        return cancelled

    def pending_sources(self) -> tuple[str, ...]:
        with self._lock:
            names = {wait.event_name for wait in self._waiters.values()}
            return tuple(sorted({event.source for name in names if (event := self._events[name]).source is not None}))

    def report_source(self, source: str, payload: Mapping[str, Any]) -> tuple[EventNotification, ...]:
        notifications: list[EventNotification] = []
        with self._lock:
            for wait_id, wait in tuple(self._waiters.items()):
                event = self._events[wait.event_name]
                if event.source != source:
                    continue
                if event.field not in payload:
                    notifications.append(self._complete_error(wait_id, f"payload is missing field {event.field!r}"))
                    continue
                try:
                    matched = OPERATORS[wait.operator](payload[event.field], wait.expected)
                except (TypeError, ValueError) as exc:
                    notifications.append(self._complete_error(wait_id, f"condition evaluation failed: {exc}"))
                    continue
                if matched:
                    message = _format_template(
                        event.templates.get(wait.operator, "Event condition {condition} matched with value {value}."),
                        condition=wait.condition,
                        value=payload[event.field],
                        timeout=wait.timeout,
                        error="",
                    )
                    notifications.append(self._complete(wait_id, "matched", message, payload))
        return tuple(notifications)

    def fail_source(self, source: str, error: str) -> tuple[EventNotification, ...]:
        with self._lock:
            wait_ids = [wait_id for wait_id, wait in self._waiters.items() if self._events[wait.event_name].source == source]
            return tuple(self._complete_error(wait_id, error) for wait_id in wait_ids)

    def expire(self) -> tuple[EventNotification, ...]:
        now = self._clock()
        with self._lock:
            expired = [wait_id for wait_id, wait in self._waiters.items() if wait.deadline <= now]
            notifications = []
            for wait_id in expired:
                wait = self._waiters[wait_id]
                event = self._events[wait.event_name]
                message = _format_template(
                    event.timeout_template,
                    condition=wait.condition,
                    value="",
                    timeout=wait.timeout,
                    error="",
                )
                notifications.append(self._complete(wait_id, "timeout", message))
            return tuple(notifications)

    def take_completion(self, wait_id: str) -> EventNotification | None:
        with self._lock:
            return self._completed.pop(wait_id, None)

    def _complete_error(self, wait_id: str, error: str) -> EventNotification:
        wait = self._waiters[wait_id]
        event = self._events[wait.event_name]
        message = _format_template(event.error_template, condition=wait.condition, value="", timeout=wait.timeout, error=error)
        return self._complete(wait_id, "error", message)

    def _complete(
        self,
        wait_id: str,
        status: Literal["matched", "timeout", "error"],
        message: str,
        payload: Mapping[str, Any] | None = None,
    ) -> EventNotification:
        wait = self._waiters.pop(wait_id)
        notification = EventNotification(wait_id, wait.event_name, wait.session_id, status, message, dict(payload) if payload else None)
        self._completed[wait_id] = notification
        LOGGER.info("Event wait completed event=%s wait_id=%s status=%s", wait.event_name, wait_id, status, extra={"event": wait.event_name})
        return notification


def load_event_scripts_isolated(root: str | Path, runner: EventScriptRunner) -> tuple[EventDescriptor, ...]:
    """Load generic event metadata without importing workspace scripts."""

    result: list[EventDescriptor] = []
    for script in sorted(Path(root).expanduser().glob("*.py")):
        try:
            result.extend(runner.list_events(script))
        except EventError as exc:
            LOGGER.warning("Unable to load isolated event script %s: %s", script, exc)
    LOGGER.info("Workspace event discovery completed loaded=%d root=%s", len(result), root)
    return tuple(result)


class EventSupervisor:
    """Poll only event scripts that currently have pending waits."""

    def __init__(self, pool: EventPool, runner: EventScriptRunner) -> None:
        self.pool = pool
        self.runner = runner
        self._poll_lock = threading.Lock()

    def poll_once(self) -> tuple[EventNotification, ...]:
        notifications: list[EventNotification] = []
        with self._poll_lock:
            notifications.extend(self.pool.expire())
            for source in self.pool.pending_sources():
                try:
                    payload = self.runner.poll(source)
                except EventError as exc:
                    LOGGER.warning("Unable to poll event source %s: %s", source, exc)
                    notifications.extend(self.pool.fail_source(source, str(exc)))
                else:
                    notifications.extend(self.pool.report_source(source, payload))
        return tuple(notifications)


class EventService:
    """Expose event waits as an LLM tool and return wake-up notifications."""

    def __init__(
        self,
        pool: EventPool,
        supervisor: EventSupervisor,
        *,
        poll_interval: float = 1.0,
        max_wait: float = 3600.0,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if poll_interval <= 0 or max_wait <= 0:
            raise ValueError("poll_interval and max_wait must be greater than zero")
        self.pool = pool
        self.supervisor = supervisor
        self.poll_interval = poll_interval
        self.max_wait = max_wait
        self.sleeper = sleeper

    def llm_tools(self) -> tuple[dict[str, Any], ...]:
        if not self.pool.descriptors():
            return ()
        return (
            {
                "type": "function",
                "function": {
                    "name": EVENT_WAIT_TOOL,
                    "description": "Wait for a dynamic event condition, then continue after a match, timeout, or source error.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "event": {"type": "string", "enum": [item.name for item in self.pool.descriptors()]},
                            "operator": {"type": "string", "enum": sorted(OPERATORS)},
                            "expected": {},
                            "timeout": {"type": "number", "exclusiveMinimum": 0, "maximum": self.max_wait},
                        },
                        "required": ["event", "operator", "expected", "timeout"],
                        "additionalProperties": False,
                    },
                },
            },
        )

    def wait(self, *, session_id: str, event: str, operator: str, expected: Any, timeout: float) -> EventNotification:
        if timeout > self.max_wait:
            raise EventError(f"Event timeout exceeds configured maximum ({self.max_wait:g} seconds)")
        request = self.pool.create_wait(event, session_id, operator, expected, timeout)
        try:
            while True:
                self.supervisor.poll_once()
                notification = self.pool.take_completion(request.wait_id)
                if notification is not None:
                    return notification
                self.sleeper(min(self.poll_interval, timeout))
        except BaseException:
            self.pool.cancel(request.wait_id)
            raise


def _format_template(template: str, **values: Any) -> str:
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError) as exc:
        raise EventError(f"Invalid event injection template: {exc}") from exc


__all__ = [
    "EVENT_WAIT_TOOL",
    "EventError",
    "EventNotification",
    "EventPool",
    "EventScriptRunner",
    "EventService",
    "EventSupervisor",
    "EventWait",
    "load_event_scripts_isolated",
]
