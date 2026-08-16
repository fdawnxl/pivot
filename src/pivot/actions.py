"""Normalize model output into one framework action protocol."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import uuid4

from .events import EVENT_WAIT_TOOL
from .models import MessageContent, ParsedResponse, ToolCall

ACTION_TOOL = "pivot_action"
ACTION_TAG = "pivot-action"
_TAGGED_ACTION = re.compile(
    r"<pivot-action>\s*(.*?)\s*</pivot-action>", re.DOTALL | re.IGNORECASE
)
_FENCED_ACTION = re.compile(r"```pivot-action\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


class ActionError(RuntimeError):
    """Raised when a model action is malformed or cannot be routed."""


class ActionKind(StrEnum):
    """Framework action destinations shared by every model-facing mechanism."""

    CAPABILITY = "capability"
    EVENT = "event"
    CONTROL = "control"
    EXECUTOR = "executor"
    MEMORY = "memory"


_ACTION_KIND_ALIASES = {
    "think": ActionKind.CAPABILITY,
    "measure": ActionKind.CAPABILITY,
    "work": ActionKind.CAPABILITY,
}


@dataclass(frozen=True, slots=True)
class ActionRequest:
    """One validated action emitted by an agent."""

    kind: ActionKind
    name: str
    arguments: dict[str, Any]
    call: ToolCall


@dataclass(frozen=True, slots=True)
class DetectedActions:
    """User-facing response text plus normalized framework actions."""

    text: str
    content: MessageContent | None
    calls: tuple[ToolCall, ...]
    actions: tuple[ActionRequest, ...]


def action_tool() -> dict[str, Any]:
    """Return the single preferred tool schema used for framework actions."""

    return {
        "type": "function",
        "function": {
            "name": ACTION_TOOL,
            "description": (
                "Perform one pivot framework action. Use this for capabilities, event waits, "
                "agent control, command executors, and durable memory. For think, measure, and work capabilities, "
                "kind must be capability."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": [item.value for item in ActionKind],
                    },
                    "name": {"type": "string", "minLength": 1},
                    "arguments": {"type": "object"},
                },
                "required": ["kind", "name", "arguments"],
                "additionalProperties": False,
            },
        },
    }


class ActionDetector:
    """Detect native tool calls and fixed-format textual actions uniformly."""

    def detect(
        self,
        response: ParsedResponse,
        *,
        capability_names: tuple[str, ...] = (),
        executor_tools: dict[str, str] | None = None,
    ) -> DetectedActions:
        actions: list[ActionRequest] = []
        calls: list[ToolCall] = []
        executor_tools = executor_tools or {}

        for call in response.tool_calls:
            request = self._from_tool_call(call, capability_names, executor_tools)
            calls.append(call)
            actions.append(request)

        text = response.text
        textual: list[ActionRequest] = []
        for pattern in (_TAGGED_ACTION, _FENCED_ACTION):
            matches = list(pattern.finditer(text))
            for match in matches:
                textual.append(self._from_json(match.group(1)))
            text = pattern.sub("", text)
        text = text.strip()
        for request in textual:
            calls.append(request.call)
            actions.append(request)

        content = response.content
        if textual and isinstance(content, str):
            content = text
        elif textual and isinstance(content, tuple):
            parts = []
            for part in content:
                normalized = dict(part)
                if isinstance(normalized.get("text"), str):
                    normalized["text"] = _strip_action_markup(
                        normalized["text"]
                    ).strip()
                parts.append(normalized)
            content = tuple(parts)
        return DetectedActions(text, content, tuple(calls), tuple(actions))

    def _from_tool_call(
        self,
        call: ToolCall,
        capability_names: tuple[str, ...],
        executor_tools: dict[str, str],
    ) -> ActionRequest:
        if call.name == ACTION_TOOL:
            return self._from_mapping(call.arguments, call=call)
        if call.name == EVENT_WAIT_TOOL:
            return ActionRequest(ActionKind.EVENT, "wait", dict(call.arguments), call)
        if call.name in executor_tools:
            return ActionRequest(
                ActionKind.EXECUTOR,
                executor_tools[call.name],
                dict(call.arguments),
                call,
            )
        # Existing provider-native capability calls remain supported and pass
        # through the same normalized routing path. Unknown tools intentionally
        # reach the capability registry so it can return its diagnostic error.
        if call.name in capability_names or call.name not in {
            ACTION_TOOL,
            EVENT_WAIT_TOOL,
        }:
            return ActionRequest(
                ActionKind.CAPABILITY, call.name, dict(call.arguments), call
            )
        raise ActionError(f"Unsupported framework tool: {call.name}")

    def _from_json(self, value: str) -> ActionRequest:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ActionError(f"Invalid {ACTION_TAG} JSON: {exc.msg}") from exc
        if not isinstance(parsed, dict):
            raise ActionError(f"{ACTION_TAG} content must be a JSON object")
        call = ToolCall(ACTION_TOOL, dict(parsed), f"action-{uuid4()}")
        return self._from_mapping(parsed, call=call)

    @staticmethod
    def _from_mapping(value: dict[str, Any], *, call: ToolCall) -> ActionRequest:
        if set(value) != {"kind", "name", "arguments"}:
            raise ActionError("pivot_action requires exactly kind, name, and arguments")
        kind = value.get("kind")
        name = value.get("name")
        arguments = value.get("arguments")
        try:
            alias = (
                _ACTION_KIND_ALIASES.get(kind, kind) if isinstance(kind, str) else kind
            )
            normalized_kind = ActionKind(alias)
        except (TypeError, ValueError) as exc:
            raise ActionError(f"Unknown pivot action kind: {kind!r}") from exc
        if not isinstance(name, str) or not name.strip():
            raise ActionError("pivot_action name must be a non-empty string")
        if not isinstance(arguments, dict):
            raise ActionError("pivot_action arguments must be a JSON object")
        return ActionRequest(normalized_kind, name, dict(arguments), call)


def _strip_action_markup(value: str) -> str:
    for pattern in (_TAGGED_ACTION, _FENCED_ACTION):
        value = pattern.sub("", value)
    return value


__all__ = [
    "ACTION_TAG",
    "ACTION_TOOL",
    "ActionDetector",
    "ActionError",
    "ActionKind",
    "ActionRequest",
    "DetectedActions",
    "action_tool",
]
