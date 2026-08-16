"""Bounded activations for persistent main and worker agents."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID, uuid4

from .actions import ActionDetector, ActionError, ActionKind, ActionRequest, action_tool
from .capabilities import CapabilityError, CapabilityRegistry
from .events import EVENT_WAIT_TOOL, EventError, EventPool, EventService
from .executors import ExecutorError, ExecutorRegistry
from .llm import LLMClient
from .logging import log_context
from .memory import ContextBuilder, MemoryError, MemoryService, RuntimeStore
from .models import Message, ToolCall, normalize_content
from .parser import parse_response

if TYPE_CHECKING:
    from .agents import AgentControl

LOGGER = logging.getLogger(__name__)


class AgentActivationError(RuntimeError):
    """Raised when a persistent agent activation cannot make progress."""


class AgentCancelled(AgentActivationError):
    """Raised when a caller cooperatively interrupts an activation."""


class CancellationToken:
    """Thread-safe cooperative cancellation signal."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()


class ActivationState(StrEnum):
    READY = "ready"
    RUNNING = "running"
    PENDING = "pending"


ProgressKind = Literal[
    "activation_started",
    "llm_waiting",
    "assistant_update",
    "capability_started",
    "capability_completed",
    "capability_failed",
    "event_wait_started",
    "event_completed",
    "executor_started",
    "executor_completed",
    "executor_failed",
    "control_started",
    "control_completed",
    "control_failed",
    "memory_started",
    "memory_completed",
    "memory_failed",
    "agent_started",
    "agent_progress",
    "agent_completed",
    "agent_failed",
    "activation_completed",
    "activation_cancelled",
    "activation_failed",
]


@dataclass(frozen=True, slots=True)
class ActivationProgress:
    kind: ProgressKind
    agent_id: str
    activation_id: str
    message: str
    round_number: int | None = None
    name: str | None = None
    result: Any = None


ProgressCallback = Callable[[ActivationProgress], None]


