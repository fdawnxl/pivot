"""Generic isolated event sources, dynamic waits, and LLM wake-up messages."""

from __future__ import annotations

import json
import logging
import operator
import os
import subprocess
import threading
import time
import tomllib
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast
from uuid import uuid4

from .models import EventDescriptor

if TYPE_CHECKING:
    from .memory import RuntimeStore

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


class EventPoller(Protocol):
    """Minimal source adapter required by the event supervisor."""

    def poll(self, source: str) -> Mapping[str, Any]: ...


class EventScriptRunner:
    """Execute event source scripts through their dedicated uv project."""

    def __init__(
        self,
        environment: str | Path,
        *,
        instance: str | Path | None = None,
        timeout: float = 15.0,
        uv_binary: str = "uv",
        max_output_bytes: int = 1024 * 1024,
    ) -> None:
        self.environment = Path(environment).expanduser().resolve()
        self.instance = Path(instance).expanduser().resolve() if instance else self.environment.parent.parent
        self.timeout = timeout
        self.uv_binary = uv_binary
        self.max_output_bytes = max_output_bytes
        if timeout <= 0 or max_output_bytes < 1:
            raise ValueError("timeout and max_output_bytes must be positive")

    def _run(self, script: str | Path, arguments: list[str]) -> object:
        script_path = Path(script).expanduser().resolve()
        if not script_path.is_file():
            raise EventError(f"Event source does not exist: {script_path}")
        command = [self.uv_binary, "run", "--project", str(self.environment), "python", str(script_path), *arguments]
        LOGGER.info("Event process started script=%s operation=%s", script_path.name, arguments[0])
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
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
                cwd=self.instance,
                env=environment,
            )
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
        if len(result.stdout.encode("utf-8")) > self.max_output_bytes:
            raise EventError(f"Event source output exceeds {self.max_output_bytes} bytes")
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
    agent_id: str
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
    agent_id: str
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
        self._condition = threading.Condition(self._lock)

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

    def create_wait(self, event_name: str, agent_id: str, operator_name: str, expected: Any, timeout: float) -> EventWait:
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
                agent_id,
                operator_name,
                expected,
                timeout,
                self._clock() + timeout,
            )
            self._waiters[wait.wait_id] = wait
        LOGGER.info("Event wait created event=%s wait_id=%s timeout=%g", event_name, wait.wait_id, timeout, extra={"event": event_name})
        return wait

    def cancel(self, wait_id: str) -> bool:
        with self._condition:
            cancelled = self._waiters.pop(wait_id, None) is not None
            if cancelled:
                self._condition.notify_all()
        LOGGER.info("Event wait cancellation wait_id=%s cancelled=%s", wait_id, cancelled)
        return cancelled

    def pending_sources(self) -> tuple[str, ...]:
        with self._lock:
            names = {wait.event_name for wait in self._waiters.values()}
            return tuple(sorted({event.source for name in names if (event := self._events[name]).source is not None}))

    def next_deadline_delay(self) -> float | None:
        """Return seconds until the earliest pending wait expires."""

        with self._lock:
            if not self._waiters:
                return None
            return max(0.0, min(wait.deadline for wait in self._waiters.values()) - self._clock())

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

    def wait_completion(self, wait_id: str, *, timeout: float) -> EventNotification | None:
        """Wait briefly for one completion without polling an event source."""

        if timeout <= 0:
            raise ValueError("Event completion wait timeout must be positive")
        with self._condition:
            notification = self._completed.pop(wait_id, None)
            if notification is not None:
                return notification
            if wait_id not in self._waiters:
                return None
            self._condition.wait(timeout)
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
        notification = EventNotification(wait_id, wait.event_name, wait.agent_id, status, message, dict(payload) if payload else None)
        self._completed[wait_id] = notification
        self._condition.notify_all()
        LOGGER.info("Event wait completed event=%s wait_id=%s status=%s", wait.event_name, wait_id, status, extra={"event": wait.event_name})
        return notification


