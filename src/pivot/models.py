"""Shared, dependency-free data models used across pivot modules."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

CapabilityKind = Literal["think", "measure", "work"]


@dataclass(frozen=True, slots=True)
class Message:
    """A chat message in the internal provider-neutral representation."""

    role: str
    content: str | None = None
    name: str | None = None
    tool_calls: tuple["ToolCall", ...] = ()
    tool_call_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"role": self.role}
        if self.content is not None:
            value["content"] = self.content
        if self.name is not None:
            value["name"] = self.name
        if self.tool_calls:
            value["tool_calls"] = [call.as_dict() for call in self.tool_calls]
        if self.tool_call_id is not None:
            value["tool_call_id"] = self.tool_call_id
        return value


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A model request to invoke a capability."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        # OpenAI-compatible APIs represent function arguments as a JSON string.
        function: dict[str, Any] = {"name": self.name, "arguments": json.dumps(self.arguments, ensure_ascii=False)}
        return {"id": self.call_id, "type": "function", "function": function}


@dataclass(frozen=True, slots=True)
class ParsedResponse:
    """The user-facing text and capability calls extracted from an LLM response."""

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    raw: Any = None


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    """Metadata injected into prompts and used for dispatch validation."""

    name: str
    kind: CapabilityKind
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
    source: str | None = None

    def as_prompt_dict(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "description": self.description, "parameters": self.parameters}


@dataclass(frozen=True, slots=True)
class EventDescriptor:
    """A generic observable field with supported dynamic conditions."""

    name: str
    description: str
    field: str
    operators: tuple[str, ...]
    templates: dict[str, str] = field(default_factory=dict)
    timeout_template: str = "Waiting for {condition} timed out after {timeout:g} seconds."
    error_template: str = "Waiting for {condition} failed: {error}."
    source: str | None = None

    def as_prompt_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "field": self.field,
            "operators": list(self.operators),
        }
