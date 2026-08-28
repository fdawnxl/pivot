"""Main-agent ownership and framework-internal worker control operations."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4

from .actions import ActionError
from .activation import (
    ActivationProgress,
    AgentCancelled,
    CancellationToken,
    PersistentAgent,
    ProgressCallback,
)
from .capabilities import CapabilityError, CapabilityRegistry
from .events import EventPool, EventService
from .executors import ExecutorRegistry
from .llm import LLMClient
from .memory import ContextBuilder, MemoryService, RuntimeStore

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
    agent: PersistentAgent = field(repr=False)
    parent_id: str | None = None
    capabilities: tuple[str, ...] = ()
    events: tuple[str, ...] = ()
    one_shot: bool = False
    state: AgentState = AgentState.CREATED
    task: str | None = None
    task_id: str | None = None
    recurrence: str | None = None
    report: Any = None
    report_revision: int = 0
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    cancellation: CancellationToken | None = field(default=None, repr=False)
    future: Future[Any] | None = field(default=None, repr=False)

    def as_dict(self) -> dict[str, Any]:
        state = (
            self.agent.state.value if self.role == AgentRole.MAIN else self.state.value
        )
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "role": self.role.value,
            "parent_id": self.parent_id,
            "capabilities": list(self.capabilities),
            "events": list(self.events),
            "one_shot": self.one_shot,
            "state": state,
            "task": self.task,
            "task_id": self.task_id,
            "recurrence": self.recurrence,
            "report": self.report,
            "report_revision": self.report_revision,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class AgentControl:
    """Create workers, assign scoped resources, and return their reports to the main agent."""

    def __init__(
        self,
        main_agent: PersistentAgent,
        *,
        llm: LLMClient,
        capabilities: CapabilityRegistry,
        memory: RuntimeStore,
        events: EventPool,
        event_service: EventService,
        executors: ExecutorRegistry,
        max_rounds: int,
        max_workers: int = 4,
        worker_retention_seconds: float = 300.0,
        worker_cleanup_interval: float = 30.0,
    ) -> None:
        self.main_agent = main_agent
        self.llm = llm
        self.capabilities = capabilities
        self.memory = memory
        self.memory_service = MemoryService(memory)
        self.events = events
        self.event_service = event_service
        self.executors = executors
        self.max_rounds = max_rounds
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        if worker_retention_seconds <= 0 or worker_cleanup_interval <= 0:
            raise ValueError("Worker retention and cleanup interval must be positive")
        self.worker_retention_seconds = worker_retention_seconds
        self.worker_cleanup_interval = worker_cleanup_interval
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="pivot-agent"
        )
        self._completion_handler: Callable[[AgentRecord], None] | None = None
        self._report_handler: Callable[[AgentRecord, int], None] | None = None
        self._records: dict[str, AgentRecord] = {
            main_agent.agent_id: AgentRecord(
                main_agent.agent_id,
                "main",
                AgentRole.MAIN,
                main_agent,
                capabilities=tuple(item.name for item in capabilities.descriptors()),
                events=tuple(item.name for item in events.descriptors()),
                state=AgentState.RUNNING,
            )
        }
        self._lock = threading.RLock()
        self._cleanup_stop = threading.Event()
        self._cleanup_thread: threading.Thread | None = None
        main_agent.configure_control(self, executors=executors)
        self._restore_workers()
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop,
            name="pivot-agent-cleanup",
            daemon=True,
        )
        self._cleanup_thread.start()

    def set_completion_handler(
        self, handler: Callable[[AgentRecord], None] | None
    ) -> None:
        """Receive worker terminal states for reinjection into the main-agent mailbox."""

        with self._lock:
            self._completion_handler = handler

    def set_report_handler(
        self, handler: Callable[[AgentRecord, int], None] | None
    ) -> None:
        """Receive explicit worker reports before the worker activation completes."""

        with self._lock:
            self._report_handler = handler

    def close(self) -> None:
        """Cancel active workers and release worker execution threads."""

        self._cleanup_stop.set()
        cleanup_thread = self._cleanup_thread
        if (
            cleanup_thread is not None
            and cleanup_thread is not threading.current_thread()
        ):
            cleanup_thread.join(timeout=max(self.worker_cleanup_interval, 1.0))
        self._cleanup_thread = None
        self.cancel_workers()
        self.cleanup_workers()
        self._executor.shutdown(wait=True, cancel_futures=True)
        # Cancellation may only reach a terminal state while the executor is
        # draining; perform one final pass after all worker futures have ended.
        self.cleanup_workers()

    def cleanup_workers(self) -> tuple[str, ...]:
        """Reclaim expired one-shot workers without touching active monitors."""

        cutoff = time.time() - self.worker_retention_seconds
        with self._lock:
            candidates = tuple(
                record
                for record in self._records.values()
                if record.role == AgentRole.WORKER
                and record.one_shot
                and record.task_id is not None
                and record.state
                in {AgentState.COMPLETED, AgentState.FAILED, AgentState.CANCELLED}
                and record.updated_at <= cutoff
                and (record.future is None or record.future.done())
            )
        removed: list[str] = []
        for record in candidates:
            if self.memory.worker_report_pending(record.agent_id):
                continue
            try:
                deleted = self.memory.delete_worker(record.agent_id)
            except Exception as exc:
                LOGGER.warning(
                    "Unable to reclaim one-shot worker agent_id=%s error_type=%s",
                    record.agent_id,
                    type(exc).__name__,
                )
                continue
            if deleted:
                with self._lock:
                    self._records.pop(record.agent_id, None)
                removed.append(record.agent_id)
                LOGGER.info("Reclaimed one-shot worker agent_id=%s", record.agent_id)
        return tuple(removed)

    def _cleanup_loop(self) -> None:
        while not self._cleanup_stop.wait(self.worker_cleanup_interval):
            try:
                self.cleanup_workers()
            except Exception:
                LOGGER.exception("One-shot worker cleanup cycle failed")

    def cancel_workers(self) -> bool:
        """Cooperatively cancel every running worker."""

        cancelled = False
        with self._lock:
            records = tuple(self._records.values())
        for record in records:
            if (
                record.role == AgentRole.WORKER
                and record.state == AgentState.RUNNING
                and record.cancellation
            ):
                record.cancellation.cancel()
                cancelled = True
        return cancelled

    def wait(self, agent_id: str, *, timeout: float | None = None) -> AgentRecord:
        """Wait for one worker in tests or application supervision code."""

        record = self.get(agent_id)
        future = record.future
        if future is not None:
            try:
                future.result(timeout=timeout)
            except Exception:
                pass
        return self.get(agent_id)

    @property
    def main_agent_id(self) -> str:
        return self.main_agent.agent_id

    def _restore_workers(self) -> None:
        """Restore durable worker identities and mark interrupted work explicitly."""

        known_events = {item.name for item in self.events.descriptors()}
        for row in self.memory.agent_rows():
            if row["role"] != AgentRole.WORKER.value:
                continue
            capabilities = tuple(row["capabilities"])
            events = tuple(row["events"])
            try:
                scoped = self.capabilities.scoped(capabilities)
            except CapabilityError as exc:
                LOGGER.warning(
                    "Unable to restore worker agent_id=%s error=%s",
                    row["agent_id"],
                    exc,
                )
                continue
            if set(events) - known_events:
                LOGGER.warning(
                    "Unable to restore worker with unavailable events agent_id=%s",
                    row["agent_id"],
                )
                continue
            worker = PersistentAgent(
                row["agent_id"],
                llm=self.llm,
                capabilities=scoped,
                memory=self.memory,
                events=self.events,
                event_service=self.event_service,
                event_names=events,
                executors=self.executors,
                memory_service=self.memory_service,
                context_builder=ContextBuilder(self.memory),
                max_rounds=self.max_rounds,
                role=AgentRole.WORKER.value,
                name=row["name"],
            )
            worker.configure_control(self, executors=self.executors)
            try:
                state = AgentState(row["state"])
            except ValueError:
                state = AgentState.FAILED
            error = None
            task = self.memory.latest_task(row["agent_id"])
            if state == AgentState.RUNNING:
                state = AgentState.FAILED
                error = "Runtime restarted before worker completion"
                self.memory.update_agent_state(row["agent_id"], state.value)
            self._records[row["agent_id"]] = AgentRecord(
                row["agent_id"],
                row["name"],
                AgentRole.WORKER,
                worker,
                parent_id=row["parent_id"],
                capabilities=capabilities,
                events=events,
                one_shot=bool(row.get("one_shot", 0)),
                state=state,
                task=task["description"] if task else None,
                task_id=task["task_id"] if task else None,
                report=task["result"] if task and task["result"] is not None else None,
                error=error,
                created_at=float(row["created_at"]),
                updated_at=float(row["updated_at"]),
            )

    def records(self) -> tuple[AgentRecord, ...]:
        with self._lock:
            return tuple(
                sorted(
                    self._records.values(),
                    key=lambda item: (
                        item.role != AgentRole.MAIN,
                        item.created_at,
                        item.agent_id,
                    ),
                )
            )

    def get(self, agent_id: str) -> AgentRecord:
        with self._lock:
            record = self._records.get(agent_id)
        if record is None:
            raise AgentControlError(f"Unknown agent: {agent_id}")
        return record

    def prompt_context(self, agent: PersistentAgent) -> list[dict[str, Any]]:
        common = [
            {"name": "agent.list", "description": "List agents visible to this agent."},
            {"name": "agent.get", "description": "Read one agent's current state."},
        ]
        if agent.agent_id == self.main_agent_id:
            return common + [
                {
                    "name": "agent.delegate",
                    "description": (
                        "Create a worker, assign a task plus explicit capability and event scopes, "
                        "and return immediately. For event scopes, recurrence is required: use once unless the user "
                        "explicitly asks for every occurrence or continuous monitoring; use rising or falling to "
                        "match the requested repeated edge direction. Mark finite work one_shot=true so the runtime "
                        "can reclaim the worker after its report; recurring monitors must use one_shot=false. The "
                        "worker result will resume the main agent through its mailbox."
                    ),
                    "arguments": [
                        "task",
                        "name?",
                        "capabilities?",
                        "events?",
                        "recurrence (required with events: once|rising|falling)",
                        "one_shot? (boolean; defaults true for finite delegated work)",
                    ],
                },
                {
                    "name": "agent.create",
                    "description": (
                        "Create an idle worker with explicit capability, event, and lifecycle scopes. Set one_shot=true "
                        "only when the worker will receive one finite assignment and may be reclaimed after completion."
                    ),
                    "arguments": ["name?", "capabilities?", "events?", "one_shot?"],
                },
                {
                    "name": "agent.assign",
                    "description": (
                        "Assign a task asynchronously to an existing worker. For a worker with events, recurrence "
                        "is required: once is one matched occurrence then finish; rising and falling are repeated "
                        "false-to-true and true-to-false edges and require explicit repeated intent."
                    ),
                    "arguments": [
                        "agent_id",
                        "task",
                        "recurrence (required with events: once|rising|falling)",
                    ],
                },
            ]
        return common + [
            {
                "name": "agent.report",
                "description": (
                    "Immediately deliver a structured or textual occurrence result to the main agent. Reporting "
                    "does not finish this worker; return a final response after reporting for one-shot tasks, or "
                    "continue waiting only when repeated monitoring was explicitly assigned."
                ),
                "arguments": ["result"],
            }
        ]

    def execute(
        self,
        agent: PersistentAgent,
        operation: str,
        arguments: Mapping[str, Any],
        *,
        cancellation: CancellationToken | None = None,
        progress: ProgressCallback | None = None,
        origin_activation_id: str | None = None,
    ) -> Any:
        """Invoke one internal control operation on behalf of an agent."""

        if operation == "agent.list":
            return [record.as_dict() for record in self._visible_records(agent)]
        if operation == "agent.get":
            agent_id = _required_string(arguments, "agent_id")
            record = self.get(agent_id)
            if record not in self._visible_records(agent):
                raise AgentControlError(f"Agent is not visible: {agent_id}")
            return record.as_dict()
        if operation == "agent.report":
            return self._report(agent, arguments)
        self._require_main(agent)
        if operation == "agent.create":
            return self._create(arguments).as_dict()
        if operation == "agent.assign":
            if set(arguments) - {"agent_id", "task", "recurrence"}:
                raise AgentControlError(
                    "agent.assign accepts only agent_id, task, and recurrence"
                )
            record = self.get(_required_string(arguments, "agent_id"))
            task = _required_string(arguments, "task")
            return self._assign(
                record,
                task,
                recurrence=_recurrence(
                    arguments.get("recurrence"), required=bool(record.events)
                ),
                cancellation=cancellation,
                progress=progress,
                origin_activation_id=origin_activation_id,
            )
        if operation == "agent.delegate":
            task = _required_string(arguments, "task")
            requested_events = _string_sequence(arguments.get("events", ()), "events")
            recurrence = _recurrence(
                arguments.get("recurrence"),
                required=bool(requested_events),
            )
            creation = {
                key: value
                for key, value in arguments.items()
                if key not in {"task", "recurrence"}
            }
            # Delegation is normally finite.  The model can opt out explicitly
            # for a reusable worker, while recurring event scopes are never
            # eligible for one-shot cleanup.
            creation.setdefault("one_shot", recurrence in {None, "once"})
            if recurrence in {"rising", "falling"} and creation["one_shot"]:
                raise AgentControlError("Recurring event workers cannot be one-shot")
            record = self._create(creation)
            return self._assign(
                record,
                task,
                recurrence=recurrence,
                cancellation=cancellation,
                progress=progress,
                origin_activation_id=origin_activation_id,
            )
        raise AgentControlError(f"Unknown agent control operation: {operation}")

    def invoke_main(
        self,
        operation: str,
        arguments: Mapping[str, Any],
        *,
        cancellation: CancellationToken | None = None,
    ) -> Any:
        """Expose the same internal control contract to the application control plane."""

        return self.execute(
            self.main_agent, operation, arguments, cancellation=cancellation
        )

    def _create(self, arguments: Mapping[str, Any]) -> AgentRecord:
        if set(arguments) - {"name", "capabilities", "events", "one_shot"}:
            raise AgentControlError(
                "agent.create accepts only name, capabilities, events, and one_shot"
            )
        capabilities = _string_sequence(
            arguments.get("capabilities", ()), "capabilities"
        )
        events = _string_sequence(arguments.get("events", ()), "events")
        one_shot = _boolean(arguments.get("one_shot", False), "one_shot")
        try:
            scoped_capabilities = self.capabilities.scoped(capabilities)
        except CapabilityError as exc:
            raise AgentControlError(str(exc)) from exc
        known_events = {item.name for item in self.events.descriptors()}
        unknown_events = sorted(set(events) - known_events)
        if unknown_events:
            raise AgentControlError(
                f"Unknown assigned events: {', '.join(unknown_events)}"
            )
        name_value = arguments.get("name")
        if name_value is None:
            with self._lock:
                name = f"worker-{len(self._records)}"
        elif isinstance(name_value, str) and name_value.strip():
            name = name_value.strip()
        else:
            raise AgentControlError("Agent name must be a non-empty string")
        agent_id = self.memory.create_worker(
            name=name,
            parent_id=self.main_agent_id,
            capabilities=capabilities,
            events=events,
            one_shot=one_shot,
        )
        worker = PersistentAgent(
            agent_id,
            llm=self.llm,
            capabilities=scoped_capabilities,
            memory=self.memory,
            events=self.events,
            event_service=self.event_service,
            event_names=events,
            executors=self.executors,
            memory_service=self.memory_service,
            context_builder=ContextBuilder(self.memory),
            max_rounds=self.max_rounds,
            role=AgentRole.WORKER.value,
            name=name,
        )
        worker.configure_control(self, executors=self.executors)
        record = AgentRecord(
            agent_id,
            name,
            AgentRole.WORKER,
            worker,
            parent_id=self.main_agent_id,
            capabilities=capabilities,
            events=events,
            one_shot=one_shot,
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
        recurrence: str | None,
        cancellation: CancellationToken | None,
        progress: ProgressCallback | None,
        origin_activation_id: str | None = None,
    ) -> dict[str, Any]:
        token = cancellation or CancellationToken()
        with self._lock:
            if (
                record.role != AgentRole.WORKER
                or record.parent_id != self.main_agent_id
            ):
                raise AgentControlError(
                    "Tasks can only be assigned to workers owned by the main agent"
                )
            if record.one_shot and recurrence in {"rising", "falling"}:
                raise AgentControlError("Recurring event workers cannot be one-shot")
            if record.state == AgentState.RUNNING:
                raise AgentControlError(f"Agent is already running: {record.agent_id}")
            record.state = AgentState.RUNNING
            record.task = task
            record.task_id = str(uuid4())
            record.recurrence = recurrence
            record.report = None
            record.report_revision = 0
            record.error = None
            record.updated_at = time.time()
            record.cancellation = token
            self.memory.update_agent_state(record.agent_id, AgentState.RUNNING.value)
            self.memory.upsert_task(
                record.task_id,
                record.agent_id,
                task,
                AgentState.RUNNING.value,
                origin_activation_id=origin_activation_id,
            )
        self._emit(
            progress, "agent_started", record, f"Delegated task to {record.name}."
        )

        record.future = self._executor.submit(
            self._run_assigned,
            record,
            task,
            recurrence,
            token,
            progress,
        )
        return {"accepted": True, "agent": record.as_dict()}

    def _run_assigned(
        self,
        record: AgentRecord,
        task: str,
        recurrence: str | None,
        cancellation: CancellationToken,
        progress: ProgressCallback | None,
    ) -> None:
        """Execute an assigned worker without occupying the main-agent activation."""

        def child_progress(update: ActivationProgress) -> None:
            self._emit(
                progress,
                "agent_progress",
                record,
                update.message,
                extra={"progress_kind": update.kind, "detail": update.result},
            )

        try:
            response = record.agent._activate(
                task,
                source="delegation",
                progress=child_progress,
                cancellation=cancellation,
                event_recurrence=recurrence,
            )
        except AgentCancelled:
            with self._lock:
                record.state = AgentState.CANCELLED
                record.updated_at = time.time()
                self.memory.update_agent_state(
                    record.agent_id, AgentState.CANCELLED.value
                )
                if record.task_id:
                    self.memory.upsert_task(
                        record.task_id,
                        record.agent_id,
                        task,
                        AgentState.CANCELLED.value,
                    )
            self._emit(
                progress, "agent_failed", record, f"{record.name} was cancelled."
            )
        except Exception as exc:
            with self._lock:
                record.state = AgentState.FAILED
                record.error = f"{type(exc).__name__}: {exc}"
                record.updated_at = time.time()
                self.memory.update_agent_state(record.agent_id, AgentState.FAILED.value)
                if record.task_id:
                    self.memory.upsert_task(
                        record.task_id,
                        record.agent_id,
                        task,
                        AgentState.FAILED.value,
                        error=record.error,
                    )
            self._emit(
                progress,
                "agent_failed",
                record,
                f"{record.name} failed: {record.error}",
            )
        else:
            with self._lock:
                record.state = AgentState.COMPLETED
                record.updated_at = time.time()
                result = record.report if record.report is not None else response
                record.report = result
                self.memory.update_agent_state(
                    record.agent_id, AgentState.COMPLETED.value
                )
                if record.task_id:
                    self.memory.upsert_task(
                        record.task_id,
                        record.agent_id,
                        task,
                        AgentState.COMPLETED.value,
                        result=result,
                    )
            self._emit(
                progress, "agent_completed", record, f"{record.name} reported a result."
            )
        finally:
            with self._lock:
                handler = self._completion_handler
            if handler is not None:
                try:
                    handler(record)
                except Exception as exc:
                    LOGGER.warning(
                        "Worker completion handler failed agent_id=%s error_type=%s",
                        record.agent_id,
                        type(exc).__name__,
                    )

    def _report(
        self, agent: PersistentAgent, arguments: Mapping[str, Any]
    ) -> dict[str, Any]:
        if set(arguments) != {"result"}:
            raise AgentControlError("agent.report requires exactly result")
        record = self.get(agent.agent_id)
        if record.role != AgentRole.WORKER:
            raise AgentControlError("Only worker agents can report delegated results")
        with self._lock:
            record.report = arguments["result"]
            record.report_revision += 1
            revision = record.report_revision
            record.updated_at = time.time()
            handler = self._report_handler
        LOGGER.info("Worker agent report received agent_id=%s", record.agent_id)
        if handler is not None:
            try:
                handler(record, revision)
            except Exception as exc:
                LOGGER.warning(
                    "Worker report handler failed agent_id=%s error_type=%s",
                    record.agent_id,
                    type(exc).__name__,
                )
        return {
            "accepted": True,
            "agent_id": record.agent_id,
            "report_revision": revision,
        }

    def _visible_records(self, agent: PersistentAgent) -> tuple[AgentRecord, ...]:
        records = self.records()
        if agent.agent_id == self.main_agent_id:
            return records
        return tuple(
            record
            for record in records
            if record.agent_id in {self.main_agent_id, agent.agent_id}
        )

    def _require_main(self, agent: PersistentAgent) -> None:
        if agent.agent_id != self.main_agent_id:
            raise AgentControlError(
                "Only the main agent can create or assign worker agents"
            )

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
            progress(
                ActivationProgress(
                    kind,
                    self.main_agent_id,
                    "worker-control",
                    message,
                    name=record.name,
                    result=result,
                )
            )
        except Exception as exc:
            LOGGER.warning(
                "Agent progress callback failed agent_id=%s error_type=%s",
                record.agent_id,
                type(exc).__name__,
            )


def _required_string(arguments: Mapping[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise AgentControlError(
            f"Agent control argument {name!r} must be a non-empty string"
        )
    return value.strip()


def _recurrence(value: Any, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise AgentControlError(
                "Event-scoped tasks require recurrence='once', 'rising', or 'falling'; "
                "use once unless the user explicitly requested repeated occurrences"
            )
        return None
    if not required:
        raise AgentControlError("recurrence is only valid for event-scoped workers")
    if value not in {"once", "rising", "falling"}:
        raise AgentControlError("recurrence must be 'once', 'rising', or 'falling'")
    return str(value)


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise AgentControlError(f"Agent control argument {name!r} must be a boolean")
    return value


def _string_sequence(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise AgentControlError(
            f"Agent control argument {name!r} must be a string array"
        )
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise AgentControlError(
            f"Agent control argument {name!r} must contain non-empty strings"
        )
    return tuple(dict.fromkeys(item.strip() for item in value))


__all__ = [
    "AgentControl",
    "AgentControlError",
    "AgentRecord",
    "AgentRole",
    "AgentState",
]
