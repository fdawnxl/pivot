"""Application control surface for one persistent global agent."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from .activation import AgentCancelled, CancellationToken, PersistentAgent, ProgressCallback
from .mailbox import MainAgentMailbox
from .memory import MemoryService
from .models import Message, ToolCall, normalize_content

if TYPE_CHECKING:
    from .runtime import Runtime

LOGGER = logging.getLogger(__name__)


class ControlError(RuntimeError):
    """Raised when a control operation is invalid or unavailable."""


class ControlTaskState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ControlOperation:
    name: str
    description: str
    handler: Callable[[Mapping[str, Any], CancellationToken], Any]

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "description": self.description}


@dataclass(slots=True)
class ControlTask:
    task_id: str
    operation: str
    arguments: dict[str, Any]
    state: ControlTaskState
    created_at: float
    updated_at: float
    cancellation: CancellationToken
    agent_id: str | None = None
    queue_sequence: int | None = None
    result: Any = None
    error: str | None = None
    future: Future[Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "operation": self.operation,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "agent_id": self.agent_id,
            "queue_sequence": self.queue_sequence,
            "result": self.result,
            "error": self.error,
        }


ControlListener = Callable[[str, Mapping[str, Any]], None]


class PivotControl:
    """Serialize main-agent input while exposing independent runtime operations."""

    def __init__(self, runtime: Runtime, *, max_workers: int = 4, task_limit: int = 256) -> None:
        if max_workers < 1 or task_limit < 1:
            raise ValueError("Control worker and task limits must be positive")
        self.runtime = runtime
        self.task_limit = task_limit
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="pivot-control")
        self._operations: dict[str, ControlOperation] = {}
        self._tasks: OrderedDict[str, ControlTask] = OrderedDict()
        self._listeners: set[ControlListener] = set()
        self._active_cancellations: set[CancellationToken] = set()
        self._main_mailbox = MainAgentMailbox()
        self._memory_service = MemoryService(runtime.memory)
        self._lock = threading.RLock()
        self._closed = False
        runtime.agents.set_completion_handler(self._on_worker_completion)
        self._register_builtin_operations()

    def subscribe(self, listener: ControlListener) -> Callable[[], None]:
        with self._lock:
            self._listeners.add(listener)

        def unsubscribe() -> None:
            with self._lock:
                self._listeners.discard(listener)

        return unsubscribe

    def register_operation(
        self,
        name: str,
        handler: Callable[[Mapping[str, Any], CancellationToken], Any],
        *,
        description: str,
    ) -> None:
        if not name or not all(part.replace("_", "").isalnum() for part in name.split(".")):
            raise ControlError(f"Invalid control operation name: {name!r}")
        with self._lock:
            if name in self._operations:
                raise ControlError(f"Control operation is already registered: {name}")
            self._operations[name] = ControlOperation(name, description, handler)

    def operations(self) -> tuple[ControlOperation, ...]:
        with self._lock:
            return tuple(self._operations[name] for name in sorted(self._operations))

    def main_snapshot(self) -> dict[str, Any]:
        agent = self.runtime.main_agent
        return {
            "agent_id": agent.agent_id,
            "name": agent.name,
            "role": agent.role,
            "state": agent.state,
            "last_active_at": agent.last_active_at,
            "messages": len(agent.history),
        }

    def history(self) -> list[dict[str, Any]]:
        return [message.as_dict() for message in self.runtime.main_agent.history]

    def submit(self, operation: str, arguments: Mapping[str, Any] | None = None) -> str:
        with self._lock:
            if self._closed:
                raise ControlError("Control service is closed")
            registered = self._operations.get(operation)
            if registered is None:
                raise ControlError(f"Unknown control operation: {operation}")
        values = dict(arguments or {})
        try:
            json.dumps(values, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ControlError("Control operation arguments must be JSON serializable") from exc
        sequence = self._main_mailbox.issue() if operation == "agent.message" else None
        now = time.time()
        task = ControlTask(
            str(uuid4()),
            operation,
            values,
            ControlTaskState.QUEUED,
            now,
            now,
            CancellationToken(),
            agent_id=self.runtime.main_agent.agent_id if operation == "agent.message" else None,
            queue_sequence=sequence,
        )
        with self._lock:
            self._prune_tasks()
            self._tasks[task.task_id] = task
            gate = threading.Event()
            task.future = self._executor.submit(self._run_task_after, gate, task, registered)
        self._emit("task_changed", task.as_dict())
        gate.set()
        return task.task_id

    def submit_message(self, message: Any) -> str:
        if isinstance(message, str) and not message.strip():
            raise ControlError("Message must not be empty")
        return self.submit("agent.message", {"message": message})

    def run_main(
        self,
        stimulus: Any,
        *,
        progress: ProgressCallback | None = None,
        cancellation: CancellationToken | None = None,
    ) -> str:
        token = cancellation or CancellationToken()
        return self._run_queued(
            self.runtime.main_agent,
            stimulus,
            token,
            self._main_mailbox.issue(),
            progress=progress,
        )

    def _run_queued(
        self,
        agent: PersistentAgent,
        stimulus: Any,
        token: CancellationToken,
        sequence: int,
        *,
        source: str = "user",
        progress: ProgressCallback | None = None,
        task: ControlTask | None = None,
    ) -> str:
        with self._lock:
            self._active_cancellations.add(token)
        try:
            return self._main_mailbox.execute(
                sequence,
                token,
                lambda: agent.activate(stimulus, source=source, progress=progress, cancellation=token),
                on_started=(lambda: self._mark_task_running(task)) if task is not None else None,
            )
        finally:
            with self._lock:
                self._active_cancellations.discard(token)

    def _on_worker_completion(self, record: Any) -> None:
        payload = {
            "type": "worker_completion",
            "agent": record.as_dict(),
            "instruction": "Review this outcome and update the user if it changes the result.",
        }
        try:
            self.submit(
                "agent.message",
                {
                    "message": "pivot internal worker completion:\n"
                    + json.dumps(payload, ensure_ascii=False, default=str),
                    "_source": "worker",
                },
            )
        except ControlError:
            LOGGER.info("Worker completion arrived after control shutdown agent_id=%s", record.agent_id)

    def task(self, task_id: str) -> ControlTask:
        with self._lock:
            task = self._tasks.get(task_id)
        if task is None:
            raise ControlError(f"Unknown control task: {task_id}")
        return task

    def tasks(self) -> tuple[ControlTask, ...]:
        with self._lock:
            return tuple(self._tasks.values())

    def wait_task(self, task_id: str, *, timeout: float | None = None) -> ControlTask:
        task = self.task(task_id)
        if task.future is not None:
            try:
                task.future.result(timeout=timeout)
            except Exception:
                pass
        return self.task(task_id)

    def cancel_task(self, task_id: str) -> bool:
        task = self.task(task_id)
        with self._lock:
            if task.state in {ControlTaskState.COMPLETED, ControlTaskState.FAILED, ControlTaskState.CANCELLED}:
                return False
            task.cancellation.cancel()
            if task.state == ControlTaskState.QUEUED:
                task.state = ControlTaskState.CANCELLED
                task.updated_at = time.time()
                if task.queue_sequence is not None:
                    self._main_mailbox.skip(task.queue_sequence)
                if task.future is not None:
                    task.future.cancel()
        self._emit("task_changed", task.as_dict())
        return True

    def interrupt_main(self) -> bool:
        cancelled = False
        with self._lock:
            active = tuple(self._active_cancellations)
        for token in active:
            token.cancel()
            cancelled = True
        for task in self.tasks():
            if task.agent_id == self.runtime.main_agent.agent_id and task.state in {
                ControlTaskState.QUEUED,
                ControlTaskState.RUNNING,
            }:
                cancelled = self.cancel_task(task.task_id) or cancelled
        return self.runtime.agents.cancel_workers() or cancelled

    def runtime_snapshot(self) -> dict[str, Any]:
        config = self.runtime.config
        return {
            "provider": config.provider.name,
            "model": config.provider.model,
            "instance_path": str(config.instance_path),
            "main_agent_id": self.runtime.main_agent.agent_id,
            "capabilities": len(self.runtime.registry.descriptors()),
            "events": len(self.runtime.events.descriptors()),
            "executors": len(self.runtime.executors.descriptors()) if self.runtime.executors else 0,
            "agents": len(self.runtime.agents.records()),
            "dependencies": len(self.runtime.dependencies.descriptors()) if self.runtime.dependencies else 0,
        }

    def request_shutdown(self) -> None:
        self._emit("shutdown_requested", {"requested_at": time.time()})

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.runtime.agents.set_completion_handler(None)
        for task in self.tasks():
            if task.state in {ControlTaskState.QUEUED, ControlTaskState.RUNNING}:
                self.cancel_task(task.task_id)
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _run_task_after(self, gate: threading.Event, task: ControlTask, operation: ControlOperation) -> None:
        gate.wait()
        self._run_task(task, operation)

    def _run_task(self, task: ControlTask, operation: ControlOperation) -> None:
        with self._lock:
            if task.state == ControlTaskState.CANCELLED:
                return
            if task.cancellation.is_cancelled():
                task.state = ControlTaskState.CANCELLED
                task.updated_at = time.time()
                cancelled = True
            else:
                if task.queue_sequence is None:
                    task.state = ControlTaskState.RUNNING
                    task.updated_at = time.time()
                cancelled = False
        self._emit("task_changed", task.as_dict())
        if cancelled:
            return
        try:
            result = self._run_message_task(task) if task.operation == "agent.message" else operation.handler(
                task.arguments, task.cancellation
            )
            json.dumps(result, ensure_ascii=False)
        except AgentCancelled:
            self._finish_task(task, ControlTaskState.CANCELLED)
        except InterruptedError as exc:
            state = ControlTaskState.CANCELLED if task.cancellation.is_cancelled() else ControlTaskState.FAILED
            self._finish_task(task, state, error=None if state == ControlTaskState.CANCELLED else f"InterruptedError: {exc}")
        except Exception as exc:
            LOGGER.error(
                "Control operation failed operation=%s task_id=%s error_type=%s",
                task.operation,
                task.task_id,
                type(exc).__name__,
            )
            self._finish_task(task, ControlTaskState.FAILED, error=f"{type(exc).__name__}: {exc}")
        else:
            self._finish_task(task, ControlTaskState.COMPLETED, result=result)

    def _run_message_task(self, task: ControlTask) -> dict[str, Any]:
        if task.queue_sequence is None:
            raise ControlError("Main-agent request has no FIFO sequence")
        if "message" not in task.arguments:
            raise ControlError("message is required")
        try:
            message = normalize_content(task.arguments["message"])
        except (TypeError, ValueError) as exc:
            raise ControlError(f"message is invalid: {exc}") from exc
        source = task.arguments.get("_source", "user")
        if source not in {"user", "worker", "event", "system"}:
            raise ControlError("Invalid main-agent stimulus source")
        response = self._run_queued(
            self.runtime.main_agent,
            message,
            task.cancellation,
            task.queue_sequence,
            source=source,
            task=task,
        )
        return {"agent_id": self.runtime.main_agent.agent_id, "response": response}

    def _mark_task_running(self, task: ControlTask) -> None:
        with self._lock:
            if task.state == ControlTaskState.CANCELLED:
                raise AgentCancelled("Main-agent request was cancelled while queued")
            task.state = ControlTaskState.RUNNING
            task.updated_at = time.time()
        self._emit("task_changed", task.as_dict())

    def _finish_task(
        self,
        task: ControlTask,
        state: ControlTaskState,
        *,
        result: Any = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            task.state = state
            task.result = result
            task.error = error
            task.updated_at = time.time()
        self._emit("task_changed", task.as_dict())

    def _emit(self, event: str, payload: Mapping[str, Any]) -> None:
        with self._lock:
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener(event, payload)
            except Exception as exc:
                LOGGER.warning("Control listener failed event=%s error_type=%s", event, type(exc).__name__)

    def _prune_tasks(self) -> None:
        while len(self._tasks) >= self.task_limit:
            removable = next(
                (
                    task_id
                    for task_id, task in self._tasks.items()
                    if task.state in {ControlTaskState.COMPLETED, ControlTaskState.FAILED, ControlTaskState.CANCELLED}
                ),
                None,
            )
            if removable is None:
                raise ControlError("Control task limit reached while all tasks are active")
            del self._tasks[removable]

    def _register_builtin_operations(self) -> None:
        self.register_operation("runtime.get", lambda _a, _c: self.runtime_snapshot(), description="Read runtime metadata.")
        self.register_operation("runtime.shutdown", self._op_shutdown, description="Request orderly shutdown.")
        self.register_operation("agent.main", lambda _a, _c: self.main_snapshot(), description="Read the main agent.")
        self.register_operation("agent.history", lambda _a, _c: self.history(), description="Read the main timeline.")
        self.register_operation("agent.message", self._unused_message_handler, description="Submit a FIFO main-agent message.")
        self.register_operation("agent.interrupt", self._op_interrupt, description="Interrupt main-agent work.")
        self.register_operation("agent.list", self._op_list_agents, description="List main and worker agents.")
        self.register_operation("agent.get", self._op_get_agent, description="Read one agent.")
        self.register_operation("agent.create", self._op_create_agent, description="Create a scoped worker.")
        self.register_operation("agent.assign", self._op_assign_agent, description="Assign a worker asynchronously.")
        self.register_operation("agent.delegate", self._op_delegate_agent, description="Create and assign a worker.")
        self.register_operation("capability.list", self._op_list_capabilities, description="List capabilities.")
        self.register_operation("capability.execute", self._op_execute_capability, description="Execute a capability.")
        self.register_operation("event.list", self._op_list_events, description="List events.")
        self.register_operation("event.wait", self._op_wait_event, description="Wait for an event outside the main agent.")
        self.register_operation("executor.list", self._op_list_executors, description="List executors.")
        self.register_operation("executor.execute", self._op_execute_executor, description="Execute a backend request.")
        self.register_operation("memory.remember", self._op_memory_remember, description="Store sourced memory.")
        self.register_operation("memory.recall", self._op_memory_recall, description="Recall relevant memory.")
        self.register_operation("memory.forget", self._op_memory_forget, description="Forget one memory record.")
        self.register_operation("dependency.list", self._op_list_dependencies, description="List dependencies.")
        self.register_operation("dependency.refresh", self._op_refresh_dependencies, description="Refresh dependencies.")
        self.register_operation("dependency.start", self._op_start_dependency, description="Start a dependency.")
        self.register_operation("dependency.stop", self._op_stop_dependency, description="Stop a dependency.")

    def _unused_message_handler(self, _arguments: Mapping[str, Any], _cancel: CancellationToken) -> Any:
        raise ControlError("agent.message must run through the FIFO mailbox")

    def _op_shutdown(self, _arguments: Mapping[str, Any], _cancel: CancellationToken) -> dict[str, bool]:
        self.request_shutdown()
        return {"requested": True}

    def _op_interrupt(self, _arguments: Mapping[str, Any], _cancel: CancellationToken) -> dict[str, bool]:
        return {"cancelled": self.interrupt_main()}

    def _op_list_agents(self, _arguments: Mapping[str, Any], _cancel: CancellationToken) -> list[dict[str, Any]]:
        return [item.as_dict() for item in self.runtime.agents.records()]

    def _op_get_agent(self, arguments: Mapping[str, Any], _cancel: CancellationToken) -> dict[str, Any]:
        return self.runtime.agents.get(_required_string(arguments, "agent_id")).as_dict()

    def _op_create_agent(self, arguments: Mapping[str, Any], cancellation: CancellationToken) -> Any:
        return self.runtime.agents.invoke_main("agent.create", arguments, cancellation=cancellation)

    def _op_assign_agent(self, arguments: Mapping[str, Any], cancellation: CancellationToken) -> Any:
        return self.runtime.agents.invoke_main("agent.assign", arguments, cancellation=cancellation)

    def _op_delegate_agent(self, arguments: Mapping[str, Any], cancellation: CancellationToken) -> Any:
        return self.runtime.agents.invoke_main("agent.delegate", arguments, cancellation=cancellation)

    def _op_list_capabilities(self, _arguments: Mapping[str, Any], _cancel: CancellationToken) -> list[dict[str, Any]]:
        return [item.as_prompt_dict() for item in self.runtime.registry.descriptors()]

    def _op_execute_capability(self, arguments: Mapping[str, Any], _cancel: CancellationToken) -> Any:
        values = arguments.get("arguments", {})
        if not isinstance(values, Mapping):
            raise ControlError("capability.execute arguments must be an object")
        return self.runtime.registry.execute(ToolCall(_required_string(arguments, "name"), dict(values)))

    def _op_list_events(self, _arguments: Mapping[str, Any], _cancel: CancellationToken) -> list[dict[str, Any]]:
        return [item.as_prompt_dict() for item in self.runtime.events.descriptors()]

    def _op_wait_event(self, arguments: Mapping[str, Any], cancellation: CancellationToken) -> dict[str, Any]:
        timeout = arguments.get("timeout")
        if not isinstance(timeout, (int, float)):
            raise ControlError("event.wait timeout must be a number")
        notification = self.runtime.event_service.wait(
            agent_id=self.runtime.main_agent.agent_id,
            event=_required_string(arguments, "event"),
            operator=_required_string(arguments, "operator"),
            expected=arguments.get("expected"),
            timeout=float(timeout),
            is_cancelled=cancellation.is_cancelled,
        )
        return notification.as_dict()

    def _op_list_executors(self, _arguments: Mapping[str, Any], _cancel: CancellationToken) -> list[dict[str, Any]]:
        return [item.as_prompt_dict() for item in self.runtime.executors.descriptors()] if self.runtime.executors else []

    def _op_execute_executor(self, arguments: Mapping[str, Any], _cancel: CancellationToken) -> Any:
        if self.runtime.executors is None:
            raise ControlError("Executor service is not available")
        values = arguments.get("arguments", {})
        if not isinstance(values, Mapping):
            raise ControlError("executor.execute arguments must be an object")
        return self.runtime.executors.execute(_required_string(arguments, "name"), values)

    def _op_memory_remember(self, arguments: Mapping[str, Any], _cancel: CancellationToken) -> Any:
        return self._memory_service.execute(self.runtime.main_agent.agent_id, "memory.remember", arguments)

    def _op_memory_recall(self, arguments: Mapping[str, Any], _cancel: CancellationToken) -> Any:
        return self._memory_service.execute(self.runtime.main_agent.agent_id, "memory.recall", arguments)

    def _op_memory_forget(self, arguments: Mapping[str, Any], _cancel: CancellationToken) -> Any:
        return self._memory_service.execute(self.runtime.main_agent.agent_id, "memory.forget", arguments)

    def _op_list_dependencies(self, _arguments: Mapping[str, Any], _cancel: CancellationToken) -> list[dict[str, Any]]:
        manager = self.runtime.dependencies
        return [_dependency_dict(item) for item in manager.statuses()] if manager else []

    def _op_refresh_dependencies(self, _arguments: Mapping[str, Any], _cancel: CancellationToken) -> list[dict[str, Any]]:
        manager = self.runtime.dependencies
        return [_dependency_dict(item) for item in manager.statuses(refresh=True)] if manager else []

    def _op_start_dependency(self, arguments: Mapping[str, Any], _cancel: CancellationToken) -> dict[str, Any]:
        if self.runtime.dependencies is None:
            raise ControlError("Dependency manager is not available")
        return _dependency_dict(self.runtime.dependencies.start(_required_string(arguments, "dependency_id")))

    def _op_stop_dependency(self, arguments: Mapping[str, Any], _cancel: CancellationToken) -> dict[str, Any]:
        if self.runtime.dependencies is None:
            raise ControlError("Dependency manager is not available")
        dependency_id = _required_string(arguments, "dependency_id")
        return {"dependency_id": dependency_id, "stopped": self.runtime.dependencies.stop(dependency_id)}


def _required_string(arguments: Mapping[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ControlError(f"{name} must be a non-empty string")
    return value.strip()


def _dependency_dict(status: Any) -> dict[str, Any]:
    return {
        "dependency_id": status.dependency_id,
        "state": status.state,
        "message": status.message,
        "details": status.details,
    }


__all__ = ["ControlError", "ControlOperation", "ControlTask", "ControlTaskState", "PivotControl"]
