"""Conversation lifecycle and capability-call loop."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

from .capabilities import CapabilityError, CapabilityRegistry
from .events import EventPool
from .llm import LLMClient
from .memory import TextMemory
from .models import Message, ToolCall
from .parser import ResponseParseError, parse_response

LOGGER = logging.getLogger(__name__)


class SessionError(RuntimeError):
    """Raised when a session cannot make progress."""


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
        self.session_id = session_id
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
            "instruction": "Use capabilities only when needed. Return concise user-facing text when work is complete.",
        }
        return Message(role="system", content="pivot runtime context:\n" + json.dumps(context, ensure_ascii=False, default=str))

    def _persist(self) -> None:
        if not self.memory:
            return
        lines = [json.dumps(message.as_dict(), ensure_ascii=False, default=str) for message in self.history]
        self.memory.write(self.session_id, "\n".join(lines) + ("\n" if lines else ""))

    def run(self, user_input: str) -> str:
        if not user_input.strip():
            raise ValueError("user_input must not be empty")
        if not self.history:
            self.history.append(self._context_message())
        self.history.append(Message(role="user", content=user_input))
        self._persist()
        for round_number in range(1, self.max_rounds + 1):
            try:
                raw = self.llm.complete(self.history, tools=self.capabilities.llm_tools())
                response = parse_response(raw)
            except Exception as exc:
                # Preserve a stable public exception while logging the implementation detail.
                LOGGER.exception("Session %s failed in LLM round %d", self.session_id, round_number)
                raise SessionError(f"LLM round {round_number} failed: {type(exc).__name__}: {exc}") from exc
            self.history.append(Message(role="assistant", content=response.text, tool_calls=response.tool_calls))
            if not response.tool_calls:
                self._persist()
                return response.text
            for call in response.tool_calls:
                try:
                    result = self.capabilities.execute(call)
                    encoded = json.dumps(result, ensure_ascii=False, default=str)
                except CapabilityError as exc:
                    encoded = json.dumps({"error": str(exc)}, ensure_ascii=False)
                self.history.append(Message(role="tool", content=encoded, name=call.name, tool_call_id=call.call_id))
            self._persist()
        raise SessionError(f"Session exceeded maximum LLM rounds ({self.max_rounds})")


class SessionManager:
    """Create and retain sessions while keeping conversation state isolated."""

    def __init__(self, **session_dependencies: Any) -> None:
        self._dependencies = session_dependencies
        self._sessions: dict[str, ConversationSession] = {}

    def get(self, session_id: str) -> ConversationSession:
        if session_id not in self._sessions:
            self._sessions[session_id] = ConversationSession(session_id, **self._dependencies)
        return self._sessions[session_id]

    def run(self, session_id: str, user_input: str) -> str:
        return self.get(session_id).run(user_input)