def load_event_scripts_isolated(root: str | Path, runner: EventScriptRunner) -> tuple[EventDescriptor, ...]:
    """Load generic event metadata without importing instance scripts."""

    result: list[EventDescriptor] = []
    for script in sorted(Path(root).expanduser().glob("*.py")):
        try:
            result.extend(runner.list_events(script))
        except EventError as exc:
            LOGGER.warning("Unable to load isolated event script %s: %s", script, exc)
    LOGGER.info("Instance event discovery completed loaded=%d root=%s", len(result), root)
    return tuple(result)


class EventSupervisor:
    """Poll event scripts shared by waits and autonomous bridge subscribers."""

    def __init__(
        self,
        pool: EventPool,
        runner: EventPoller | None,
        *,
        poll_interval: float = 1.0,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("Event poll interval must be positive")
        self.pool = pool
        self.runner = runner
        self.poll_interval = poll_interval
        self._poll_lock = threading.Lock()
        self._listeners: dict[str, set[Callable[[str, Mapping[str, Any]], None]]] = {}
        self._listener_lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._stop = threading.Event()
        self._wake_condition = threading.Condition()
        self._wake_generation = 0
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        """Return whether the shared polling loop is active."""

        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        """Start the single polling loop shared by waits and subscribers."""

        with self._lifecycle_lock:
            if self.running:
                return
            if self._thread is not None:
                raise EventError("Event supervisor cannot be restarted after stopping")
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="pivot-event-supervisor",
                daemon=True,
            )
            self._thread.start()
        LOGGER.info("Event supervisor started poll_interval=%g", self.poll_interval)

    def close(self) -> None:
        """Stop the shared polling loop once all event consumers are closed."""

        with self._lifecycle_lock:
            thread = self._thread
            if thread is None:
                return
            self._stop.set()
            self.wake()
        if thread is not threading.current_thread():
            thread.join()
        LOGGER.info("Event supervisor stopped")

    def wake(self) -> None:
        """Wake the polling loop when its source set changes."""

        with self._wake_condition:
            self._wake_generation += 1
            self._wake_condition.notify_all()

    def _run(self) -> None:
        with self._wake_condition:
            generation = self._wake_generation
        while not self._stop.is_set():
            self.poll_once()
            deadline_delay = self.pool.next_deadline_delay()
            delay = (
                self.poll_interval
                if deadline_delay is None
                else min(self.poll_interval, deadline_delay)
            )
            with self._wake_condition:
                if self._stop.is_set():
                    return
                if generation == self._wake_generation:
                    self._wake_condition.wait(delay)
                generation = self._wake_generation

    def subscribe(
        self,
        source: str,
        listener: Callable[[str, Mapping[str, Any]], None],
    ) -> Callable[[], None]:
        """Subscribe to successful source observations without creating an Agent wait."""

        if not source:
            raise ValueError("Event source must not be empty")
        with self._listener_lock:
            self._listeners.setdefault(source, set()).add(listener)
        self.wake()

        def unsubscribe() -> None:
            with self._listener_lock:
                listeners = self._listeners.get(source)
                if listeners is not None:
                    listeners.discard(listener)
                    if not listeners:
                        self._listeners.pop(source, None)
            self.wake()

        return unsubscribe

    def _sources(self) -> tuple[str, ...]:
        with self._listener_lock:
            subscribed = tuple(self._listeners)
        return tuple(sorted(set(self.pool.pending_sources()).union(subscribed)))

    def poll_once(self) -> tuple[EventNotification, ...]:
        notifications: list[EventNotification] = []
        with self._poll_lock:
            notifications.extend(self.pool.expire())
            for source in self._sources():
                if self.runner is None:
                    error = "Event source runner is unavailable"
                    LOGGER.warning("Unable to poll event source %s: %s", source, error)
                    notifications.extend(self.pool.fail_source(source, error))
                    continue
                try:
                    payload = self.runner.poll(source)
                except Exception as exc:
                    detail = str(exc) if isinstance(exc, EventError) else f"{type(exc).__name__}: {exc}"
                    LOGGER.warning("Unable to poll event source %s: %s", source, detail)
                    notifications.extend(self.pool.fail_source(source, detail))
                else:
                    notifications.extend(self.pool.report_source(source, payload))
                    with self._listener_lock:
                        listeners = tuple(self._listeners.get(source, ()))
                    for listener in listeners:
                        try:
                            listener(source, payload)
                        except Exception as exc:
                            LOGGER.warning(
                                "Event source subscriber failed source=%s error_type=%s",
                                source,
                                type(exc).__name__,
                            )
        return tuple(notifications)


