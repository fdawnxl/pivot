"""Main-agent ownership and framework-internal worker control operations."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4

from .actions import ActionError
from .capabilities import CapabilityError, CapabilityRegistry
from .events import EventPool, EventService
from .executors import ExecutorRegistry
from .llm import LLMClient
from .memory import TextMemory
from .session import (
    CancellationToken,
    ConversationSession,
    ProgressCallback,
    SessionCancelled,
    SessionProgress,
)

LOGGER = logging.getLogger(__name__)


class AgentControlError(ActionError):
    """Raised when an internal agent control operation is invalid."""


class AgentRole(StrEnum):
    MAIN = "main"
    WORKER = "worker"


class AgentState(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class AgentRecord:
    """Observable state for one main or delegated agent."""

    agent_id: str
    name: str
    role: AgentRole
    session: ConversationSession = field(repr=False)
    parent_id: str | None = None
    capabilities: tuple[str, ...] = ()
    events: tuple[str, ...] = ()
    state: AgentState = AgentState.CREATED
    task: str | None = None
    report: Any = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        state = self.session.state.value if self.role == AgentRole.MAIN else self.state.value
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "role": self.role.value,
            "parent_id": self.parent_id,
            "capabilities": list(self.capabilities),
            "events": list(self.events),
            "state": state,
            "task": self.task,
            "report": self.report,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class AgentControl:
    """Create workers, assign scoped resources, and return their reports to the main agent."""

    def __init__(
        self,
        main_agent: ConversationSession,
        *,
        llm: LLMClient,
        capabilities: CapabilityRegistry,
        child_memory: TextMemory,
        events: EventPool,
        event_service: EventService,
        executors: ExecutorRegistry,
        max_rounds: int,
    ) -> None:
        self.main_agent = main_agent
        self.llm = llm
        self.capabilities = capabilities
        self.child_memory = child_memory
        self.events = events
        self.event_service = event_service
        self.executors = executors
        self.max_rounds = max_rounds
        self._records: dict[str, AgentRecord] = {
            main_agent.session_id: AgentRecord(
                main_agent.session_id,
                "main",
                AgentRole.MAIN,
                main_agent,
                capabilities=tuple(item.name for item in capabilities.descriptors()),
                events=tuple(item.name for item in events.descriptors()),
                state=AgentState.RUNNING,
            )
        }
        self._lock = threading.RLock()
        main_agent.configure_agent(role=AgentRole.MAIN.value, name="main", control=self, executors=executors)

    @property
    def main_agent_id(self) -> str:
        return self.main_agent.session_id

    def records(self) -> tuple[AgentRecord, ...]:
        with self._lock:
            return tuple(
                sorted(
                    self._records.values(),
                    key=lambda item: (item.role != AgentRole.MAIN, item.created_at, item.agent_id),
                )
            )

    def get(self, agent_id: str) -> AgentRecord:
        with self._lock:
            record = self._records.get(agent_id)
        if record is None:
            raise AgentControlError(f"Unknown agent: {agent_id}")
        return record

    def prompt_context(self, session: ConversationSession) -> list[dict[str, Any]]:
        common = [
            {"name": "agent.list", "description": "List agents visible to this agent."},
            {"name": "agent.get", "description": "Read one agent's current state."},
        ]
        if session.session_id == self.main_agent_id:
            return common + [
                {
                    "name": "agent.delegate",
                    "description": (
                        "Create a worker, assign a task plus explicit capability and event scopes, "
                        "wait for its report, and return that report."
                    ),
                    "arguments": ["task", "name?", "capabilities?", "events?"],
                },
                {
                    "name": "agent.create",
                    "description": "Create an idle worker with explicit capability and event scopes.",
                    "arguments": ["name?", "capabilities?", "events?"],
                },
                {
                    "name": "agent.assign",
                    "description": "Assign a task to an existing worker and wait for its report.",
                    "arguments": ["agent_id", "task"],
                },
            ]
        return common + [
            {
                "name": "agent.report",
                "description": "Report a structured or textual task result to the main agent.",
                "arguments": ["result"],
            }
        ]

    def execute(
        self,
        session: ConversationSession,
        operation: str,
        arguments: Mapping[str, Any],
        *,
        cancellation: CancellationToken | None = None,
        progress: ProgressCallback | None = None,
    ) -> Any:
        """Invoke one internal control operation on behalf of an agent."""

        if operation == "agent.list":
            return [record.as_dict() for record in self._visible_records(session)]
        if operation == "agent.get":
            agent_id = _required_string(arguments, "agent_id")
            record = self.get(agent_id)
            if record not in self._visible_records(session):
                raise AgentControlError(f"Agent is not visible: {agent_id}")
            return record.as_dict()
        if operation == "agent.report":
            return self._report(session, arguments)
        self._require_main(session)
        if operation == "agent.create":
            return self._create(arguments).as_dict()
        if operation == "agent.assign":
            record = self.get(_required_string(arguments, "agent_id"))
            task = _required_string(arguments, "task")
            return self._assign(record, task, cancellation=cancellation, progress=progress)
        if operation == "agent.delegate":
            task = _required_string(arguments, "task")
            creation = {key: value for key, value in arguments.items() if key != "task"}
            record = self._create(creation)
            return self._assign(record, task, cancellation=cancellation, progress=progress)
        raise AgentControlError(f"Unknown agent control operation: {operation}")

    def invoke_main(
        self,
        operation: str,
        arguments: Mapping[str, Any],
        *,
        cancellation: CancellationToken | None = None,
    ) -> Any:
        """Expose the same internal control contract to the application control plane."""

        return self.execute(self.main_agent, operation, arguments, cancellation=cancellation)

    def _create(self, arguments: Mapping[str, Any]) -> AgentRecord:
        if set(arguments) - {"name", "capabilities", "events"}:
            raise AgentControlError("agent.create accepts only name, capabilities, and events")
        capabilities = _string_sequence(arguments.get("capabilities", ()), "capabilities")
        events = _string_sequence(arguments.get("events", ()), "events")
        try:
            scoped_capabilities = self.capabilities.scoped(capabilities)
        except CapabilityError as exc:
            raise AgentControlError(str(exc)) from exc
        known_events = {item.name for item in self.events.descriptors()}
        unknown_events = sorted(set(events) - known_events)
        if unknown_events:
            raise AgentControlError(f"Unknown assigned events: {', '.join(unknown_events)}")
        name_value = arguments.get("name")
        if name_value is None:
            with self._lock:
                name = f"worker-{len(self._records)}"
        elif isinstance(name_value, str) and name_value.strip():
            name = name_value.strip()
        else:
            raise AgentControlError("Agent name must be a non-empty string")
        agent_id = str(uuid4())
        session = ConversationSession(
            agent_id,
            llm=self.llm,
            capabilities=scoped_capabilities,
            memory=self.child_memory,
            events=self.events,
            event_service=self.event_service,
            event_names=events,
            executors=self.executors,
            max_rounds=self.max_rounds,
            agent_role=AgentRole.WORKER.value,
            agent_name=name,
        )
        session.configure_agent(role=AgentRole.WORKER.value, name=name, control=self, executors=self.executors)
        record = AgentRecord(
            agent_id,
            name,
            AgentRole.WORKER,
            session,
            parent_id=self.main_agent_id,
            capabilities=capabilities,
            events=events,
        )
        with self._lock:
            self._records[agent_id] = record
        LOGGER.info(
            "Worker agent created agent_id=%s capabilities=%d events=%d",
            agent_id,
            len(capabilities),
            len(events),
        )
        return record

    def _assign(
        self,
        record: AgentRecord,
        task: str,
        *,
        cancellation: CancellationToken | None,
        progress: ProgressCallback | None,
    ) -> dict[str, Any]:
        with self._lock:
            if record.role != AgentRole.WORKER or record.parent_id != self.main_agent_id:
                raise AgentControlError("Tasks can only be assigned to workers owned by the main agent")
            if record.state == AgentState.RUNNING:
                raise AgentControlError(f"Agent is already running: {record.agent_id}")
            record.state = AgentState.RUNNING
            record.task = task
            record.report = None
            record.error = None
            record.updated_at = time.time()
        self._emit(progress, "agent_started", record, f"Delegated task to {record.name}.")

        def child_progress(update: SessionProgress) -> None:
            self._emit(
                progress,
                "agent_progress",
                record,
                update.message,
                extra={"progress_kind": update.kind, "detail": update.result},
            )

        try:
            response = record.session.run(task, progress=child_progress, cancellation=cancellation)
        except SessionCancelled:
            with self._lock:
                record.state = AgentState.CANCELLED
                record.updated_at = time.time()
            self._emit(progress, "agent_failed", record, f"{record.name} was cancelled.")
            raise
        except Exception as exc:
            with self._lock:
                record.state = AgentState.FAILED
                record.error = f"{type(exc).__name__}: {exc}"
                record.updated_at = time.time()
            self._emit(progress, "agent_failed", record, f"{record.name} failed: {record.error}")
            raise AgentControlError(f"Worker {record.name} failed: {record.error}") from exc
        with self._lock:
            record.state = AgentState.COMPLETED
            record.updated_at = time.time()
            result = record.report if record.report is not None else response
            record.report = result
        self._emit(progress, "agent_completed", record, f"{record.name} reported a result.")
        return {"agent": record.as_dict(), "result": result}

    def _report(self, session: ConversationSession, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if set(arguments) != {"result"}:
            raise AgentControlError("agent.report requires exactly result")
        record = self.get(session.session_id)
        if record.role != AgentRole.WORKER:
            raise AgentControlError("Only worker agents can report delegated results")
        with self._lock:
            record.report = arguments["result"]
            record.updated_at = time.time()
        LOGGER.info("Worker agent report received agent_id=%s", record.agent_id)
        return {"accepted": True, "agent_id": record.agent_id}

    def _visible_records(self, session: ConversationSession) -> tuple[AgentRecord, ...]:
        records = self.records()
        if session.session_id == self.main_agent_id:
            return records
        return tuple(record for record in records if record.agent_id in {self.main_agent_id, session.session_id})

    def _require_main(self, session: ConversationSession) -> None:
        if session.session_id != self.main_agent_id:
            raise AgentControlError("Only the main agent can create or assign worker agents")

    def _emit(
        self,
        progress: ProgressCallback | None,
        kind: str,
        record: AgentRecord,
        message: str,
        *,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        if progress is None:
            return
        result = {"agent": record.as_dict(), **dict(extra or {})}
        try:
            progress(SessionProgress(kind, self.main_agent_id, message, name=record.name, result=result))
        except Exception as exc:
            LOGGER.warning("Agent progress callback failed agent_id=%s error_type=%s", record.agent_id, type(exc).__name__)


def _required_string(arguments: Mapping[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise AgentControlError(f"Agent control argument {name!r} must be a non-empty string")
    return value.strip()


def _string_sequence(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise AgentControlError(f"Agent control argument {name!r} must be a string array")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise AgentControlError(f"Agent control argument {name!r} must contain non-empty strings")
    return tuple(dict.fromkeys(item.strip() for item in value))


__all__ = [
    "AgentControl",
    "AgentControlError",
    "AgentRecord",
    "AgentRole",
    "AgentState",
]
