"""Conversation lifecycle and capability-call loop."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from .capabilities import CapabilityError, CapabilityRegistry
from .events import EVENT_WAIT_TOOL, EventError, EventPool, EventService
from .llm import LLMClient
from .logging import log_context
from .memory import TextMemory
from .models import Message, ToolCall
from .parser import ResponseParseError, parse_response

LOGGER = logging.getLogger(__name__)


class SessionError(RuntimeError):
    """Raised when a session cannot make progress."""


class SessionCancelled(SessionError):
    """Raised when the caller interrupts an active conversation turn."""


class CancellationToken:
    """Thread-safe cooperative cancellation signal for one conversation turn."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        """Request cancellation at the next safe session boundary."""

        self._event.set()

    def is_cancelled(self) -> bool:
        """Return whether cancellation was requested."""

        return self._event.is_set()


ProgressKind = Literal[
    "turn_started",
    "llm_waiting",
    "assistant_update",
    "capability_started",
    "capability_completed",
    "capability_failed",
    "event_wait_started",
    "event_completed",
    "turn_completed",
    "turn_cancelled",
    "turn_failed",
]


@dataclass(frozen=True, slots=True)
class SessionProgress:
    """A user-safe progress update emitted while a session is working."""

    kind: ProgressKind
    session_id: str
    message: str
    round_number: int | None = None
    name: str | None = None
    result: Any = None


ProgressCallback = Callable[[SessionProgress], None]


class SessionState(StrEnum):
    """Lifecycle state maintained by the core conversation runtime."""

    READY = "ready"
    RUNNING = "running"
    PENDING = "pending"


def normalize_session_id(session_id: str) -> str:
    """Validate and canonicalize a session UUID."""

    try:
        return str(UUID(session_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("session_id must be a valid UUID") from exc


class ConversationSession:
    """Run one bounded, observable conversation against an LLM client."""

    def __init__(
        self,
        session_id: str,
        *,
        llm: LLMClient,
        capabilities: CapabilityRegistry,
        memory: TextMemory | None = None,
        events: EventPool | None = None,
        event_service: EventService | None = None,
        max_rounds: int = 8,
    ) -> None:
        if max_rounds < 1:
            raise ValueError("max_rounds must be positive")
        self.session_id = normalize_session_id(session_id)
        self.llm = llm
        self.capabilities = capabilities
        self.memory = memory
        self.events = events
        self.event_service = event_service
        self.max_rounds = max_rounds
        self.history: list[Message] = []
        self._state = SessionState.READY
        self._last_active_at = time.time()
        self._state_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._restore()

    @property
    def state(self) -> SessionState:
        """Return the current runtime lifecycle state."""

        with self._state_lock:
            return self._state

    @property
    def last_active_at(self) -> float:
        """Return the wall-clock time of the latest runtime activity."""

        with self._state_lock:
            return self._last_active_at

    def _set_state(self, state: SessionState) -> None:
        if not isinstance(state, SessionState):
            raise TypeError("state must be a SessionState")
        with self._state_lock:
            self._state = state
            self._last_active_at = time.time()
        LOGGER.debug("Session state changed session_id=%s state=%s", self.session_id, state)

    def _restore(self) -> None:
        """Restore valid JSON-lines history while isolating corrupt memory files."""

        if not self.memory:
            return
        try:
            content = self.memory.read(self.session_id)
            restored: list[Message] = []
            for line in content.splitlines():
                value = json.loads(line)
                calls = []
                for raw_call in value.get("tool_calls", []):
                    function = raw_call.get("function", raw_call)
                    arguments = function.get("arguments", {})
                    if isinstance(arguments, str):
                        arguments = json.loads(arguments)
                    calls.append(ToolCall(function["name"], arguments, raw_call.get("id")))
                restored.append(Message(value["role"], value.get("content"), value.get("name"), tuple(calls), value.get("tool_call_id")))
            self.history = restored
            if restored:
                try:
                    self._last_active_at = self.memory.path_for(self.session_id).stat().st_mtime
                except OSError:
                    LOGGER.debug("Unable to read session memory timestamp session_id=%s", self.session_id)
                LOGGER.info("Session memory restored session_id=%s messages=%d", self.session_id, len(restored))
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            LOGGER.warning("Unable to restore session %s memory; starting a new context: %s", self.session_id, exc)
            self.history = []

    def _context_message(self) -> Message:
        event_context: Sequence[dict[str, Any]] = ()
        if self.events:
            event_context = [event.as_prompt_dict() for event in self.events.descriptors()]
        context = {
            "capabilities": self.capabilities.prompt_context(),
            "events": event_context,
            "instruction": (
                "Think capabilities are lazy summaries. Call pivot_read_think to read a relevant think capability "
                "before applying it. Use executable capabilities only when needed and return concise user-facing text."
            ),
        }
        return Message(role="system", content="pivot runtime context:\n" + json.dumps(context, ensure_ascii=False, default=str))

    def _persist(self) -> None:
        if not self.memory:
            return
        lines = [json.dumps(message.as_dict(), ensure_ascii=False, default=str) for message in self.history]
        self.memory.write(self.session_id, "\n".join(lines) + ("\n" if lines else ""))

    def run(
        self,
        user_input: str,
        *,
        progress: ProgressCallback | None = None,
        cancellation: CancellationToken | None = None,
    ) -> str:
        if not user_input.strip():
            raise ValueError("user_input must not be empty")
        if not self._run_lock.acquire(blocking=False):
            raise SessionError("Session is already running")
        self._set_state(SessionState.RUNNING)
        try:
            with log_context(correlation_id=str(uuid4()), session_id=self.session_id):
                return self._run_correlated(user_input, progress=progress, cancellation=cancellation)
        finally:
            self._set_state(SessionState.READY)
            self._run_lock.release()

    def _run_correlated(
        self,
        user_input: str,
        *,
        progress: ProgressCallback | None = None,
        cancellation: CancellationToken | None = None,
    ) -> str:
        """Run one turn under the correlation context established by ``run``."""

        self._raise_if_cancelled(cancellation, progress)
        if not self.history:
            self.history.append(self._context_message())
        self.history.append(Message(role="user", content=user_input))
        self._persist()
        rollback_to = len(self.history)
        LOGGER.info("Session turn started session_id=%s input_length=%d", self.session_id, len(user_input))
        self._emit_progress(progress, "turn_started", "Turn started.")
        for round_number in range(1, self.max_rounds + 1):
            self._raise_if_cancelled(cancellation, progress, round_number=round_number, rollback_to=rollback_to)
            LOGGER.debug("Session LLM round started session_id=%s round=%d", self.session_id, round_number)
            self._emit_progress(progress, "llm_waiting", "Waiting for the model.", round_number=round_number)
            try:
                tools = self.capabilities.llm_tools() + (self.event_service.llm_tools() if self.event_service else ())
                raw = self.llm.complete(self.history, tools=tools)
                self._raise_if_cancelled(cancellation, progress, round_number=round_number, rollback_to=rollback_to)
                response = parse_response(raw)
            except SessionCancelled:
                raise
            except Exception as exc:
                # Preserve a stable public exception while logging the implementation detail.
                LOGGER.exception("Session %s failed in LLM round %d", self.session_id, round_number)
                self._emit_progress(progress, "turn_failed", f"LLM round failed: {type(exc).__name__}.", round_number=round_number)
                raise SessionError(f"LLM round {round_number} failed: {type(exc).__name__}: {exc}") from exc
            self.history.append(Message(role="assistant", content=response.text, tool_calls=response.tool_calls))
            LOGGER.debug(
                "Session LLM round completed session_id=%s round=%d tool_calls=%d text_length=%d",
                self.session_id,
                round_number,
                len(response.tool_calls),
                len(response.text),
            )
            if not response.tool_calls:
                self._persist()
                LOGGER.info("Session turn completed session_id=%s rounds=%d", self.session_id, round_number)
                self._emit_progress(progress, "turn_completed", "Turn completed.", round_number=round_number, result=response.text)
                return response.text
            if response.text.strip():
                self._emit_progress(
                    progress,
                    "assistant_update",
                    response.text.strip(),
                    round_number=round_number,
                )
            for call in response.tool_calls:
                self._raise_if_cancelled(cancellation, progress, round_number=round_number, rollback_to=rollback_to)
                try:
                    if call.name == EVENT_WAIT_TOOL:
                        self._set_state(SessionState.PENDING)
                        self._emit_progress(
                            progress,
                            "event_wait_started",
                            f"Waiting for event {call.arguments.get('event', 'unknown')}.",
                            round_number=round_number,
                            name=call.name,
                            result=call.arguments,
                        )
                        try:
                            result = self._wait_for_event(
                                call,
                                cancellation=cancellation,
                                progress=progress,
                                round_number=round_number,
                                rollback_to=rollback_to,
                            )
                        finally:
                            self._set_state(SessionState.RUNNING)
                        self._emit_progress(
                            progress,
                            "event_completed",
                            f"Event wait finished with status {result.get('status', 'unknown')}.",
                            round_number=round_number,
                            name=call.name,
                            result=result,
                        )
                    else:
                        self._emit_progress(
                            progress,
                            "capability_started",
                            f"Running capability {call.name}.",
                            round_number=round_number,
                            name=call.name,
                            result=call.arguments,
                        )
                        result = self.capabilities.execute(call)
                        self._emit_progress(
                            progress,
                            "capability_completed",
                            f"Capability {call.name} completed.",
                            round_number=round_number,
                            name=call.name,
                            result=result,
                        )
                    encoded = json.dumps(result, ensure_ascii=False)
                except (CapabilityError, EventError) as exc:
                    LOGGER.warning("Session capability call failed session_id=%s capability=%s", self.session_id, call.name)
                    self._emit_progress(
                        progress,
                        "capability_failed" if call.name != EVENT_WAIT_TOOL else "event_completed",
                        f"{call.name} failed: {exc}.",
                        round_number=round_number,
                        name=call.name,
                        result={"error": str(exc)},
                    )
                    encoded = json.dumps({"error": str(exc)}, ensure_ascii=False)
                self.history.append(Message(role="tool", content=encoded, name=call.name, tool_call_id=call.call_id))
                self._raise_if_cancelled(cancellation, progress, round_number=round_number, rollback_to=rollback_to)
            self._persist()
        LOGGER.error("Session exceeded maximum rounds session_id=%s max_rounds=%d", self.session_id, self.max_rounds)
        self._emit_progress(progress, "turn_failed", f"Maximum rounds reached ({self.max_rounds}).")
        raise SessionError(f"Session exceeded maximum LLM rounds ({self.max_rounds})")

    def _emit_progress(
        self,
        callback: ProgressCallback | None,
        kind: ProgressKind,
        message: str,
        *,
        round_number: int | None = None,
        name: str | None = None,
        result: Any = None,
    ) -> None:
        if callback is None:
            return
        update = SessionProgress(kind, self.session_id, message, round_number, name, result)
        try:
            callback(update)
        except Exception as exc:
            LOGGER.warning("Session progress callback failed session_id=%s error_type=%s", self.session_id, type(exc).__name__)

    def _raise_if_cancelled(
        self,
        cancellation: CancellationToken | None,
        progress: ProgressCallback | None,
        *,
        round_number: int | None = None,
        rollback_to: int | None = None,
    ) -> None:
        if cancellation is None or not cancellation.is_cancelled():
            return
        LOGGER.info("Session turn interrupted session_id=%s round=%s", self.session_id, round_number or "pending")
        if rollback_to is not None:
            del self.history[rollback_to:]
        self._persist()
        self._emit_progress(progress, "turn_cancelled", "Turn interrupted by the user.", round_number=round_number)
        raise SessionCancelled("Turn interrupted by the user")

    def _wait_for_event(
        self,
        call: ToolCall,
        *,
        cancellation: CancellationToken | None = None,
        progress: ProgressCallback | None = None,
        round_number: int | None = None,
        rollback_to: int | None = None,
    ) -> dict[str, Any]:
        if self.event_service is None:
            raise EventError("Event service is not available")
        required = {"event", "operator", "expected", "timeout"}
        if set(call.arguments) != required:
            raise EventError(f"Event wait arguments must be exactly: {', '.join(sorted(required))}")
        event = call.arguments["event"]
        operator_name = call.arguments["operator"]
        timeout = call.arguments["timeout"]
        if not isinstance(event, str) or not isinstance(operator_name, str) or not isinstance(timeout, (int, float)):
            raise EventError("Event wait has invalid argument types")
        try:
            notification = self.event_service.wait(
                session_id=self.session_id,
                event=event,
                operator=operator_name,
                expected=call.arguments["expected"],
                timeout=float(timeout),
                is_cancelled=cancellation.is_cancelled if cancellation else None,
            )
        except InterruptedError:
            self._raise_if_cancelled(
                cancellation,
                progress,
                round_number=round_number,
                rollback_to=rollback_to,
            )
            raise
        return notification.as_dict()


class SessionManager:
    """Create and retain sessions while keeping conversation state isolated."""

    def __init__(self, **session_dependencies: Any) -> None:
        self._dependencies = session_dependencies
        self._sessions: dict[str, ConversationSession] = {}
        self._lock = threading.RLock()

    def create(self) -> ConversationSession:
        """Create a session with a unique UUID4 identifier."""

        with self._lock:
            while True:
                session_id = str(uuid4())
                if session_id not in self._sessions:
                    return self.get(session_id)

    def get(self, session_id: str) -> ConversationSession:
        canonical = normalize_session_id(session_id)
        with self._lock:
            if canonical not in self._sessions:
                self._sessions[canonical] = ConversationSession(canonical, **self._dependencies)
                LOGGER.info("Session created session_id=%s", canonical)
            return self._sessions[canonical]

    def run(
        self,
        session_id: str,
        user_input: str,
        *,
        progress: ProgressCallback | None = None,
        cancellation: CancellationToken | None = None,
    ) -> str:
        return self.get(session_id).run(user_input, progress=progress, cancellation=cancellation)

    def sessions(self) -> tuple[ConversationSession, ...]:
        """Return sessions in stable UUID order for interactive status displays."""

        with self._lock:
            return tuple(self._sessions[key] for key in sorted(self._sessions))

    def available_sessions(self) -> tuple[ConversationSession, ...]:
        """Load and return managed and persisted sessions in stable UUID order."""

        if self._dependencies.get("memory") is not None:
            memory = self._dependencies["memory"]
            try:
                paths = tuple(memory.root.iterdir()) if memory.root.is_dir() else ()
            except OSError as exc:
                LOGGER.warning("Unable to discover persisted sessions error_type=%s", type(exc).__name__)
                paths = ()
            for path in paths:
                if not path.is_dir() or not (path / "history.jsonl").is_file():
                    continue
                try:
                    self.get(path.name)
                except (ValueError, OSError):
                    LOGGER.debug("Skipping invalid persisted session path=%s", path)
        return self.sessions()
