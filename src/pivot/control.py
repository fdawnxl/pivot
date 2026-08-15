"""Shared application control surface for local and remote clients."""

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
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .models import Message, ToolCall, normalize_content
from .mailbox import MainAgentMailbox
from .session import CancellationToken, ConversationSession, ProgressCallback, SessionCancelled

if TYPE_CHECKING:
    from .runtime import Runtime

LOGGER = logging.getLogger(__name__)


class ControlError(RuntimeError):
    """Raised when a control operation is invalid or cannot be completed."""


class ControlTaskState(StrEnum):
    """Lifecycle state for work submitted through the control surface."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ControlOperation:
    """One remotely invokable application operation."""

    name: str
    description: str
    handler: Callable[[Mapping[str, Any], CancellationToken], Any]

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "description": self.description}


@dataclass(slots=True)
class ControlTask:
    """Observable execution record for one asynchronous control request."""

    task_id: str
    operation: str
    arguments: dict[str, Any]
    state: ControlTaskState
    created_at: float
    updated_at: float
    cancellation: CancellationToken
    session_id: str | None = None
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
            "session_id": self.session_id,
            "queue_sequence": self.queue_sequence,
            "result": self.result,
            "error": self.error,
        }


ControlListener = Callable[[str, Mapping[str, Any]], None]


class PivotControl:
    """Control pivot through a stable operation registry and asynchronous tasks."""

    def __init__(
        self,
        runtime: Runtime,
        *,
        selected_session: ConversationSession | None = None,
        max_workers: int = 4,
        task_limit: int = 256,
    ) -> None:
        if max_workers < 1 or task_limit < 1:
            raise ValueError("Control worker and task limits must be positive")
        self.runtime = runtime
        self.max_workers = max_workers
        self.task_limit = task_limit
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="pivot-control")
        self._operations: dict[str, ControlOperation] = {}
        self._tasks: OrderedDict[str, ControlTask] = OrderedDict()
        self._listeners: set[ControlListener] = set()
        self._active_cancellations: dict[str, set[CancellationToken]] = {}
        self._main_mailbox = MainAgentMailbox()
        main_agent = runtime.agents.main_agent if runtime.agents is not None else None
        initial_session = selected_session or main_agent
        self._selected_session_id = initial_session.session_id if initial_session else None
        self._lock = threading.RLock()
        self._closed = False
        self._register_builtin_operations()

    @property
    def selected_session_id(self) -> str | None:
        with self._lock:
            return self._selected_session_id

    def subscribe(self, listener: ControlListener) -> Callable[[], None]:
        """Subscribe to task and session events and return an unsubscribe callback."""

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
        """Register an operation without changing the transport ABI."""

        if not name or not all(part.replace("_", "").isalnum() for part in name.split(".")):
            raise ControlError(f"Invalid control operation name: {name!r}")
        with self._lock:
            if name in self._operations:
                raise ControlError(f"Control operation is already registered: {name}")
            self._operations[name] = ControlOperation(name, description, handler)

    def operations(self) -> tuple[ControlOperation, ...]:
        with self._lock:
            return tuple(self._operations[name] for name in sorted(self._operations))

    def create_session(self, *, select: bool = True) -> ConversationSession:
        session = self.runtime.agents.main_agent if self.runtime.agents is not None else self.runtime.sessions.create()
        if select:
            self.select_session(session.session_id)
        self._emit("session_created", self.session_snapshot(session.session_id))
        return session

    def get_session(self, session_id: str | None = None) -> ConversationSession:
        resolved = session_id or self.selected_session_id
        if self.runtime.agents is not None:
            main_agent = self.runtime.agents.main_agent
            if resolved is None or resolved == main_agent.session_id:
                return main_agent
            raise ControlError("User messages can only target the main agent")
        if not resolved:
            raise ControlError("No conversation is selected")
        try:
            return self.runtime.sessions.get(resolved)
        except ValueError as exc:
            raise ControlError(str(exc)) from exc

    def select_session(self, session_id: str) -> ConversationSession:
        session = self.get_session(session_id)
        with self._lock:
            changed = self._selected_session_id != session.session_id
            self._selected_session_id = session.session_id
        if changed:
            self._emit("session_selected", self.session_snapshot(session.session_id))
        return session

    def sessions(self) -> tuple[ConversationSession, ...]:
        if self.runtime.agents is not None:
            return (self.runtime.agents.main_agent,)
        return self.runtime.sessions.available_sessions()

    def session_snapshot(self, session_id: str | None = None) -> dict[str, Any]:
        session = self.get_session(session_id)
        return {
            "session_id": session.session_id,
            "state": session.state,
            "last_active_at": session.last_active_at,
            "selected": session.session_id == self.selected_session_id,
            "messages": len(session.history),
        }

    def list_sessions(self) -> list[dict[str, Any]]:
        return [self.session_snapshot(session.session_id) for session in self.sessions()]

    def history(self, session_id: str | None = None) -> list[dict[str, Any]]:
        return [_message_dict(message) for message in self.get_session(session_id).history]

    def submit(self, operation: str, arguments: Mapping[str, Any] | None = None) -> str:
        """Queue a registered operation and return its task UUID immediately."""

        with self._lock:
            if self._closed:
                raise ControlError("Control service is closed")
            registered = self._operations.get(operation)
            if registered is None:
                raise ControlError(f"Unknown control operation: {operation}")
        values = dict(arguments or {})
        queue_sequence: int | None = None
        if operation == "session.send":
            values["session_id"] = self.get_session(_optional_session_id(values)).session_id
            queue_sequence = self._main_mailbox.issue()
        try:
            json.dumps(values, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ControlError("Control operation arguments must be JSON serializable") from exc
        now = time.time()
        task = ControlTask(
            str(uuid4()),
            operation,
            values,
            ControlTaskState.QUEUED,
            now,
            now,
            CancellationToken(),
            session_id=_optional_session_id(values) if operation == "session.send" else None,
            queue_sequence=queue_sequence,
        )
        with self._lock:
            self._prune_tasks()
            self._tasks[task.task_id] = task
            start_gate = threading.Event()
            task.future = self._executor.submit(self._run_task_after, start_gate, task, registered)
        self._emit("task_changed", task.as_dict())
        start_gate.set()
        return task.task_id

    def submit_message(self, message: Any, *, session_id: str | None = None) -> str:
        if isinstance(message, str) and not message.strip():
            raise ControlError("Message must not be empty")
        if not isinstance(message, str):
            try:
                json.dumps(message, ensure_ascii=False)
            except (TypeError, ValueError) as exc:
                raise ControlError("Message content must be JSON serializable") from exc
        resolved = self.get_session(session_id).session_id
        return self.submit("session.send", {"session_id": resolved, "message": message})

    def run(
        self,
        session_id: str,
        user_input: Any,
        *,
        progress: ProgressCallback | None = None,
        cancellation: CancellationToken | None = None,
    ) -> str:
        """Run a turn locally while making it visible to remote cancellation."""

        session = self.get_session(session_id)
        token = cancellation or CancellationToken()
        sequence = self._main_mailbox.issue()
        return self._run_queued(
            session,
            user_input,
            token,
            sequence,
            progress=progress,
        )

    def _run_queued(
        self,
        session: ConversationSession,
        user_input: Any,
        token: CancellationToken,
        sequence: int,
        *,
        progress: ProgressCallback | None = None,
        task: ControlTask | None = None,
    ) -> str:
        """Run one main-agent request at its reserved FIFO position."""

        with self._lock:
            self._active_cancellations.setdefault(session.session_id, set()).add(token)
        try:
            return self._main_mailbox.execute(
                sequence,
                token,
                lambda: self.runtime.sessions.run(
                    session.session_id,
                    user_input,
                    progress=progress,
                    cancellation=token,
                ),
                on_started=(lambda: self._mark_task_running(task)) if task is not None else None,
            )
        finally:
            with self._lock:
                active = self._active_cancellations.get(session.session_id)
                if active is not None:
                    active.discard(token)
                    if not active:
                        self._active_cancellations.pop(session.session_id, None)

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
        future = task.future
        if future is not None:
            try:
                future.result(timeout=timeout)
            except SessionCancelled:
                pass
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

    def cancel_session(self, session_id: str | None = None) -> bool:
        resolved = self.get_session(session_id).session_id
        cancelled = False
        with self._lock:
            active = tuple(self._active_cancellations.get(resolved, ()))
        for cancellation in active:
            cancellation.cancel()
            cancelled = True
        for task in self.tasks():
            if task.session_id == resolved and task.state in {ControlTaskState.QUEUED, ControlTaskState.RUNNING}:
                cancelled = self.cancel_task(task.task_id) or cancelled
        return cancelled

    def runtime_snapshot(self) -> dict[str, Any]:
        config = self.runtime.config
        return {
            "provider": config.provider.name,
            "model": config.provider.model,
            "instance_path": str(config.instance_path),
            "selected_session_id": self.selected_session_id,
            "capabilities": len(self.runtime.registry.descriptors()),
            "events": len(self.runtime.events.descriptors()),
            "executors": len(self.runtime.executors.descriptors()) if self.runtime.executors else 0,
            "agents": len(self.runtime.agents.records()) if self.runtime.agents else 0,
            "main_agent_id": self.runtime.agents.main_agent_id if self.runtime.agents else None,
            "dependencies": len(self.runtime.dependencies.descriptors()) if self.runtime.dependencies else 0,
        }

    def request_shutdown(self) -> None:
        """Ask the owning application to stop through the shared event channel."""

        self._emit("shutdown_requested", {"requested_at": time.time()})

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        for task in self.tasks():
            if task.state in {ControlTaskState.QUEUED, ControlTaskState.RUNNING}:
                self.cancel_task(task.task_id)
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _run_task_after(
        self,
        start_gate: threading.Event,
        task: ControlTask,
        operation: ControlOperation,
    ) -> None:
        start_gate.wait()
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
            if task.operation == "session.send":
                result = self._run_message_task(task)
            else:
                result = operation.handler(task.arguments, task.cancellation)
            json.dumps(result, ensure_ascii=False)
        except SessionCancelled:
            self._finish_task(task, ControlTaskState.CANCELLED)
        except InterruptedError as exc:
            if task.cancellation.is_cancelled():
                self._finish_task(task, ControlTaskState.CANCELLED)
            else:
                self._finish_task(task, ControlTaskState.FAILED, error=f"InterruptedError: {exc}")
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
        session = self.get_session(_optional_session_id(task.arguments))
        if "message" not in task.arguments:
            raise ControlError("message is required")
        try:
            message = normalize_content(task.arguments["message"])
        except (TypeError, ValueError) as exc:
            raise ControlError(f"message is invalid: {exc}") from exc
        response = self._run_queued(
            session,
            message,
            task.cancellation,
            task.queue_sequence,
            task=task,
        )
        return {"session_id": session.session_id, "response": response}

    def _mark_task_running(self, task: ControlTask) -> None:
        with self._lock:
            if task.state == ControlTaskState.CANCELLED:
                raise SessionCancelled("Main-agent request was cancelled while queued")
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
        self.register_operation("runtime.get", lambda _args, _cancel: self.runtime_snapshot(), description="Read runtime metadata.")
        self.register_operation("runtime.shutdown", self._op_shutdown, description="Request an orderly application shutdown.")
        self.register_operation("session.create", self._op_create_session, description="Create and optionally select a conversation.")
        self.register_operation("session.select", self._op_select_session, description="Select a conversation for implicit operations.")
        self.register_operation("session.list", lambda _args, _cancel: self.list_sessions(), description="List conversations.")
        self.register_operation("session.get", self._op_get_session, description="Read conversation metadata.")
        self.register_operation("session.history", self._op_history, description="Read conversation history.")
        self.register_operation("session.send", self._op_send, description="Send a message and run the conversation turn.")
        self.register_operation("session.cancel", self._op_cancel_session, description="Interrupt active controlled work for a conversation.")
        self.register_operation("capability.list", self._op_list_capabilities, description="List available capabilities.")
        self.register_operation("capability.execute", self._op_execute_capability, description="Execute a registered capability.")
        self.register_operation("event.list", self._op_list_events, description="List available event sources.")
        self.register_operation("event.wait", self._op_wait_event, description="Wait for an event condition.")
        self.register_operation("executor.list", self._op_list_executors, description="List execution backends.")
        self.register_operation("executor.execute", self._op_execute_executor, description="Execute one backend request.")
        if self.runtime.agents is not None:
            self.register_operation("agent.list", self._op_list_agents, description="List the main and delegated agents.")
            self.register_operation("agent.get", self._op_get_agent, description="Read one agent state and report.")
            self.register_operation("agent.create", self._op_create_agent, description="Create a scoped worker agent.")
            self.register_operation("agent.assign", self._op_assign_agent, description="Assign a task to a worker agent.")
            self.register_operation("agent.delegate", self._op_delegate_agent, description="Create and run a scoped worker agent.")
        self.register_operation("dependency.list", self._op_list_dependencies, description="List dependency status.")
        self.register_operation("dependency.refresh", self._op_refresh_dependencies, description="Refresh dependency status.")
        self.register_operation("dependency.start", self._op_start_dependency, description="Start one dependency.")
        self.register_operation("dependency.stop", self._op_stop_dependency, description="Stop one dependency.")

    def _op_create_session(self, arguments: Mapping[str, Any], _cancel: CancellationToken) -> dict[str, Any]:
        select = arguments.get("select", True)
        if not isinstance(select, bool):
            raise ControlError("session.create select must be a boolean")
        return self.session_snapshot(self.create_session(select=select).session_id)

    def _op_select_session(self, arguments: Mapping[str, Any], _cancel: CancellationToken) -> dict[str, Any]:
        return self.session_snapshot(self.select_session(_required_string(arguments, "session_id")).session_id)

    def _op_get_session(self, arguments: Mapping[str, Any], _cancel: CancellationToken) -> dict[str, Any]:
        return self.session_snapshot(_optional_session_id(arguments))

    def _op_history(self, arguments: Mapping[str, Any], _cancel: CancellationToken) -> list[dict[str, Any]]:
        return self.history(_optional_session_id(arguments))

    def _op_send(self, arguments: Mapping[str, Any], cancellation: CancellationToken) -> dict[str, Any]:
        session = self.get_session(_optional_session_id(arguments))
        if "message" not in arguments:
            raise ControlError("message is required")
        try:
            message = normalize_content(arguments["message"])
        except (TypeError, ValueError) as exc:
            raise ControlError(f"message is invalid: {exc}") from exc
        response = self.run(session.session_id, message, cancellation=cancellation)
        return {"session_id": session.session_id, "response": response}

    def _op_cancel_session(self, arguments: Mapping[str, Any], _cancel: CancellationToken) -> dict[str, Any]:
        session_id = _optional_session_id(arguments)
        return {"session_id": self.get_session(session_id).session_id, "cancelled": self.cancel_session(session_id)}

    def _op_list_capabilities(self, _arguments: Mapping[str, Any], _cancel: CancellationToken) -> list[dict[str, Any]]:
        return [item.as_prompt_dict() for item in self.runtime.registry.descriptors()]

    def _op_execute_capability(self, arguments: Mapping[str, Any], _cancel: CancellationToken) -> Any:
        name = _required_string(arguments, "name")
        values = arguments.get("arguments", {})
        if not isinstance(values, Mapping):
            raise ControlError("capability.execute arguments must be an object")
        return self.runtime.registry.execute(ToolCall(name, dict(values)))

    def _op_list_events(self, _arguments: Mapping[str, Any], _cancel: CancellationToken) -> list[dict[str, Any]]:
        return [item.as_prompt_dict() for item in self.runtime.events.descriptors()]

    def _op_wait_event(self, arguments: Mapping[str, Any], cancellation: CancellationToken) -> dict[str, Any]:
        event = _required_string(arguments, "event")
        operator = _required_string(arguments, "operator")
        timeout = arguments.get("timeout")
        if not isinstance(timeout, (int, float)):
            raise ControlError("event.wait timeout must be a number")
        notification = self.runtime.event_service.wait(
            session_id=self.get_session(_optional_session_id(arguments)).session_id,
            event=event,
            operator=operator,
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
        name = _required_string(arguments, "name")
        values = arguments.get("arguments", {})
        if not isinstance(values, Mapping):
            raise ControlError("executor.execute arguments must be an object")
        return self.runtime.executors.execute(name, values)

    def _op_list_agents(self, _arguments: Mapping[str, Any], _cancel: CancellationToken) -> list[dict[str, Any]]:
        agents = self._agent_control()
        return [item.as_dict() for item in agents.records()]

    def _op_get_agent(self, arguments: Mapping[str, Any], _cancel: CancellationToken) -> dict[str, Any]:
        return self._agent_control().get(_required_string(arguments, "agent_id")).as_dict()

    def _op_create_agent(self, arguments: Mapping[str, Any], cancellation: CancellationToken) -> Any:
        return self._agent_control().invoke_main("agent.create", arguments, cancellation=cancellation)

    def _op_assign_agent(self, arguments: Mapping[str, Any], cancellation: CancellationToken) -> Any:
        return self._agent_control().invoke_main("agent.assign", arguments, cancellation=cancellation)

    def _op_delegate_agent(self, arguments: Mapping[str, Any], cancellation: CancellationToken) -> Any:
        return self._agent_control().invoke_main("agent.delegate", arguments, cancellation=cancellation)

    def _agent_control(self) -> Any:
        if self.runtime.agents is None:
            raise ControlError("Agent control service is not available")
        return self.runtime.agents

    def _op_shutdown(self, _arguments: Mapping[str, Any], _cancel: CancellationToken) -> dict[str, bool]:
        self.request_shutdown()
        return {"requested": True}

    def _op_list_dependencies(self, _arguments: Mapping[str, Any], _cancel: CancellationToken) -> list[dict[str, Any]]:
        manager = self.runtime.dependencies
        return [_dependency_dict(item) for item in manager.statuses()] if manager else []

    def _op_refresh_dependencies(self, _arguments: Mapping[str, Any], _cancel: CancellationToken) -> list[dict[str, Any]]:
        manager = self.runtime.dependencies
        return [_dependency_dict(item) for item in manager.statuses(refresh=True)] if manager else []

    def _op_start_dependency(self, arguments: Mapping[str, Any], _cancel: CancellationToken) -> dict[str, Any]:
        manager = self.runtime.dependencies
        if manager is None:
            raise ControlError("Dependency manager is not available")
        return _dependency_dict(manager.start(_required_string(arguments, "dependency_id")))

    def _op_stop_dependency(self, arguments: Mapping[str, Any], _cancel: CancellationToken) -> dict[str, Any]:
        manager = self.runtime.dependencies
        if manager is None:
            raise ControlError("Dependency manager is not available")
        dependency_id = _required_string(arguments, "dependency_id")
        return {"dependency_id": dependency_id, "stopped": manager.stop(dependency_id)}


def _required_string(arguments: Mapping[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ControlError(f"{name} must be a non-empty string")
    return value


def _optional_session_id(arguments: Mapping[str, Any]) -> str | None:
    value = arguments.get("session_id")
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ControlError("session_id must be a string")
    return value


def _message_dict(message: Message) -> dict[str, Any]:
    return message.as_dict()


def _dependency_dict(status: Any) -> dict[str, Any]:
    return {
        "dependency_id": status.dependency_id,
        "state": status.state,
        "message": status.message,
        "details": status.details,
    }


__all__ = [
    "ControlError",
    "ControlOperation",
    "ControlTask",
    "ControlTaskState",
    "PivotControl",
]