def normalize_agent_id(agent_id: str) -> str:
    try:
        return str(UUID(agent_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("agent_id must be a valid UUID") from exc


def _action_content(result: Any) -> str | tuple[dict[str, Any], ...]:
    candidate = result.get("content") if isinstance(result, dict) and "content" in result else result
    if isinstance(candidate, (list, tuple)):
        try:
            normalized = normalize_content(candidate)
        except (TypeError, ValueError):
            normalized = None
        if isinstance(normalized, tuple):
            return normalized
    return json.dumps(result, ensure_ascii=False, default=str)


class PersistentAgent:
    """Run finite activations while retaining identity and memory indefinitely."""

    def __init__(
        self,
        agent_id: str,
        *,
        llm: LLMClient,
        capabilities: CapabilityRegistry,
        memory: RuntimeStore,
        events: EventPool | None = None,
        event_service: EventService | None = None,
        event_names: Sequence[str] | None = None,
        executors: ExecutorRegistry | None = None,
        memory_service: MemoryService | None = None,
        context_builder: ContextBuilder | None = None,
        agent_control: AgentControl | None = None,
        role: Literal["main", "worker"] = "main",
        name: str = "main",
        max_rounds: int = 8,
    ) -> None:
        if max_rounds < 1:
            raise ValueError("max_rounds must be positive")
        self.agent_id = normalize_agent_id(agent_id)
        self.llm = llm
        self.capabilities = capabilities
        self.memory = memory
        self.events = events
        self.event_service = event_service
        self.event_names = tuple(dict.fromkeys(event_names)) if event_names is not None else None
        self.executors = executors
        self.memory_service = memory_service or MemoryService(memory)
        self.context_builder = context_builder or ContextBuilder(memory)
        self.agent_control = agent_control
        self.role = role
        self.name = name
        self.max_rounds = max_rounds
        self.action_detector = ActionDetector()
        self._state = ActivationState.READY
        self._last_active_at = time.time()
        self._state_lock = threading.Lock()
        self._activation_lock = threading.Lock()

    @property
    def state(self) -> ActivationState:
        with self._state_lock:
            return self._state

    @property
    def last_active_at(self) -> float:
        with self._state_lock:
            return self._last_active_at

    @property
    def history(self) -> tuple[Message, ...]:
        """Return the durable visible timeline; it is not the model context window."""

        return self.memory.messages(self.agent_id)

    def configure_control(self, control: AgentControl, *, executors: ExecutorRegistry | None = None) -> None:
        self.agent_control = control
        if executors is not None:
            self.executors = executors

    def wait_until_idle(self) -> None:
        """Wait until the current activation reaches its cleanup boundary."""

        self._activation_lock.acquire()
        self._activation_lock.release()

    def _set_state(self, state: ActivationState) -> None:
        with self._state_lock:
            self._state = state
            self._last_active_at = time.time()
        self.memory.update_agent_state(self.agent_id, state.value)

    def activate(
        self,
        stimulus: Any,
        *,
        source: str = "user",
        progress: ProgressCallback | None = None,
        cancellation: CancellationToken | None = None,
    ) -> str:
        try:
            normalized = normalize_content(stimulus)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"stimulus is invalid: {exc}") from exc
        if isinstance(normalized, str) and not normalized.strip():
            raise ValueError("stimulus must not be empty")
        if isinstance(normalized, tuple) and not normalized:
            raise ValueError("stimulus must not be empty")
        if not self._activation_lock.acquire(blocking=False):
            raise AgentActivationError("Agent is already active")
        token = cancellation or CancellationToken()
        activation_id = self.memory.create_activation(self.agent_id, source, normalized)
        input_role = "user" if source == "user" else "system"
        self.memory.append_message(self.agent_id, activation_id, Message(input_role, normalized))
        query = normalized if isinstance(normalized, str) else json.dumps(list(normalized), ensure_ascii=False, default=str)
        self._set_state(ActivationState.RUNNING)
        try:
            with log_context(correlation_id=activation_id, agent_id=self.agent_id):
                response = self._run_activation(
                    activation_id,
                    query,
                    progress=progress,
                    cancellation=token,
                )
        except AgentCancelled as exc:
            self.memory.finish_activation(activation_id, "cancelled", error=str(exc))
            raise
        except BaseException as exc:
            self.memory.finish_activation(
                activation_id,
                "failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        else:
            self.memory.finish_activation(activation_id, "completed", response=response)
            self.memory.record_episode(self.agent_id, activation_id, query, response)
            return response
        finally:
            self._set_state(ActivationState.READY)
            self._activation_lock.release()

    def _run_activation(
        self,
        activation_id: str,
        query: str,
        *,
        progress: ProgressCallback | None,
        cancellation: CancellationToken,
    ) -> str:
        self._raise_if_cancelled(activation_id, cancellation, progress)
        self._emit(progress, activation_id, "activation_started", "Activation started.")
        for round_number in range(1, self.max_rounds + 1):
            self._raise_if_cancelled(activation_id, cancellation, progress, round_number)
            self._emit(
                progress,
                activation_id,
                "llm_waiting",
                "Waiting for the model.",
                round_number=round_number,
            )
            try:
                tools = (action_tool(),) + self.capabilities.llm_tools()
                if self.event_service and (self.role == "worker" or self.agent_control is None):
                    tools += self.event_service.llm_tools(self.event_names)
                if self.executors:
                    tools += self.executors.llm_tools()
                messages = self.context_builder.build(
                    agent_id=self.agent_id,
                    activation_id=activation_id,
                    query=query,
                    runtime_context=self._runtime_context(),
                )
                raw = self.llm.complete(messages, tools=tools)
                self._raise_if_cancelled(activation_id, cancellation, progress, round_number)
                response = parse_response(raw)
                detected = self.action_detector.detect(
                    response,
                    capability_names=tuple(item.name for item in self.capabilities.descriptors()),
                    executor_tools=self.executors.tool_routes() if self.executors else {},
                )
            except AgentCancelled:
                raise
            except Exception as exc:
                LOGGER.exception("Agent activation failed agent_id=%s round=%d", self.agent_id, round_number)
                self._emit(
                    progress,
                    activation_id,
                    "activation_failed",
                    f"LLM round failed: {type(exc).__name__}.",
                    round_number=round_number,
                )
                raise AgentActivationError(
                    f"LLM round {round_number} failed: {type(exc).__name__}: {exc}"
                ) from exc
            self.memory.append_message(
                self.agent_id,
                activation_id,
                Message("assistant", detected.content, tool_calls=detected.calls),
            )
            if not detected.actions:
                self._emit(
                    progress,
                    activation_id,
                    "activation_completed",
                    "Activation completed.",
                    round_number=round_number,
                    result=detected.text,
                )
                return detected.text
            if detected.text:
                self._emit(
                    progress,
                    activation_id,
                    "assistant_update",
                    detected.text,
                    round_number=round_number,
                )
            for action in detected.actions:
                self._raise_if_cancelled(activation_id, cancellation, progress, round_number)
                try:
                    result = self._execute_action(
                        activation_id,
                        action,
                        progress=progress,
                        cancellation=cancellation,
                        round_number=round_number,
                    )
                    tool_content = _action_content(result)
                except AgentCancelled:
                    raise
                except (ActionError, CapabilityError, EventError, ExecutorError, MemoryError) as exc:
                    failed_kind: ProgressKind = {
                        ActionKind.EVENT: "event_completed",
                        ActionKind.EXECUTOR: "executor_failed",
                        ActionKind.CONTROL: "control_failed",
                        ActionKind.MEMORY: "memory_failed",
                    }.get(action.kind, "capability_failed")
                    self._emit(
                        progress,
                        activation_id,
                        failed_kind,
                        f"{action.name} failed: {exc}.",
                        round_number=round_number,
                        name=action.name,
                        result={"error": str(exc)},
                    )
                    tool_content = json.dumps({"error": str(exc)}, ensure_ascii=False)
                self.memory.append_message(
                    self.agent_id,
                    activation_id,
                    Message("tool", tool_content, name=action.call.name, tool_call_id=action.call.call_id),
                )
        self._emit(
            progress,
            activation_id,
            "activation_failed",
            f"Maximum rounds reached ({self.max_rounds}).",
        )
        raise AgentActivationError(f"Agent exceeded maximum LLM rounds ({self.max_rounds})")

    def _execute_action(
        self,
        activation_id: str,
        action: ActionRequest,
        *,
        progress: ProgressCallback | None,
        cancellation: CancellationToken,
        round_number: int,
    ) -> Any:
        if action.kind == ActionKind.CAPABILITY:
            self._emit_started(progress, activation_id, "capability", action, round_number)
            result = self.capabilities.execute(ToolCall(action.name, action.arguments, action.call.call_id))
            self._emit_completed(progress, activation_id, "capability", action, round_number, result)
            return result
        if action.kind == ActionKind.EVENT:
            if action.name not in {"wait", EVENT_WAIT_TOOL}:
                raise ActionError(f"Unknown event action: {action.name}")
            timeout = action.arguments.get("timeout")
            if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
                raise EventError("Event timeout must be a positive number")
            if self.role == "main" and self.agent_control is not None and timeout > 1:
                raise ActionError("Main-agent event waits over 1 second must be delegated to a worker")
            continuation_id = action.call.call_id or str(uuid4())
            self.memory.save_continuation(
                continuation_id,
                self.agent_id,
                "event",
                "waiting",
                action.arguments,
                deadline=time.time() + timeout,
            )
            self._set_state(ActivationState.PENDING)
            self._emit(
                progress,
                activation_id,
                "event_wait_started",
                f"Waiting for event {action.arguments.get('event', 'unknown')}.",
                round_number=round_number,
                name=action.name,
                result=action.arguments,
            )
            try:
                result = self._wait_for_event(action, cancellation)
            except BaseException:
                self.memory.save_continuation(
                    continuation_id,
                    self.agent_id,
                    "event",
                    "cancelled",
                    action.arguments,
                )
                raise
            finally:
                self._set_state(ActivationState.RUNNING)
            self.memory.save_continuation(
                continuation_id,
                self.agent_id,
                "event",
                "completed",
                action.arguments,
                result=result,
            )
            self._emit(
                progress,
                activation_id,
                "event_completed",
                f"Event wait finished with status {result.get('status', 'unknown')}.",
                round_number=round_number,
                name=action.name,
                result=result,
            )
            return result
        if action.kind == ActionKind.EXECUTOR:
            if self.executors is None:
                raise ActionError("Executor service is not available")
            self._emit_started(progress, activation_id, "executor", action, round_number)
            result = self.executors.execute(action.name, action.arguments)
            self._emit_completed(progress, activation_id, "executor", action, round_number, result)
            return result
        if action.kind == ActionKind.CONTROL:
            if self.agent_control is None:
                raise ActionError("Agent control service is not available")
            self._emit_started(progress, activation_id, "control", action, round_number)
            result = self.agent_control.execute(
                self,
                action.name,
                action.arguments,
                cancellation=cancellation,
                progress=progress,
            )
            self._emit_completed(progress, activation_id, "control", action, round_number, result)
            return result
        if action.kind == ActionKind.MEMORY:
            self._emit_started(progress, activation_id, "memory", action, round_number)
            result = self.memory_service.execute(self.agent_id, action.name, action.arguments)
            self._emit_completed(progress, activation_id, "memory", action, round_number, result)
            return result
        raise ActionError(f"Unsupported action kind: {action.kind}")

    def _wait_for_event(self, action: ActionRequest, cancellation: CancellationToken) -> dict[str, Any]:
        if self.event_service is None:
            raise EventError("Event service is not available")
        required = {"event", "operator", "expected", "timeout"}
        if set(action.arguments) != required:
            raise EventError(f"Event wait arguments must be exactly: {', '.join(sorted(required))}")
        event = action.arguments["event"]
        operator_name = action.arguments["operator"]
        timeout = action.arguments["timeout"]
        if not isinstance(event, str) or not isinstance(operator_name, str) or not isinstance(timeout, (int, float)):
            raise EventError("Event wait has invalid argument types")
        if self.event_names is not None and event not in self.event_names:
            raise EventError(f"Event is not assigned to this agent: {event}")
        try:
            notification = self.event_service.wait(
                agent_id=self.agent_id,
                event=event,
                operator=operator_name,
                expected=action.arguments["expected"],
                timeout=float(timeout),
                is_cancelled=cancellation.is_cancelled,
            )
        except InterruptedError as exc:
            raise AgentCancelled("Activation interrupted during event wait") from exc
        return notification.as_dict()

    def _runtime_context(self) -> dict[str, Any]:
        event_context: list[dict[str, Any]] = []
        if self.events:
            allowed = set(self.event_names) if self.event_names is not None else None
            event_context = [
                event.as_prompt_dict()
                for event in self.events.descriptors()
                if allowed is None or event.name in allowed
            ]
        if self.role == "main":
            instruction = (
                "You are the persistent device-wide main agent and the only agent that communicates with users. "
                "Delegate specialist work and event waits longer than one second. Delegation returns immediately; "
                "worker completion arrives later as a typed stimulus in the durable main inbox."
            )
        else:
            instruction = (
                "You are a delegated worker. Use only assigned resources, perform long waits when needed, and report "
                "a structured result with control agent.report."
            )
        return {
            "agent": {"id": self.agent_id, "name": self.name, "role": self.role},
            "capabilities": self.capabilities.prompt_context(),
            "events": event_context,
            "executors": self.executors.prompt_context() if self.executors else [],
            "control": self.agent_control.prompt_context(self) if self.agent_control else [],
            "memory": list(self.memory_service.prompt_context()),
            "action_protocol": {
                "preferred_tool": "pivot_action",
                "shape": {"kind": "capability|event|control|executor|memory", "name": "operation", "arguments": {}},
                "capability_kind_rule": "Use kind=capability for think, measure, and work capabilities.",
            },
            "instruction": instruction,
        }

    def _raise_if_cancelled(
        self,
        activation_id: str,
        cancellation: CancellationToken,
        progress: ProgressCallback | None,
        round_number: int | None = None,
    ) -> None:
        if not cancellation.is_cancelled():
            return
        self._emit(
            progress,
            activation_id,
            "activation_cancelled",
            "Activation interrupted by the user.",
            round_number=round_number,
        )
        raise AgentCancelled("Activation interrupted by the user")

    def _emit(
        self,
        callback: ProgressCallback | None,
        activation_id: str,
        kind: ProgressKind,
        message: str,
        *,
        round_number: int | None = None,
        name: str | None = None,
        result: Any = None,
    ) -> None:
        if callback is None:
            return
        try:
            callback(ActivationProgress(kind, self.agent_id, activation_id, message, round_number, name, result))
        except Exception as exc:
            LOGGER.warning("Activation progress callback failed agent_id=%s error_type=%s", self.agent_id, type(exc).__name__)

    def _emit_started(
        self,
        callback: ProgressCallback | None,
        activation_id: str,
        prefix: Literal["capability", "executor", "control", "memory"],
        action: ActionRequest,
        round_number: int,
    ) -> None:
        self._emit(
            callback,
            activation_id,
            f"{prefix}_started",  # type: ignore[arg-type]
            f"Running {prefix} {action.name}.",
            round_number=round_number,
            name=action.name,
            result=action.arguments,
        )

    def _emit_completed(
        self,
        callback: ProgressCallback | None,
        activation_id: str,
        prefix: Literal["capability", "executor", "control", "memory"],
        action: ActionRequest,
        round_number: int,
        result: Any,
    ) -> None:
        self._emit(
            callback,
            activation_id,
            f"{prefix}_completed",  # type: ignore[arg-type]
            f"{prefix.title()} {action.name} completed.",
            round_number=round_number,
            name=action.name,
            result=result,
        )


__all__ = [
    "ActivationProgress",
    "ActivationState",
    "AgentActivationError",
    "AgentCancelled",
    "CancellationToken",
    "PersistentAgent",
    "ProgressCallback",
    "ProgressKind",
    "normalize_agent_id",
]