@dataclass(frozen=True, slots=True)
class EventBridgeRule:
    """Declarative rule converting a matching event condition into a stimulus."""

    bridge_id: str
    event_name: str
    operator: str
    expected: Any
    delivery: Literal["activate", "state"] = "activate"
    priority: int = 20
    replay_safe: bool = False
    cooldown: float = 0.0

    @property
    def signature(self) -> str:
        """Return the stable condition identity used to scope persisted edge state."""

        return json.dumps(
            [self.event_name, self.operator, self.expected],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], events: EventPool) -> "EventBridgeRule":
        allowed = {
            "id",
            "event",
            "operator",
            "expected",
            "delivery",
            "priority",
            "replay_safe",
            "cooldown",
        }
        unknown = set(value) - allowed
        if unknown:
            raise EventError(f"Unknown event bridge fields: {', '.join(sorted(unknown))}")
        bridge_id = value.get("id")
        if not isinstance(bridge_id, str) or not bridge_id.strip() or len(bridge_id) > 256:
            raise EventError("Event bridge id must be a non-empty string of at most 256 characters")
        event_name = value.get("event")
        if not isinstance(event_name, str) or not event_name.strip():
            raise EventError("Event bridge event must be a non-empty string")
        descriptor = next((item for item in events.descriptors() if item.name == event_name), None)
        if descriptor is None:
            raise EventError(f"Event bridge references unknown event: {event_name}")
        if descriptor.source is None:
            raise EventError(f"Event bridge references event without a pollable source: {event_name}")
        operator_name = value.get("operator")
        if operator_name not in descriptor.operators:
            raise EventError(f"Event bridge operator is not supported by event {event_name}")
        if "expected" not in value:
            raise EventError("Event bridge expected value is required")
        try:
            json.dumps(value.get("expected"), ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise EventError("Event bridge expected value must be JSON serializable") from exc
        try:
            delivery = str(value.get("delivery", "activate"))
            if delivery not in {"activate", "state"}:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise EventError("Event bridge delivery must be 'activate' or 'state'") from exc
        priority = value.get("priority", 20)
        if not isinstance(priority, int) or isinstance(priority, bool) or not -100 <= priority <= 100:
            raise EventError("Event bridge priority must be an integer between -100 and 100")
        replay_safe = value.get("replay_safe", delivery == "state")
        if not isinstance(replay_safe, bool):
            raise EventError("Event bridge replay_safe must be a boolean")
        cooldown = value.get("cooldown", 0.0)
        if not isinstance(cooldown, (int, float)) or isinstance(cooldown, bool) or cooldown < 0:
            raise EventError("Event bridge cooldown must be zero or positive")
        return cls(
            bridge_id.strip(),
            event_name.strip(),
            str(operator_name),
            value.get("expected"),
            cast(Literal["activate", "state"], delivery),
            priority,
            replay_safe,
            float(cooldown),
        )


def load_event_bridge_rules(path: str | Path, events: EventPool) -> tuple[EventBridgeRule, ...]:
    """Load optional instance bridge rules, skipping invalid rules independently."""

    bridge_path = Path(path).expanduser().resolve()
    if not bridge_path.is_file():
        return ()
    try:
        with bridge_path.open("rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        LOGGER.warning("Unable to load event bridge rules path=%s error=%s", bridge_path, exc)
        return ()
    raw_rules = document.get("bridge", document.get("bridges", []))
    if not isinstance(raw_rules, list):
        LOGGER.warning("Event bridge rules must be an array path=%s", bridge_path)
        return ()
    result: list[EventBridgeRule] = []
    seen: set[str] = set()
    for raw in raw_rules:
        if not isinstance(raw, Mapping):
            LOGGER.warning("Skipping non-object event bridge rule path=%s", bridge_path)
            continue
        try:
            rule = EventBridgeRule.from_mapping(raw, events)
            if rule.bridge_id in seen:
                raise EventError(f"Duplicate event bridge id: {rule.bridge_id}")
            seen.add(rule.bridge_id)
            result.append(rule)
        except EventError as exc:
            LOGGER.warning("Skipping invalid event bridge rule path=%s error=%s", bridge_path, exc)
    LOGGER.info("Event bridge discovery completed loaded=%d path=%s", len(result), bridge_path)
    return tuple(result)


class EventStimulusBridge:
    """Continuously translate rising event conditions into durable main-agent stimuli."""

    def __init__(
        self,
        rules: Sequence[EventBridgeRule],
        *,
        supervisor: EventSupervisor,
        store: RuntimeStore,
        publish: Callable[[Mapping[str, Any], Mapping[str, Any]], str],
    ) -> None:
        self.rules = tuple(rules)
        self.supervisor = supervisor
        self.store = store
        self.publish = publish
        self._rules_by_source: dict[str, tuple[EventBridgeRule, ...]] = {}
        self._descriptors = {item.name: item for item in supervisor.pool.descriptors()}
        bridge_ids = [rule.bridge_id for rule in self.rules]
        if len(set(bridge_ids)) != len(bridge_ids):
            raise EventError("Event bridge ids must be unique")
        for rule in self.rules:
            descriptor = self._descriptors.get(rule.event_name)
            if descriptor is None or descriptor.source is None:
                raise EventError(f"Event bridge {rule.bridge_id} has no pollable event source")
            if rule.operator not in descriptor.operators:
                raise EventError(f"Event bridge {rule.bridge_id} uses an unsupported operator")
            source = descriptor.source
            self._rules_by_source[source] = (*self._rules_by_source.get(source, ()), rule)
        self._unsubscribers: list[Callable[[], None]] = []
        self._lock = threading.RLock()

    def start(self) -> None:
        if self._unsubscribers or not self._rules_by_source:
            return
        for source in self._rules_by_source:
            self._unsubscribers.append(self.supervisor.subscribe(source, self.observe))
        LOGGER.info("Event-to-stimulus bridge started rules=%d", len(self.rules))

    def close(self) -> None:
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        if self.rules:
            LOGGER.info("Event-to-stimulus bridge stopped rules=%d", len(self.rules))

    def observe(self, source: str, payload: Mapping[str, Any]) -> None:
        """Evaluate one successful source observation against configured bridge rules."""

        now = time.time()
        with self._lock:
            rules = self._rules_by_source.get(source, ())
            for rule in rules:
                descriptor = self._descriptors[rule.event_name]
                if descriptor.field not in payload:
                    continue
                value = payload[descriptor.field]
                try:
                    matched = OPERATORS[rule.operator](value, rule.expected)
                except (TypeError, ValueError):
                    LOGGER.warning("Event bridge condition evaluation failed bridge_id=%s", rule.bridge_id)
                    continue
                previous = self.store.event_bridge_state(rule.bridge_id)
                if previous is not None and previous["rule_signature"] != rule.signature:
                    previous = None
                if not matched:
                    self.store.save_event_bridge_state(
                        rule.bridge_id,
                        rule_signature=rule.signature,
                        matched=False,
                        value=value,
                        observed_at=now,
                    )
                    continue
                if previous is not None and previous["matched"]:
                    self.store.save_event_bridge_state(
                        rule.bridge_id,
                        rule_signature=rule.signature,
                        matched=True,
                        value=value,
                        observed_at=now,
                    )
                    continue
                if (
                    previous is not None
                    and previous["fired_at"] is not None
                    and rule.cooldown > 0
                    and now - float(previous["fired_at"]) < rule.cooldown
                ):
                    self.store.save_event_bridge_state(
                        rule.bridge_id,
                        rule_signature=rule.signature,
                        matched=True,
                        value=value,
                        observed_at=now,
                    )
                    continue
                occurrence_id = str(uuid4())
                stimulus_payload = {
                    "values": {descriptor.field: value},
                    "event": rule.event_name,
                    "bridge_id": rule.bridge_id,
                    "occurrence_id": occurrence_id,
                    "condition": {
                        "field": descriptor.field,
                        "operator": rule.operator,
                        "expected": rule.expected,
                    },
                    "observation": dict(payload),
                    "observed_at": now,
                }
                envelope = {
                    "kind": "observation",
                    "source": f"event-bridge:{rule.bridge_id}",
                    "payload": stimulus_payload,
                    "priority": rule.priority,
                    "delivery": rule.delivery,
                    "replay_safe": rule.replay_safe,
                    "causation_id": occurrence_id,
                    "dedupe_key": occurrence_id,
                }
                occurrence = {
                    "occurrence_id": occurrence_id,
                    "bridge_id": rule.bridge_id,
                    "event_name": rule.event_name,
                    "source": source,
                    "field": descriptor.field,
                    "operator": rule.operator,
                    "expected": rule.expected,
                    "value": value,
                    "payload": stimulus_payload,
                    "observed_at": now,
                    "rule_signature": rule.signature,
                    "fired_at": now,
                }
                try:
                    stimulus_id = self.publish(envelope, occurrence)
                except Exception as exc:
                    LOGGER.warning(
                        "Event bridge publish failed bridge_id=%s error_type=%s",
                        rule.bridge_id,
                        type(exc).__name__,
                    )
                    continue
                self.store.save_event_bridge_state(
                    rule.bridge_id,
                    rule_signature=rule.signature,
                    matched=True,
                    value=value,
                    observed_at=now,
                    fired_at=now,
                )
                LOGGER.info(
                    "Event bridge emitted stimulus bridge_id=%s stimulus_id=%s occurrence_id=%s",
                    rule.bridge_id,
                    stimulus_id,
                    occurrence_id,
                )


class EventService:
    """Expose event waits as an LLM tool and return wake-up notifications."""

    def __init__(
        self,
        pool: EventPool,
        supervisor: EventSupervisor,
        *,
        poll_interval: float = 1.0,
        max_wait: float = 3600.0,
    ) -> None:
        if poll_interval <= 0 or max_wait <= 0:
            raise ValueError("poll_interval and max_wait must be greater than zero")
        self.pool = pool
        self.supervisor = supervisor
        self.poll_interval = poll_interval
        self.max_wait = max_wait

    def start(self) -> None:
        """Start the runtime-owned event polling supervisor."""

        self.supervisor.start()

    def close(self) -> None:
        """Stop the runtime-owned event polling supervisor."""

        self.supervisor.close()

    def llm_tools(self, event_names: tuple[str, ...] | None = None) -> tuple[dict[str, Any], ...]:
        """Return the event wait tool limited to an agent's assigned event names."""

        descriptors = self.pool.descriptors()
        if event_names is not None:
            allowed = set(event_names)
            descriptors = tuple(item for item in descriptors if item.name in allowed)
        if not descriptors:
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
                            "event": {"type": "string", "enum": [item.name for item in descriptors]},
                            "operator": {
                                "type": "string",
                                "enum": sorted({operator for item in descriptors for operator in item.operators}),
                            },
                            "expected": {},
                            "timeout": {"type": "number", "exclusiveMinimum": 0, "maximum": self.max_wait},
                        },
                        "required": ["event", "operator", "expected", "timeout"],
                        "additionalProperties": False,
                    },
                },
            },
        )

    def wait(
        self,
        *,
        agent_id: str,
        event: str,
        operator: str,
        expected: Any,
        timeout: float,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> EventNotification:
        if timeout > self.max_wait:
            raise EventError(f"Event timeout exceeds configured maximum ({self.max_wait:g} seconds)")
        self.start()
        request = self.pool.create_wait(event, agent_id, operator, expected, timeout)
        self.supervisor.wake()
        try:
            while True:
                if is_cancelled is not None and is_cancelled():
                    raise InterruptedError("Event wait interrupted by the caller")
                notification = self.pool.wait_completion(
                    request.wait_id,
                    timeout=min(self.poll_interval, timeout),
                )
                if notification is not None:
                    return notification
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
    "EventPoller",
    "EventPool",
    "EventScriptRunner",
    "EventService",
    "EventBridgeRule",
    "EventStimulusBridge",
    "EventSupervisor",
    "EventWait",
    "load_event_scripts_isolated",
    "load_event_bridge_rules",
]
