"""In-process event definitions and deterministic waiter pool."""

from __future__ import annotations

import operator
import importlib.util
import logging
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .models import EventDescriptor

LOGGER = logging.getLogger(__name__)


class EventError(RuntimeError):
    """Raised for invalid definitions or event operations."""


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

    def descriptors(self) -> tuple[EventDescriptor, ...]:
        with self._lock:
            return tuple(sorted(self._events.values(), key=lambda item: item.name))

    def wait(self, event_name: str, session_id: str, callback: Callable[[EventNotification], None]) -> None:
        with self._lock:
            if event_name not in self._events:
                raise EventError(f"Unknown event: {event_name}")
            self._waiters[event_name][session_id] = callback

    def cancel(self, event_name: str, session_id: str) -> bool:
        with self._lock:
            return self._waiters.get(event_name, {}).pop(session_id, None) is not None

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
                continue
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
