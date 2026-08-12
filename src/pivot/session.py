"""Conversation lifecycle and capability-call loop."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

from .capabilities import CapabilityError, CapabilityRegistry
from .events import EventPool
from .llm import LLMClient
from .logging import log_context
from .memory import TextMemory
from .models import Message, ToolCall
from .parser import ResponseParseError, parse_response

LOGGER = logging.getLogger(__name__)


class SessionError(RuntimeError):
    """Raised when a session cannot make progress."""


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
        max_rounds: int = 8,
    ) -> None:
        if max_rounds < 1:
            raise ValueError("max_rounds must be positive")
        self.session_id = normalize_session_id(session_id)
        self.llm = llm
        self.capabilities = capabilities
        self.memory = memory
        self.events = events
        self.max_rounds = max_rounds
        self.history: list[Message] = []
        self._restore()

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

    def run(self, user_input: str) -> str:
        with log_context(correlation_id=str(uuid4()), session_id=self.session_id):
            return self._run_correlated(user_input)

    def _run_correlated(self, user_input: str) -> str:
        """Run one turn under the correlation context established by ``run``."""

        if not user_input.strip():
            raise ValueError("user_input must not be empty")
        if not self.history:
            self.history.append(self._context_message())
        self.history.append(Message(role="user", content=user_input))
        self._persist()
        LOGGER.info("Session turn started session_id=%s input_length=%d", self.session_id, len(user_input))
        for round_number in range(1, self.max_rounds + 1):
            LOGGER.debug("Session LLM round started session_id=%s round=%d", self.session_id, round_number)
            try:
                raw = self.llm.complete(self.history, tools=self.capabilities.llm_tools())
                response = parse_response(raw)
            except Exception as exc:
                # Preserve a stable public exception while logging the implementation detail.
                LOGGER.exception("Session %s failed in LLM round %d", self.session_id, round_number)
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
                return response.text
            for call in response.tool_calls:
                try:
                    result = self.capabilities.execute(call)
                    encoded = json.dumps(result, ensure_ascii=False, default=str)
                except CapabilityError as exc:
                    LOGGER.warning("Session capability call failed session_id=%s capability=%s", self.session_id, call.name)
                    encoded = json.dumps({"error": str(exc)}, ensure_ascii=False)
                self.history.append(Message(role="tool", content=encoded, name=call.name, tool_call_id=call.call_id))
            self._persist()
        LOGGER.error("Session exceeded maximum rounds session_id=%s max_rounds=%d", self.session_id, self.max_rounds)
        raise SessionError(f"Session exceeded maximum LLM rounds ({self.max_rounds})")


class SessionManager:
    """Create and retain sessions while keeping conversation state isolated."""

    def __init__(self, **session_dependencies: Any) -> None:
        self._dependencies = session_dependencies
        self._sessions: dict[str, ConversationSession] = {}

    def create(self) -> ConversationSession:
        """Create a session with a unique UUID4 identifier."""

        while True:
            session_id = str(uuid4())
            if session_id not in self._sessions:
                return self.get(session_id)

    def get(self, session_id: str) -> ConversationSession:
        canonical = normalize_session_id(session_id)
        if canonical not in self._sessions:
            self._sessions[canonical] = ConversationSession(canonical, **self._dependencies)
            LOGGER.info("Session created session_id=%s", canonical)
        return self._sessions[canonical]

    def run(self, session_id: str, user_input: str) -> str:
        return self.get(session_id).run(user_input)
